import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from app.models.earnings import (
    BistEarningsEvent,
    BistFiscalPeriodEarningsCalendar,
    EarningsCalendarStatus,
    FiscalPeriod,
)

logger = logging.getLogger(__name__)

DEFAULT_PARTITION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "generated",
    "earnings",
    "bist100"
)


class BistPartitionUpsertResult(BaseModel):
    status: EarningsCalendarStatus = "success"
    files_created: int = 0
    files_updated: int = 0
    events_added: int = 0
    events_changed: int = 0
    events_unchanged: int = 0
    affected_files: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None


def is_incoming_event_superior_or_newer(inc_ev: BistEarningsEvent, cur_ev: BistEarningsEvent) -> bool:
    """Determine whether incoming event takes precedence over existing event for the same identity key.

    Rules:
    - KAP > Official IR (KAP always takes precedence over IR)
    - If both KAP: compare (publish_date, disclosure_index)
    - If incoming is KAP and current is Official IR: incoming KAP wins
    - If incoming is Official IR and current is KAP: current KAP wins
    - If both Official IR:
      - Same source_provider: incoming is current live version -> incoming wins
      - Different source_provider: deterministic tiebreaker (current preserved)
    """
    if inc_ev.source == "KAP" and cur_ev.source != "KAP":
        return True
    if inc_ev.source != "KAP" and cur_ev.source == "KAP":
        return False
    if inc_ev.source == "KAP" and cur_ev.source == "KAP":
        inc_pub = inc_ev.publish_date or ""
        inc_disc = inc_ev.disclosure_index or 0
        cur_pub = cur_ev.publish_date or ""
        cur_disc = cur_ev.disclosure_index or 0
        return (inc_pub > cur_pub) or (inc_pub == cur_pub and inc_disc > cur_disc)
    # Both Official IR
    if inc_ev.source_provider == cur_ev.source_provider:
        return True
    return False


def events_are_equal(e1: BistEarningsEvent, e2: BistEarningsEvent) -> bool:
    """Compare two events across all relevant data fields."""
    return (
        e1.symbol == e2.symbol and
        e1.company_name == e2.company_name and
        e1.fiscal_year == e2.fiscal_year and
        e1.fiscal_period == e2.fiscal_period and
        e1.earnings_date == e2.earnings_date and
        e1.period_start == e2.period_start and
        e1.period_end == e2.period_end and
        e1.publish_date == e2.publish_date and
        e1.date_type == e2.date_type and
        e1.source == e2.source and
        e1.source_provider == e2.source_provider and
        e1.source_type == e2.source_type and
        e1.source_event_id == e2.source_event_id and
        e1.disclosure_index == e2.disclosure_index and
        e1.source_url == e2.source_url
    )


