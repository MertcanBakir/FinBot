import json
import logging
import os
import tempfile
from typing import Dict, List, Optional, Protocol, Set

from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    KapSymbolEarningsStatus,
    Nasdaq100EarningsCalendar,
)
from app.services.database_calendar_reader import database_calendar_reader

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "generated")
BIST_ROLLING_CACHE_FILE = os.path.join(CACHE_DIR, "bist100_earnings_calendar.json")
NASDAQ_ROLLING_CACHE_FILE = os.path.join(CACHE_DIR, "nasdaq100_earnings_calendar.json")


class CalendarStateStore(Protocol):
    bist_file_path: str
    nasdaq_file_path: str

    def load_bist(self) -> Optional[Bist100EarningsCalendar]: ...
    def load_nasdaq(self) -> Optional[Nasdaq100EarningsCalendar]: ...
    def save_bist(self, calendar: Bist100EarningsCalendar) -> bool: ...
    def save_nasdaq(self, calendar: Nasdaq100EarningsCalendar) -> bool: ...


def merge_bist_partial_calendar(
    previous: Bist100EarningsCalendar,
    current: Bist100EarningsCalendar,
) -> Bist100EarningsCalendar:
    """Pure transformation helper to merge a partial BIST100 refresh with previous canonical cache.

    Rules:
    - Current coverage window [coverage_start, coverage_end], generated_at, warnings, and metrics are authoritative.
    - Status remains 'partial'.
    - For symbols where current.is_complete == True: current results are authoritative.
      Previous events for this symbol are completely replaced (0 current events = healthy negative).
    - For symbols where current.is_complete == False (or missing in current):
      Previous events within current [coverage_start, coverage_end] are carried forward.
      Symbol status remains 'unavailable' (is_complete = False).
    """
    merged = current.model_copy(deep=True)
    merged.status = "partial"
    merged.provider_status = "partial"

    cov_start = current.coverage_start
    cov_end = current.coverage_end

    # Map current symbol results
    current_sym_results = {s.symbol: s.model_copy(deep=True) for s in current.symbol_results}
    prev_sym_results = {s.symbol: s.model_copy(deep=True) for s in previous.symbol_results}

    # Track complete vs incomplete symbols in current
    complete_symbols: Set[str] = {
        sym for sym, st in current_sym_results.items() if st.is_complete
    }

    # Identify symbols present in previous but completely missing from current
    missing_symbols = set(prev_sym_results.keys()) - set(current_sym_results.keys())
    for sym in missing_symbols:
        prev_st = prev_sym_results[sym]
        missing_st = KapSymbolEarningsStatus(
            symbol=sym,
            member_resolution_status=prev_st.member_resolution_status,
            refresh_status="failed",
            calendar_status="unavailable",
            scheduled_event_count=0,
            is_complete=False,
            warnings=["Symbol missing from current partial refresh; previous cached events carried forward"]
        )
        current_sym_results[sym] = missing_st

    incomplete_symbols: Set[str] = {
        sym for sym, st in current_sym_results.items() if not st.is_complete
    }

    # Symbols whose Official IR fetch was incomplete/failed
    ir_incomplete_syms: Set[str] = set(getattr(current, "ir_incomplete_symbols", []))

    # 1. Collect events directly from current
    merged_events: List[BistEarningsEvent] = list(current.events)
    current_identities = {(ev.symbol, ev.period_start, ev.period_end) for ev in merged_events}

    # 2. Collect carried-forward events for incomplete/missing symbols from previous (within current coverage window)
    carried_forward_symbols: Set[str] = set()

    for prev_ev in previous.events:
        sym = prev_ev.symbol
        if cov_start <= prev_ev.earnings_date <= cov_end:
            key = (prev_ev.symbol, prev_ev.period_start, prev_ev.period_end)
            if sym in incomplete_symbols:
                if key not in current_identities:
                    merged_events.append(prev_ev.model_copy(deep=True))
                    carried_forward_symbols.add(sym)
            elif sym in ir_incomplete_syms and prev_ev.source == "Official IR":
                if key not in current_identities:
                    merged_events.append(prev_ev.model_copy(deep=True))
                    carried_forward_symbols.add(sym)

    # Add per-symbol warning for carried-forward symbols if not already present
    for sym in incomplete_symbols:
        st = current_sym_results[sym]
        st.refresh_status = "failed"
        st.calendar_status = "unavailable"
        st.is_complete = False

        warn_msg = "Previous cached BIST events carried forward because current symbol refresh was unavailable"
        if sym in carried_forward_symbols and warn_msg not in st.warnings:
            st.warnings.append(warn_msg)

    for sym in ir_incomplete_syms:
        if sym in carried_forward_symbols and sym not in incomplete_symbols:
            st = current_sym_results.get(sym)
            if st:
                warn_msg = f"Previous cached Official IR events carried forward because current {sym} IR provider was unavailable"
                if warn_msg not in st.warnings:
                    st.warnings.append(warn_msg)

    # Global warning summary for carried-forward symbols
    warnings_out = list(current.warnings)
    if carried_forward_symbols:
        summary_warn = f"Carried forward previous cached BIST events for unavailable symbols: {', '.join(sorted(carried_forward_symbols))}"
        if summary_warn not in warnings_out:
            warnings_out.append(summary_warn)

    # Deduplicate events by identity (symbol, period_start, period_end) with KAP precedence
    identity_event_map: Dict[
        tuple[str, Optional[str], Optional[str]], BistEarningsEvent
    ] = {}
    for ev in merged_events:
        key = (ev.symbol, ev.period_start, ev.period_end)
        if key not in identity_event_map:
            identity_event_map[key] = ev
        else:
            existing = identity_event_map[key]
            if ev.source == "KAP" and existing.source != "KAP":
                identity_event_map[key] = ev

    unique_events = list(identity_event_map.values())
    unique_events.sort(key=lambda ev: (ev.earnings_date, ev.symbol, ev.fiscal_year or 0, ev.fiscal_period or ""))

    # Sort symbol_results by symbol
    sorted_sym_results = sorted(current_sym_results.values(), key=lambda s: s.symbol)

    merged.events = unique_events
    merged.symbol_results = sorted_sym_results
    merged.warnings = warnings_out

    return merged


