import logging
from typing import Dict, List, Optional, Protocol, Set

from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    KapSymbolEarningsStatus,
    Nasdaq100EarningsCalendar,
)
from app.services.database_calendar_reader import database_calendar_reader

logger = logging.getLogger(__name__)

class CalendarStateStore(Protocol):
    state_source: str

    def load_bist(self) -> Optional[Bist100EarningsCalendar]: ...
    def load_nasdaq(self) -> Optional[Nasdaq100EarningsCalendar]: ...
    def stage_bist(self, calendar: Bist100EarningsCalendar) -> Optional[Bist100EarningsCalendar]: ...
    def stage_nasdaq(self, calendar: Nasdaq100EarningsCalendar) -> Optional[Nasdaq100EarningsCalendar]: ...


def merge_bist_partial_calendar(
    previous: Bist100EarningsCalendar,
    current: Bist100EarningsCalendar,
) -> Bist100EarningsCalendar:
    """Merge a partial BIST100 refresh with the previous canonical Supabase state.

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
            warnings=["Symbol missing from current partial refresh; previously stored events carried forward"]
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

        warn_msg = "Previously stored BIST events carried forward because current symbol refresh was unavailable"
        if sym in carried_forward_symbols and warn_msg not in st.warnings:
            st.warnings.append(warn_msg)

    for sym in ir_incomplete_syms:
        if sym in carried_forward_symbols and sym not in incomplete_symbols:
            st = current_sym_results.get(sym)
            if st:
                warn_msg = f"Previously stored Official IR events carried forward because current {sym} IR provider was unavailable"
                if warn_msg not in st.warnings:
                    st.warnings.append(warn_msg)

    # Global warning summary for carried-forward symbols
    warnings_out = list(current.warnings)
    if carried_forward_symbols:
        summary_warn = f"Carried forward previously stored BIST events for unavailable symbols: {', '.join(sorted(carried_forward_symbols))}"
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


class SupabaseCalendarStateStore:
    """Read prior earnings state from Supabase and stage the current result in memory.

    The refresh job performs the durable, transactional Supabase write separately.
    """

    state_source = "supabase://earnings_events"

    def __init__(self) -> None:
        self._pending_bist: Optional[Bist100EarningsCalendar] = None
        self._pending_nasdaq: Optional[Nasdaq100EarningsCalendar] = None

    def load_bist(self) -> Optional[Bist100EarningsCalendar]:
        return self._pending_bist or database_calendar_reader.load_bist()

    def load_nasdaq(self) -> Optional[Nasdaq100EarningsCalendar]:
        return self._pending_nasdaq or database_calendar_reader.load_nasdaq()

    def stage_bist(self, calendar: Bist100EarningsCalendar) -> Optional[Bist100EarningsCalendar]:
        if calendar.status == "failed":
            return None
        if calendar.status == "partial":
            previous = self.load_bist()
            self._pending_bist = (
                merge_bist_partial_calendar(previous, calendar)
                if previous is not None
                else calendar.model_copy(deep=True)
            )
        else:
            self._pending_bist = calendar.model_copy(deep=True)
        return self._pending_bist

    def stage_nasdaq(self, calendar: Nasdaq100EarningsCalendar) -> Optional[Nasdaq100EarningsCalendar]:
        if calendar.status == "failed":
            return None
        if calendar.status == "partial" and self.load_nasdaq() is not None:
            return None
        self._pending_nasdaq = calendar.model_copy(deep=True)
        return self._pending_nasdaq


calendar_state_store = SupabaseCalendarStateStore()
