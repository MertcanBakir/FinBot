import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from app.models.economic_calendar import RollingEconomicCalendar
from app.services.economic_event_identity import (
    canonical_istanbul_iso,
    economic_occurrence_key,
    economic_provider_key,
    economic_stable_identity,
)

logger = logging.getLogger(__name__)

EXPECTED_PROVIDERS: Set[str] = {"TUIK", "TCMB", "BLS", "BEA", "FED"}


def validate_economic_calendar_state_source(
    file_path: Path,
    require_current_coverage: bool = False,
    as_of: Optional[date] = None
) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    # 1. File existence check
    if not file_path.exists():
        errors.append(f"Cache file does not exist: {file_path}")
        return False, errors

    # 2. JSON parsing
    try:
        raw_text = file_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception as e:
        errors.append(f"Invalid JSON content: {e}")
        return False, errors

    # 3. Pydantic schema validation
    try:
        cal = RollingEconomicCalendar.model_validate(data)
    except Exception as e:
        errors.append(f"Pydantic schema validation failed: {e}")
        return False, errors

    # 4. Schema version
    if cal.schema_version != "2.0":
        errors.append(f"Invalid schema_version '{cal.schema_version}', expected '2.0'.")

    # 5. Status cannot be failed
    if cal.status == "failed":
        errors.append("Calendar status cannot be 'failed' in persisted cache.")

    # 6. Status partial with 0 events is invalid
    if cal.status == "partial" and len(cal.events) == 0:
        errors.append("Calendar status 'partial' with 0 events is prohibited in persisted cache.")

    # 7. Provider set exact match (no missing, no extra, no duplicate)
    provider_ids = [pr.provider_id for pr in cal.provider_results]
    provider_id_set = set(provider_ids)

    if len(provider_ids) != len(provider_id_set):
        errors.append(f"Duplicate provider entries found in provider_results: {provider_ids}")

    if provider_id_set != EXPECTED_PROVIDERS:
        missing = EXPECTED_PROVIDERS - provider_id_set
        extra = provider_id_set - EXPECTED_PROVIDERS
        if missing:
            errors.append(f"Missing required providers: {missing}")
        if extra:
            errors.append(f"Unexpected extra providers: {extra}")

    # 8. Provider structure_validated check for success and partial
    for pr in cal.provider_results:
        if pr.status in ("success", "partial") and not pr.structure_validated:
            errors.append(f"Provider {pr.provider_id} has status '{pr.status}' but structure_validated is False.")

    # 9. Coverage window calculation (must be exactly 90 days delta = 91 days inclusive)
    try:
        cov_start_d = date.fromisoformat(cal.coverage_start)
        cov_end_d = date.fromisoformat(cal.coverage_end)
        expected_cov_end = cov_start_d + timedelta(days=90)
        if cov_end_d != expected_cov_end:
            errors.append(f"coverage_end '{cal.coverage_end}' is not coverage_start + 90 days ('{expected_cov_end.isoformat()}').")
    except Exception as e:
        errors.append(f"Invalid coverage date formats: {e}")
        return False, errors

    # 10. generated_at Istanbul date matches coverage_start
    try:
        tz_tr = ZoneInfo(cal.timezone or "Europe/Istanbul")
        gen_dt = datetime.fromisoformat(cal.generated_at)
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=tz_tr)
        else:
            gen_dt = gen_dt.astimezone(tz_tr)
        gen_date = gen_dt.date()
        if gen_date != cov_start_d:
            errors.append(f"generated_at Istanbul date '{gen_date}' does not match coverage_start '{cov_start_d}'.")
        if require_current_coverage:
            target_date = as_of or datetime.now(tz_tr).date()
            if cov_start_d != target_date:
                errors.append(f"coverage_start '{cov_start_d}' does not match current date '{target_date}'.")
    except Exception as e:
        errors.append(f"Invalid generated_at timestamp '{cal.generated_at}': {e}")

    # 11 & 12. Event timestamp bounds and timezone validation
    exact_seen: Set[Tuple[Any, ...]] = set()
    stable_map: Dict[Tuple[str, ...], Set[str]] = {}

    for ev in cal.events:
        ts_str = ev.scheduled_at_istanbul
        if not ts_str:
            errors.append(f"Event '{ev.name}' is missing scheduled_at_istanbul.")
            continue

        try:
            ev_dt = datetime.fromisoformat(ts_str)
            if ev_dt.tzinfo is None:
                errors.append(f"Event '{ev.name}' timestamp '{ts_str}' is missing timezone offset.")
                continue
            ev_d = ev_dt.astimezone(tz_tr).date()
            if ev_d < cov_start_d or ev_d > cov_end_d:
                errors.append(f"Event '{ev.name}' timestamp date '{ev_d}' is outside coverage window [{cov_start_d} .. {cov_end_d}].")
        except Exception as e:
            errors.append(f"Event '{ev.name}' timestamp '{ts_str}' failed parsing: {e}")
            continue

        # 13. Exact duplicate occurrence check
        occ_key = economic_occurrence_key(ev)
        if occ_key in exact_seen:
            errors.append(f"Duplicate exact occurrence found in calendar: {occ_key}")
        exact_seen.add(occ_key)

        # 14. Stable identity conflicting timestamps
        if s_key := economic_stable_identity(ev):
            stable_map.setdefault(s_key, set()).add(canonical_istanbul_iso(ev.scheduled_at_istanbul))

    for s_key, ts_set in stable_map.items():
        if len(ts_set) > 1:
            stable_str = ":".join(str(x) for x in s_key)
            errors.append(f"Conflicting timestamps found for stable identity '{stable_str}': {sorted(list(ts_set))}")

    # 15 & 16. Provider counts consistency with event list
    total_reported_events = 0
    for pr in cal.provider_results:
        p_id = pr.provider_id
        matching_events = [
            e for e in cal.events
            if economic_provider_key(e) in (p_id, pr.provider_name)
        ]
        reported_pr_count = pr.event_count + pr.carried_forward_event_count
        total_reported_events += reported_pr_count

        if len(matching_events) != reported_pr_count:
            errors.append(
                f"Provider {p_id} count mismatch: {len(matching_events)} events in calendar vs {reported_pr_count} reported (fresh={pr.event_count}, carried={pr.carried_forward_event_count})."
            )

    if len(cal.events) != total_reported_events:
        errors.append(f"Total calendar event count ({len(cal.events)}) does not match sum of provider reported counts ({total_reported_events}).")

    return len(errors) == 0, errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FinBot local economic-calendar state fixture validator")
    parser.add_argument("--file", type=str, required=True, help="Path to a local JSON state fixture")
    parser.add_argument("--require-current-coverage", action="store_true", help="Require coverage_start to match current date in Europe/Istanbul")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    path = Path(args.file)
    is_valid, validation_errors = validate_economic_calendar_state_source(
        path,
        require_current_coverage=args.require_current_coverage
    )

    if not is_valid:
        print(f"STATE VALIDATION FAILED for {path}:", file=sys.stderr)
        for err in validation_errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"STATE VALIDATION PASSED for {path}.")
        sys.exit(0)