class JsonCalendarStateStore:
    """Service handling loading and atomic saving of schema 2.0 canonical rolling earnings JSON caches."""

    def __init__(
        self,
        bist_file_path: Optional[str] = None,
        nasdaq_file_path: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ):
        if cache_dir:
            self.bist_file_path = bist_file_path or os.path.join(cache_dir, "bist100_earnings_calendar.json")
            self.nasdaq_file_path = nasdaq_file_path or os.path.join(cache_dir, "nasdaq100_earnings_calendar.json")
        else:
            self.bist_file_path = bist_file_path or BIST_ROLLING_CACHE_FILE
            self.nasdaq_file_path = nasdaq_file_path or NASDAQ_ROLLING_CACHE_FILE

    def _atomic_save(
        self,
        file_path: str,
        data: dict,
        label: str,
    ) -> bool:
        """Atomically save data dict to JSON file_path using tempfile in target dir + fsync + os.replace."""
        temp_name: Optional[str] = None
        try:
            parent_dir = os.path.dirname(file_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            with tempfile.NamedTemporaryFile("w", dir=parent_dir, delete=False, encoding="utf-8") as temp_file:
                temp_name = temp_file.name
                json.dump(data, temp_file, ensure_ascii=False, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())

            os.replace(temp_name, file_path)
            logger.info(f"Rolling {label} calendar successfully saved to {file_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to atomic save rolling {label} calendar cache to {file_path}: {e}", exc_info=True)
            if temp_name and os.path.exists(temp_name):
                try:
                    os.remove(temp_name)
                except Exception as rm_err:
                    logger.warning(f"Failed to cleanup temp file {temp_name}: {rm_err}")
            return False

    def load_bist(self) -> Optional[Bist100EarningsCalendar]:
        """Load and strictly validate BIST100 schema 2.0 canonical rolling earnings cache.

        Returns None if file does not exist, is corrupt, or schema/market validation fails.
        """
        if not os.path.exists(self.bist_file_path):
            return None
        try:
            with open(self.bist_file_path, encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                logger.warning(f"Invalid JSON data in {self.bist_file_path}")
                return None

            if data.get("schema_version") != "2.0" or data.get("market") != "BIST100":
                logger.warning(f"Schema/market mismatch in {self.bist_file_path} (schema: {data.get('schema_version')}, market: {data.get('market')})")
                return None

            return Bist100EarningsCalendar.model_validate(data)
        except Exception as e:
            logger.warning(f"Failed to load or validate BIST100 rolling earnings cache: {e}")
            return None

    def load_nasdaq(self) -> Optional[Nasdaq100EarningsCalendar]:
        """Load and strictly validate NASDAQ100 schema 2.0 canonical rolling earnings cache.

        Returns None if file does not exist, is corrupt, or schema/market validation fails.
        """
        if not os.path.exists(self.nasdaq_file_path):
            return None
        try:
            with open(self.nasdaq_file_path, encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                logger.warning(f"Invalid JSON data in {self.nasdaq_file_path}")
                return None

            if data.get("schema_version") != "2.0" or data.get("market") != "NASDAQ100":
                logger.warning(f"Schema/market mismatch in {self.nasdaq_file_path} (schema: {data.get('schema_version')}, market: {data.get('market')})")
                return None

            return Nasdaq100EarningsCalendar.model_validate(data)
        except Exception as e:
            logger.warning(f"Failed to load or validate NASDAQ100 rolling earnings cache: {e}")
            return None

    def save_bist(self, calendar: Bist100EarningsCalendar) -> bool:
        """Save BIST100 canonical rolling calendar to JSON cache.

        Return semantics:
          True  -> New payload successfully written atomically.
          False -> Payload not written; previous valid cache preserved or write failed.
        """
        if calendar.status == "success":
            return self._atomic_save(self.bist_file_path, calendar.model_dump(mode="json"), "BIST100")
        elif calendar.status == "failed":
            logger.warning("BIST100 fetch status is failed; preserving previous cache and skipping write.")
            return False
        elif calendar.status == "partial":
            prev = self.load_bist()
            if prev is not None:
                merged = merge_bist_partial_calendar(prev, calendar)
                return self._atomic_save(self.bist_file_path, merged.model_dump(mode="json"), "BIST100 (merged partial)")
            else:
                return self._atomic_save(self.bist_file_path, calendar.model_dump(mode="json"), "BIST100 (partial)")

        return False

    def save_nasdaq(self, calendar: Nasdaq100EarningsCalendar) -> bool:
        """Save NASDAQ100 canonical rolling calendar to JSON cache.

        Return semantics:
          True  -> New payload successfully written atomically.
          False -> Payload not written; previous valid cache preserved or write failed.
        """
        if calendar.status == "success":
            return self._atomic_save(self.nasdaq_file_path, calendar.model_dump(mode="json"), "NASDAQ100")
        elif calendar.status == "partial":
            prev = self.load_nasdaq()
            if prev is not None:
                logger.warning("NASDAQ100 fetch status is partial and valid previous cache exists; preserving previous cache.")
                return False
            else:
                return self._atomic_save(self.nasdaq_file_path, calendar.model_dump(mode="json"), "NASDAQ100 (partial)")
        elif calendar.status == "failed":
            logger.warning("NASDAQ100 fetch status is failed; preserving previous cache and skipping write.")
            return False

        return False


class SupabaseCalendarStateStore:
    """Calendar state facade backed by Supabase instead of rolling JSON files.

    Refresh jobs keep the established load/save contract for merge and report
    decisions. Saves live only for the process lifetime; the job's transactional
    Supabase synchronization is the durable write.
    """

    bist_file_path = "supabase://earnings_events?market=BIST100"
    nasdaq_file_path = "supabase://earnings_events?market=NASDAQ100"

    def __init__(self) -> None:
        self._pending_bist: Optional[Bist100EarningsCalendar] = None
        self._pending_nasdaq: Optional[Nasdaq100EarningsCalendar] = None

    def load_bist(self) -> Optional[Bist100EarningsCalendar]:
        return self._pending_bist or database_calendar_reader.load_bist()

    def load_nasdaq(self) -> Optional[Nasdaq100EarningsCalendar]:
        return self._pending_nasdaq or database_calendar_reader.load_nasdaq()

    def save_bist(self, calendar: Bist100EarningsCalendar) -> bool:
        if calendar.status == "failed":
            return False
        if calendar.status == "partial":
            previous = self.load_bist()
            self._pending_bist = (
                merge_bist_partial_calendar(previous, calendar)
                if previous is not None
                else calendar.model_copy(deep=True)
            )
        else:
            self._pending_bist = calendar.model_copy(deep=True)
        return True

    def save_nasdaq(self, calendar: Nasdaq100EarningsCalendar) -> bool:
        if calendar.status == "failed":
            return False
        if calendar.status == "partial" and self.load_nasdaq() is not None:
            return False
        self._pending_nasdaq = calendar.model_copy(deep=True)
        return True


calendar_state_store = SupabaseCalendarStateStore()