class BistFiscalPeriodArchive:
    """Cache service managing BIST100 fiscal period partition archive files."""

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or DEFAULT_PARTITION_DIR

    def _get_partition_filename(self, fiscal_period: FiscalPeriod, fiscal_year: int) -> str:
        return f"bist100_{fiscal_period}_{fiscal_year}_earnings_calendar.json"

    def _get_partition_path(self, fiscal_period: FiscalPeriod, fiscal_year: int) -> str:
        return os.path.join(self.data_dir, self._get_partition_filename(fiscal_period, fiscal_year))

    def load_partition(self, fiscal_year: int, fiscal_period: FiscalPeriod) -> Optional[BistFiscalPeriodEarningsCalendar]:
        """Load and validate a single fiscal period partition file."""
        filepath = self._get_partition_path(fiscal_period, fiscal_year)
        if not os.path.exists(filepath):
            return None

        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            cal = BistFiscalPeriodEarningsCalendar.model_validate(data)
            expected_fname = self._get_partition_filename(cal.fiscal_period, cal.fiscal_year)
            actual_fname = os.path.basename(filepath)
            if actual_fname != expected_fname:
                logger.error(f"Partition filename {actual_fname} mismatch with payload metadata {expected_fname}")
                return None
            return cal
        except Exception as e:
            logger.error(f"Failed to load/validate partition file {filepath}: {e}")
            return None

    def load_all_partitions(self) -> List[BistFiscalPeriodEarningsCalendar]:
        """Load all valid partition files present in the partition directory."""
        if not os.path.exists(self.data_dir):
            return []

        partitions: List[BistFiscalPeriodEarningsCalendar] = []
        for filename in sorted(os.listdir(self.data_dir)):
            if filename.startswith("bist100_") and filename.endswith("_earnings_calendar.json"):
                filepath = os.path.join(self.data_dir, filename)
                try:
                    with open(filepath, encoding="utf-8") as f:
                        data = json.load(f)
                    partition = BistFiscalPeriodEarningsCalendar.model_validate(data)
                    expected_fname = self._get_partition_filename(partition.fiscal_period, partition.fiscal_year)
                    if filename != expected_fname:
                        logger.warning(f"Skipping partition file {filename}: filename/payload metadata mismatch")
                        continue
                    partitions.append(partition)
                except Exception as e:
                    logger.warning(f"Skipping invalid/corrupt partition file {filepath}: {e}")
        return partitions

    def _atomic_save(self, partition: BistFiscalPeriodEarningsCalendar) -> bool:
        """Atomically write a partition calendar to disk."""
        os.makedirs(self.data_dir, exist_ok=True)
        filepath = self._get_partition_path(partition.fiscal_period, partition.fiscal_year)
        payload = partition.model_dump(mode="json")

        temp_fd = None
        temp_path = None
        try:
            temp_fd, temp_path = tempfile.mkstemp(
                dir=self.data_dir,
                prefix=f".tmp_{self._get_partition_filename(partition.fiscal_period, partition.fiscal_year)}_"
            )
            with open(temp_fd, "w", encoding="utf-8") as f:
                temp_fd = None  # open took ownership
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, filepath)
            temp_path = None
            logger.info(f"Successfully saved fiscal-period archive atomically to {filepath}")
            return True
        except Exception as e:
            logger.error(f"Failed to atomically save fiscal-period archive to {filepath}: {e}", exc_info=True)
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except Exception:
                    pass
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return False

    def _validate_event_for_partition(self, ev: BistEarningsEvent) -> Tuple[bool, Optional[str]]:
        """Validate that incoming event has required non-null fields for partition archive (source-aware)."""
        if ev.fiscal_year is None:
            return False, f"Event {ev.symbol} missing fiscal_year"
        if ev.fiscal_period not in ("Q1", "Q2", "Q3", "FY"):
            return False, f"Event {ev.symbol} missing/invalid fiscal_period: {ev.fiscal_period}"
        if not ev.period_start or not ev.period_end:
            return False, f"Event {ev.symbol} missing period_start/period_end"
        if not ev.source_url:
            return False, f"Event {ev.symbol} missing source_url"

        if ev.source == "KAP":
            if not ev.publish_date or ev.disclosure_index is None:
                return False, f"KAP Event {ev.symbol} missing publish_date/disclosure_index"
            if ev.source_type != "financial_calendar" or ev.date_type != "company_announced":
                return False, f"KAP Event {ev.symbol} has invalid source metadata ({ev.source_type}/{ev.date_type})"
        elif ev.source == "Official IR":
            if not ev.source_provider or not ev.source_event_id:
                return False, f"Official IR Event {ev.symbol} missing source_provider/source_event_id"
            if ev.source_type != "official_financial_calendar" or ev.date_type != "company_announced":
                return False, f"Official IR Event {ev.symbol} has invalid source metadata ({ev.source_type}/{ev.date_type})"
        else:
            return False, f"Event {ev.symbol} has unknown source ({ev.source})"

        return True, None

    def upsert_events(
        self,
        events: List[BistEarningsEvent],
        status: EarningsCalendarStatus = "success",
        generated_at: Optional[str] = None,
        warnings: Optional[List[str]] = None
    ) -> BistPartitionUpsertResult:
        """Upsert incoming normalized events into fiscal period partition archive files."""
        provider_status: EarningsCalendarStatus = status

        # Deduplicate incoming warnings deterministically
        raw_warnings = list(warnings) if warnings else []
        incoming_warnings: List[str] = []
        for w in raw_warnings:
            if w not in incoming_warnings:
                incoming_warnings.append(w)

        result_warnings: List[str] = list(incoming_warnings)

        if provider_status == "failed":
            archive_status: EarningsCalendarStatus = "failed"
            msg = "Refresh status is failed; partition files untouched."
            if msg not in result_warnings:
                result_warnings.append(msg)
            return BistPartitionUpsertResult(
                status=archive_status,
                warnings=result_warnings
            )

        if generated_at is None:
            generated_at = datetime.now(timezone.utc).isoformat()

        # Validate partition directory for corrupted or filename-mismatched files before modifying anything
        if os.path.exists(self.data_dir):
            for fname in os.listdir(self.data_dir):
                if fname.startswith("bist100_") and fname.endswith("_earnings_calendar.json"):
                    fpath = os.path.join(self.data_dir, fname)
                    try:
                        with open(fpath, encoding="utf-8") as f:
                            raw = json.load(f)
                        cal = BistFiscalPeriodEarningsCalendar.model_validate(raw)
                        expected_fname = self._get_partition_filename(cal.fiscal_period, cal.fiscal_year)
                        if fname != expected_fname:
                            raise ValueError(f"Filename {fname} does not match embedded metadata {expected_fname}")
                    except Exception as ve:
                        err_msg = f"Existing partition file {fname} is corrupt or invalid schema: {ve}"
                        logger.error(err_msg)
                        if err_msg not in result_warnings:
                            result_warnings.append(err_msg)
                        return BistPartitionUpsertResult(
                            status="failed",
                            error=err_msg,
                            warnings=result_warnings
                        )

        # Validate incoming events
        valid_events: List[BistEarningsEvent] = []
        invalid_events: List[Tuple[BistEarningsEvent, str]] = []

        for ev in events:
            is_valid, reason = self._validate_event_for_partition(ev)
            if is_valid:
                valid_events.append(ev)
            else:
                invalid_events.append((ev, reason or "Invalid partition event"))
                if reason and reason not in result_warnings:
                    result_warnings.append(reason)

        if not events:
            # Healthy empty incoming events -> do not delete old records, return status as incoming provider_status
            archive_status = provider_status
            logger.info("No incoming events to upsert; existing partition files preserved.")
            return BistPartitionUpsertResult(
                status=archive_status,
                warnings=result_warnings
            )

        if not valid_events:
            err_msg = "Incoming event list contained no valid partition events."
            logger.error(err_msg)
            if err_msg not in result_warnings:
                result_warnings.append(err_msg)
            return BistPartitionUpsertResult(
                status="failed",
                error=err_msg,
                warnings=result_warnings
            )

        # Determine effective archive_status
        if invalid_events or provider_status == "partial":
            archive_status = "partial"
        else:
            archive_status = "success"

        # Load all existing partitions
        existing_partitions = self.load_all_partitions()
        partitions_map: Dict[Tuple[int, FiscalPeriod], BistFiscalPeriodEarningsCalendar] = {
            (p.fiscal_year, p.fiscal_period): p for p in existing_partitions
        }

        # ----------------------------------------------------------------------
        # PROACTIVE CROSS-PARTITION & INTRA-PARTITION DUPLICATE RECONCILIATION
        # ----------------------------------------------------------------------
        # Identity key: (symbol, period_start, period_end)
        global_identity_events: Dict[Tuple[str, str, str], List[Tuple[Tuple[int, FiscalPeriod], BistEarningsEvent]]] = {}

        for key, p in partitions_map.items():
            for ev in p.events:
                if ev.period_start and ev.period_end:
                    ident = (ev.symbol, ev.period_start, ev.period_end)
                    global_identity_events.setdefault(ident, []).append((key, ev))

        working_partition_events: Dict[Tuple[int, FiscalPeriod], List[BistEarningsEvent]] = {
            key: list(p.events) for key, p in partitions_map.items()
        }

        modified_partitions: Dict[Tuple[int, FiscalPeriod], BistFiscalPeriodEarningsCalendar] = {}
        affected_filenames: Set[str] = set()

        for ident, occurrences in global_identity_events.items():
            if len(occurrences) > 1:
                # Find global winner by source precedence helper
                winner_item = occurrences[0]
                for candidate in occurrences[1:]:
                    if is_incoming_event_superior_or_newer(candidate[1], winner_item[1]):
                        winner_item = candidate

                winner_key, winner_ev = winner_item

                for occ_key, occ_ev in occurrences:
                    if occ_key != winner_key or occ_ev is not winner_ev:
                        old_events = working_partition_events[occ_key]
                        new_events = [e for e in old_events if e is not occ_ev]
                        if len(new_events) != len(old_events):
                            working_partition_events[occ_key] = new_events
                            old_fname = self._get_partition_filename(occ_key[1], occ_key[0])
                            affected_filenames.add(old_fname)
                            w = f"Removed cross-partition duplicate {ident[0]} ({ident[1]} to {ident[2]}) from {occ_key[1]}_{occ_key[0]}"
                            if w not in result_warnings:
                                result_warnings.append(w)

        # Current identity map of winner events after proactive cleanup
        identity_map: Dict[Tuple[str, str, str], Tuple[int, FiscalPeriod, BistEarningsEvent]] = {}
        for key, ev_list in working_partition_events.items():
            for ev in ev_list:
                if ev.period_start and ev.period_end:
                    ident = (ev.symbol, ev.period_start, ev.period_end)
                    identity_map[ident] = (key[0], key[1], ev)

        # ----------------------------------------------------------------------
        # PROCESS INCOMING VALID EVENTS
        # ----------------------------------------------------------------------
        valid_incoming_groups: Dict[Tuple[int, FiscalPeriod], List[BistEarningsEvent]] = {}
        for ev in valid_events:
            if ev.fiscal_year is not None and ev.fiscal_period is not None:
                key = (ev.fiscal_year, ev.fiscal_period)
                valid_incoming_groups.setdefault(key, []).append(ev)

        events_added_count = 0
        events_changed_count = 0
        events_unchanged_count = 0

        for (fy, fp), inc_list in valid_incoming_groups.items():
            current_events = working_partition_events.get((fy, fp), [])
            events_by_ident: Dict[Tuple[str, str, str], BistEarningsEvent] = {
                (e.symbol, e.period_start, e.period_end): e for e in current_events if e.period_start and e.period_end
            }

            for inc_ev in inc_list:
                if not inc_ev.period_start or not inc_ev.period_end:
                    continue
                ident = (inc_ev.symbol, inc_ev.period_start, inc_ev.period_end)

                # Check if this identity exists in another partition
                if ident in identity_map:
                    cur_fy, cur_fp, cur_ev = identity_map[ident]

                    if (cur_fy, cur_fp) != (fy, fp):
                        if is_incoming_event_superior_or_newer(inc_ev, cur_ev):
                            # Incoming is superior/newer -> remove losing event from cur_fp/cur_fy partition
                            cur_key = (cur_fy, cur_fp)
                            old_events = working_partition_events.get(cur_key, [])
                            new_events = [e for e in old_events if (e.symbol, e.period_start, e.period_end) != ident]
                            if len(new_events) != len(old_events):
                                working_partition_events[cur_key] = new_events
                                old_fname = self._get_partition_filename(cur_fp, cur_fy)
                                affected_filenames.add(old_fname)
                                w = f"Removed cross-partition duplicate {ident[0]} ({ident[1]} to {ident[2]}) from {cur_fp}_{cur_fy}"
                                if w not in result_warnings:
                                    result_warnings.append(w)
                        else:
                            # Existing event in cur_fp/cur_fy is superior/newer -> skip incoming
                            events_unchanged_count += 1
                            w = f"Skipped incoming event for {ident[0]} because superior provenance exists in {cur_fp}_{cur_fy}"
                            if w not in result_warnings:
                                result_warnings.append(w)
                            continue

                if ident in events_by_ident:
                    ex_ev = events_by_ident[ident]

                    if is_incoming_event_superior_or_newer(inc_ev, ex_ev):
                        if not events_are_equal(ex_ev, inc_ev):
                            events_changed_count += 1
                            events_by_ident[ident] = inc_ev
                        else:
                            events_unchanged_count += 1
                    else:
                        events_unchanged_count += 1
                else:
                    events_added_count += 1
                    events_by_ident[ident] = inc_ev

                identity_map[ident] = (fy, fp, events_by_ident[ident])

            working_partition_events[(fy, fp)] = list(events_by_ident.values())

        # Determine which partitions need saving and construct updated calendar models
        for (fy, fp), ev_list in working_partition_events.items():
            filepath = self._get_partition_path(fp, fy)
            file_existed = os.path.exists(filepath)
            filename = self._get_partition_filename(fp, fy)
            existing_cal = partitions_map.get((fy, fp))

            # Compare events list to existing events
            existing_events_list = existing_cal.events if existing_cal else []
            events_changed = False
            if not existing_cal or len(existing_events_list) != len(ev_list):
                events_changed = True
            else:
                for e1, e2 in zip(existing_events_list, ev_list, strict=False):
                    if not events_are_equal(e1, e2):
                        events_changed = True
                        break

            metadata_changed = False
            if (fy, fp) in valid_incoming_groups and existing_cal:
                if existing_cal.status != archive_status or existing_cal.provider_status != provider_status or existing_cal.warnings != result_warnings:
                    metadata_changed = True

            if not file_existed or events_changed or metadata_changed:
                sorted_events = list(ev_list)
                sorted_events.sort(key=lambda ev: (ev.earnings_date, ev.symbol, ev.period_start or "", ev.period_end or ""))

                # When metadata or events change, use new generated_at timestamp
                target_generated_at = generated_at if (not file_existed or events_changed or metadata_changed) else existing_cal.generated_at

                updated_cal = BistFiscalPeriodEarningsCalendar(
                    schema_version="2.1",
                    dataset_type="fiscal_period_partition",
                    generated_at=target_generated_at,
                    market="BIST100",
                    fiscal_year=fy,
                    fiscal_period=fp,
                    status=archive_status,
                    provider_status=provider_status,
                    events=sorted_events,
                    warnings=result_warnings
                )
                modified_partitions[(fy, fp)] = updated_cal
                affected_filenames.add(filename)

        # Atomic writes and counting
        files_created_count = 0
        files_updated_count = 0

        for (fy, fp), cal in modified_partitions.items():
            fname = self._get_partition_filename(fp, fy)
            fpath = self._get_partition_path(fp, fy)
            existed_before = os.path.exists(fpath)

            save_ok = self._atomic_save(cal)
            if not save_ok:
                err_msg = f"Atomic write failed for partition file {fname}"
                logger.error(err_msg)
                if err_msg not in result_warnings:
                    result_warnings.append(err_msg)
                return BistPartitionUpsertResult(
                    status="failed",
                    error=err_msg,
                    warnings=result_warnings
                )

            if not existed_before:
                files_created_count += 1
            else:
                files_updated_count += 1

        return BistPartitionUpsertResult(
            status=archive_status,
            files_created=files_created_count,
            files_updated=files_updated_count,
            events_added=events_added_count,
            events_changed=events_changed_count,
            events_unchanged=events_unchanged_count,
            affected_files=sorted(list(affected_filenames)),
            warnings=result_warnings
        )


bist_fiscal_period_archive = BistFiscalPeriodArchive()
