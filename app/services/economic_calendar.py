import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.economic_calendar import (
    EconomicCalendarStatus,
    EconomicProviderResult,
    EconomicProviderStatus,
    RollingEconomicCalendar,
)
from app.models.market import EconomicEvent
from app.providers.calendar import (
    BaseCalendarProvider,
    BeaCalendarProvider,
    BlsCalendarProvider,
    FedCalendarProvider,
    TcmbCalendarProvider,
    TuikCalendarProvider,
)
from app.providers.calendar.base import CalendarProviderFetchResult
from app.services.economic_event_identity import (
    canonical_istanbul_iso,
    economic_occurrence_key,
    economic_provider_key,
    economic_stable_identity,
)

logger = logging.getLogger(__name__)


def _validate_and_scope_provider_events(
    provider_id: str,
    provider_name: str,
    fetch_res: CalendarProviderFetchResult,
    cov_start: date,
    cov_end: date,
    tz_tr: ZoneInfo
) -> Tuple[List[EconomicEvent], EconomicProviderResult, List[str]]:
    valid_in_coverage: List[EconomicEvent] = []
    validation_warnings: List[str] = list(fetch_res.warnings)
    malformed_count = fetch_res.malformed_count

    for ev in fetch_res.events:
        ts = getattr(ev, "scheduled_at_istanbul", None)
        if not ts:
            msg = f"{provider_name} returned an invalid event timestamp for '{getattr(ev, 'name', 'Unknown')}': {ts}"
            validation_warnings.append(msg)
            malformed_count += 1
            continue

        try:
            dt_ev = datetime.fromisoformat(ts)
            if dt_ev.tzinfo is None:
                dt_ev = dt_ev.replace(tzinfo=tz_tr)
            else:
                dt_ev = dt_ev.astimezone(tz_tr)

            ev_date = dt_ev.date()
            if cov_start <= ev_date <= cov_end:
                valid_in_coverage.append(ev)
        except Exception:
            msg = f"{provider_name} returned an invalid event timestamp for '{getattr(ev, 'name', 'Unknown')}': {ts}"
            validation_warnings.append(msg)
            malformed_count += 1

    # Check for stable identity collisions within provider's valid events
    grouped_by_stable: Dict[Tuple[str, ...], List[EconomicEvent]] = {}
    for ev in valid_in_coverage:
        if s_key := economic_stable_identity(ev):
            grouped_by_stable.setdefault(s_key, []).append(ev)

    scoped_events: List[EconomicEvent] = []
    has_stable_conflict = False
    conflicted_stable_keys: Set[Tuple[str, ...]] = set()

    for s_key, group in grouped_by_stable.items():
        distinct_timestamps = {canonical_istanbul_iso(e.scheduled_at_istanbul) for e in group}
        if len(distinct_timestamps) > 1:
            has_stable_conflict = True
            conflicted_stable_keys.add(s_key)
            stable_id_str = s_key[2] if len(s_key) > 2 and str(s_key[2]).startswith(f"{s_key[1]}:") else ":".join(str(x) for x in s_key[1:])
            msg = f"{provider_name}: Conflicting events detected for stable identity '{stable_id_str}' with multiple timestamps: {sorted(list(distinct_timestamps))}; conflicting current events were excluded"
            validation_warnings.append(msg)

    # Rebuild events: completely EXCLUDE conflicting stable identity events
    seen_occurrences = set()
    for ev in valid_in_coverage:
        s_key = economic_stable_identity(ev)
        if s_key and s_key in conflicted_stable_keys:
            continue

        occ_key = economic_occurrence_key(ev)
        if occ_key not in seen_occurrences:
            seen_occurrences.add(occ_key)
            scoped_events.append(ev)

    seen = set()
    deduped_warnings = []
    for w in validation_warnings:
        w_s = w.strip()
        if w_s and w_s not in seen:
            seen.add(w_s)
            deduped_warnings.append(w_s)

    # Strict type-safe decision table without string guessing
    if not fetch_res.structure_validated:
        p_status: EconomicProviderStatus = "failed"
    elif len(scoped_events) > 0:
        if malformed_count > 0 or has_stable_conflict or len(deduped_warnings) > 0:
            p_status = "partial"
        else:
            p_status = "success"
    else:  # len(scoped_events) == 0
        if fetch_res.candidate_count == 0 and malformed_count == 0 and not has_stable_conflict and len(deduped_warnings) == 0:
            p_status = "success"
        else:
            p_status = "failed"

    pr_result = EconomicProviderResult(
        provider_id=provider_id,
        provider_name=provider_name,
        status=p_status,
        structure_validated=fetch_res.structure_validated,
        event_count=len(scoped_events),
        carried_forward_event_count=0,
        observed_candidate_count=fetch_res.observed_candidate_count,
        candidate_count=fetch_res.candidate_count,
        malformed_count=malformed_count,
        warnings=deduped_warnings,
        error=deduped_warnings[0] if (p_status == "failed" and deduped_warnings) else None
    )

    return scoped_events, pr_result, deduped_warnings


class EconomicCalendarService:
    def __init__(self, providers: Optional[List[BaseCalendarProvider]] = None):
        self.providers: List[BaseCalendarProvider] = providers or [
            TuikCalendarProvider(),
            TcmbCalendarProvider(),
            BlsCalendarProvider(),
            BeaCalendarProvider(),
            FedCalendarProvider()
        ]

    async def get_rolling_calendar(
        self,
        as_of: Optional[datetime] = None,
    ) -> RollingEconomicCalendar:
        """Fetch 91-day rolling economic calendar (as_of -> as_of + 90 days inclusive) in Europe/Istanbul timezone."""
        tz_tr = ZoneInfo(settings.TIMEZONE)
        if as_of is None:
            ref_dt = datetime.now(tz_tr)
        elif as_of.tzinfo is None:
            ref_dt = as_of.replace(tzinfo=tz_tr)
        else:
            ref_dt = as_of.astimezone(tz_tr)

        cov_start = ref_dt.date()
        cov_end = cov_start + timedelta(days=90)

        start_dt = datetime.combine(cov_start, time.min, tzinfo=tz_tr)
        end_dt = datetime.combine(cov_end, time.max, tzinfo=tz_tr)

        tasks = [p.fetch_events(start_dt, end_dt) for p in self.providers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        provider_results: List[EconomicProviderResult] = []
        all_events: List[EconomicEvent] = []
        combined_warnings: List[str] = []

        for provider, res in zip(self.providers, results, strict=False):
            p_id = getattr(provider, "provider_id", provider.__class__.__name__)
            p_name = getattr(provider, "provider_name", provider.__class__.__name__)

            if isinstance(res, Exception):
                err_msg = f"{p_name} unavailable: {str(res)}"
                logger.warning(err_msg)
                provider_results.append(EconomicProviderResult(
                    provider_id=p_id,
                    provider_name=p_name,
                    status="failed",
                    structure_validated=False,
                    event_count=0,
                    carried_forward_event_count=0,
                    observed_candidate_count=0,
                    candidate_count=0,
                    malformed_count=0,
                    warnings=[err_msg],
                    error=str(res)
                ))
                combined_warnings.append(err_msg)
            else:
                # res is a CalendarProviderFetchResult instance
                valid_events, pr_res, p_warnings = _validate_and_scope_provider_events(
                    p_id, p_name, res, cov_start, cov_end, tz_tr
                )
                provider_results.append(pr_res)
                all_events.extend(valid_events)
                combined_warnings.extend(p_warnings)

        # Unified global deduplication using economic_occurrence_key
        dedup_map: Dict[Tuple[Any, ...], EconomicEvent] = {}
        for ev in all_events:
            try:
                key = economic_occurrence_key(ev)
                if key not in dedup_map:
                    dedup_map[key] = ev
            except Exception:
                continue

        unique_events = list(dedup_map.values())
        unique_events.sort(key=lambda x: (
            x.scheduled_at_istanbul,
            x.country,
            x.event_code or x.name,
            economic_provider_key(x)
        ))

        # Re-sync provider event counts with final unique_events
        synced_provider_results: List[EconomicProviderResult] = []
        for pr in provider_results:
            final_count = sum(
                1 for e in unique_events
                if economic_provider_key(e) in (pr.provider_id, pr.provider_name)
            )
            synced_provider_results.append(pr.model_copy(update={"event_count": final_count}))
        provider_results = synced_provider_results

        # Overall Status (Strict Safety Decision Matrix: Rule A, B, C, D)
        if len(provider_results) == 0:
            agg_status: EconomicCalendarStatus = "success"
        else:
            all_success = all(pr.status == "success" and pr.structure_validated for pr in provider_results)
            if all_success:
                agg_status = "success"
            elif len(unique_events) > 0:
                agg_status = "partial"
            else:
                # At least one provider is partial/failed AND 0 total fresh events -> overall failed!
                agg_status = "failed"

        # Global exact deduplication of warnings
        seen_warns = set()
        deduped_combined_warnings = []
        for w in combined_warnings:
            w_s = w.strip()
            if w_s and w_s not in seen_warns:
                seen_warns.add(w_s)
                deduped_combined_warnings.append(w_s)

        return RollingEconomicCalendar(
            schema_version="2.0",
            generated_at=ref_dt.isoformat(),
            coverage_start=cov_start.isoformat(),
            coverage_end=cov_end.isoformat(),
            timezone="Europe/Istanbul",
            status=agg_status,
            provider_results=provider_results,
            events=unique_events,
            warnings=deduped_combined_warnings
        )

    async def get_events_between(
        self,
        start: datetime,
        end: datetime
    ) -> Tuple[List[EconomicEvent], List[str]]:
        """Fetch and return events between start and end using unified identity dedup."""
        tz_tr = ZoneInfo(settings.TIMEZONE)
        
        # Ensure start and end are timezone-aware in Europe/Istanbul
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz_tr)
        else:
            start = start.astimezone(tz_tr)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz_tr)
        else:
            end = end.astimezone(tz_tr)

        tasks = [p.fetch_events(start, end) for p in self.providers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_events: List[EconomicEvent] = []
        warnings: List[str] = []

        for provider, res in zip(self.providers, results, strict=False):
            provider_name = getattr(provider, "provider_name", provider.__class__.__name__)
            if isinstance(res, Exception):
                logger.warning(f"{provider_name} failed: {res}")
                warnings.append(f"{provider_name} unavailable: {str(res)}")
            elif isinstance(res, CalendarProviderFetchResult):
                all_events.extend(res.events)
                warnings.extend(res.warnings)
            elif isinstance(res, (tuple, list)) and len(res) == 2:
                # Backward compatibility for tuple returns
                all_events.extend(res[0])
                warnings.extend(res[1])

        filtered_events: List[EconomicEvent] = []
        for ev in all_events:
            try:
                dt_event = datetime.fromisoformat(ev.scheduled_at_istanbul).astimezone(tz_tr)
                if start <= dt_event <= end:
                    filtered_events.append(ev)
            except Exception as e:
                logger.debug(f"Error filtering event date: {e}")

        dedup_map: Dict[Tuple[Any, ...], EconomicEvent] = {}
        for ev in filtered_events:
            try:
                key = economic_occurrence_key(ev)
                if key not in dedup_map:
                    dedup_map[key] = ev
            except Exception:
                continue

        unique_events = list(dedup_map.values())
        unique_events.sort(key=lambda x: datetime.fromisoformat(x.scheduled_at_istanbul))

        return unique_events, warnings


economic_calendar_service = EconomicCalendarService()
