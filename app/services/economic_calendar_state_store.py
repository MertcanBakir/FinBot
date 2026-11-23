import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Protocol, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.economic_calendar import (
    EconomicCalendarStatus,
    EconomicProviderResult,
    RollingEconomicCalendar,
)
from app.models.market import EconomicEvent
from app.services.database_calendar_reader import database_calendar_reader
from app.services.economic_event_identity import (
    canonical_istanbul_iso,
    economic_occurrence_key,
    economic_provider_key,
    economic_stable_identity,
)

logger = logging.getLogger(__name__)


class EconomicCalendarStateStore(Protocol):
    state_source: str

    def load(self) -> Optional[RollingEconomicCalendar]: ...
    def stage(self, calendar: RollingEconomicCalendar) -> RollingEconomicCalendar: ...


def merge_partial_economic_calendar(
    previous: Optional[RollingEconomicCalendar],
    current: RollingEconomicCalendar
) -> RollingEconomicCalendar:
    """
    Merge a current rolling calendar result with the previous rolling calendar based on provider statuses:
    - Provider 'success': Authoritative, removes old provider events, uses new events (even if empty).
    - Provider 'partial': Retains new events, carries forward previous non-colliding events in coverage (stale=True, carried_forward=True).
    - Provider 'failed': Carries forward all previous events for that provider in coverage (stale=True, carried_forward=True).
    """
    cov_start = date.fromisoformat(current.coverage_start)
    cov_end = date.fromisoformat(current.coverage_end)
    tz_tr = ZoneInfo(current.timezone)

    final_events: List[EconomicEvent] = []
    updated_provider_results: List[EconomicProviderResult] = []

    # Map previous events by provider_key
    prev_events_by_provider: Dict[str, List[EconomicEvent]] = {}
    if previous is not None:
        for ev in previous.events:
            try:
                dt_ev = datetime.fromisoformat(ev.scheduled_at_istanbul)
                if dt_ev.tzinfo is None:
                    dt_ev = dt_ev.replace(tzinfo=tz_tr)
                else:
                    dt_ev = dt_ev.astimezone(tz_tr)
                
                # Discard previous events that naturally aged out of current coverage
                if cov_start <= dt_ev.date() <= cov_end:
                    p_key = economic_provider_key(ev)
                    prev_events_by_provider.setdefault(p_key, []).append(ev)
            except Exception:
                continue

    # Process each provider from current
    for pr in current.provider_results:
        p_key = pr.provider_id
        # Current events for this provider
        curr_p_events = [
            ev for ev in current.events
            if economic_provider_key(ev) in (p_key, pr.provider_name)
        ]

        carried_for_provider: List[EconomicEvent] = []

        if pr.status == "success":
            # Success is authoritative: use curr_p_events as is, do not carry forward old events
            final_events.extend(curr_p_events)
            updated_provider_results.append(pr.model_copy(update={
                "event_count": len(curr_p_events),
                "carried_forward_event_count": 0
            }))

        elif pr.status == "partial":
            # Keep current events
            final_events.extend(curr_p_events)

            # Build stable and occurrence keys for current events of this provider
            curr_stable_keys = {
                economic_stable_identity(ev) for ev in curr_p_events
                if economic_stable_identity(ev) is not None
            }
            curr_occurrence_keys = {
                economic_occurrence_key(ev) for ev in curr_p_events
            }

            # Carry forward previous events of this provider that don't collide
            prev_p_events = prev_events_by_provider.get(p_key, [])
            for prev_ev in prev_p_events:
                s_key = economic_stable_identity(prev_ev)
                occ_key = economic_occurrence_key(prev_ev)

                # Collision check
                if (s_key and s_key in curr_stable_keys) or (occ_key in curr_occurrence_keys):
                    continue

                carried_ev = prev_ev.model_copy(update={"carried_forward": True, "stale": True})
                carried_for_provider.append(carried_ev)

            final_events.extend(carried_for_provider)
            updated_provider_results.append(pr.model_copy(update={
                "event_count": len(curr_p_events),
                "carried_forward_event_count": len(carried_for_provider)
            }))

        elif pr.status == "failed":
            # Carry forward all previous events in coverage for this provider
            prev_p_events = prev_events_by_provider.get(p_key, [])
            for prev_ev in prev_p_events:
                carried_ev = prev_ev.model_copy(update={"carried_forward": True, "stale": True})
                carried_for_provider.append(carried_ev)

            final_events.extend(carried_for_provider)
            updated_provider_results.append(pr.model_copy(update={
                "event_count": 0,
                "carried_forward_event_count": len(carried_for_provider)
            }))

    # Defense-in-depth: Deduplicate final events and resolve conflicting timestamps strictly
    final_stable_groups: Dict[Tuple[str, ...], List[EconomicEvent]] = {}
    final_non_stable: List[EconomicEvent] = []
    defense_warnings_by_provider: Dict[str, List[str]] = {}

    for ev in final_events:
        s_key = economic_stable_identity(ev)
        if s_key:
            final_stable_groups.setdefault(s_key, []).append(ev)
        else:
            final_non_stable.append(ev)

    filtered_final_events: List[EconomicEvent] = list(final_non_stable)
    for s_key, group in final_stable_groups.items():
        distinct_ts = {canonical_istanbul_iso(e.scheduled_at_istanbul) for e in group}
        p_id = s_key[1]
        stable_id_str = s_key[2] if len(s_key) > 2 and str(s_key[2]).startswith(f"{s_key[1]}:") else ":".join(str(x) for x in s_key[1:])

        if len(distinct_ts) == 1:
            # 1. All have same timestamp: pick non-carried if available (exact deduplication)
            picked = next((e for e in group if not e.carried_forward), group[0])
            filtered_final_events.append(picked)
        else:
            fresh_events = [e for e in group if not e.carried_forward]
            carried_events = [e for e in group if e.carried_forward]

            if len(fresh_events) == 1:
                # 2. Exactly 1 fresh + 1 or more carried with different timestamps: fresh is authoritative
                filtered_final_events.append(fresh_events[0])
            elif len(fresh_events) > 1:
                # 3. Multiple fresh with different timestamps: conflict! Exclude stable identity entirely
                warn_msg = f"{p_id}: Conflicting fresh events for stable identity '{stable_id_str}' with multiple timestamps; identity excluded from final state"
                defense_warnings_by_provider.setdefault(p_id, []).append(warn_msg)
            else:
                # 4. Zero fresh + multiple carried with different timestamps: ambiguous! Exclude stable identity entirely
                warn_msg = f"{p_id}: Ambiguous carried-forward events for stable identity '{stable_id_str}' with multiple timestamps; identity excluded from final state"
                defense_warnings_by_provider.setdefault(p_id, []).append(warn_msg)

    # Deduplicate final events by occurrence key
    dedup_map: Dict[Tuple[Any, ...], EconomicEvent] = {}
    for ev in filtered_final_events:
        key = economic_occurrence_key(ev)
        if key not in dedup_map:
            dedup_map[key] = ev
        else:
            existing = dedup_map[key]
            if existing.carried_forward and not ev.carried_forward:
                dedup_map[key] = ev

    sorted_events = list(dedup_map.values())
    sorted_events.sort(key=lambda x: (
        x.scheduled_at_istanbul,
        x.country,
        x.event_code or x.name,
        economic_provider_key(x)
    ))

    # Update provider result counts and statuses after defense exclusions
    final_provider_results: List[EconomicProviderResult] = []
    for pr in updated_provider_results:
        p_id = pr.provider_id
        final_fresh = sum(
            1 for e in sorted_events
            if economic_provider_key(e) in (p_id, pr.provider_name) and not e.carried_forward
        )
        final_carried = sum(
            1 for e in sorted_events
            if economic_provider_key(e) in (p_id, pr.provider_name) and e.carried_forward
        )

        new_status = pr.status
        pr_warns = list(pr.warnings)
        if p_id in defense_warnings_by_provider:
            for w in defense_warnings_by_provider[p_id]:
                if w not in pr_warns:
                    pr_warns.append(w)
            if new_status == "success":
                new_status = "partial"

        final_provider_results.append(pr.model_copy(update={
            "status": new_status,
            "event_count": final_fresh,
            "carried_forward_event_count": final_carried,
            "warnings": pr_warns
        }))

    # Overall Status calculation (Strict Safety Decision Matrix)
    if len(final_provider_results) == 0:
        overall_status: EconomicCalendarStatus = "success"
    else:
        all_success = all(pr.status == "success" and pr.structure_validated for pr in final_provider_results)
        if all_success:
            overall_status = "success"
        elif len(sorted_events) > 0:
            overall_status = "partial"
        else:
            overall_status = "failed"

    # Merge warnings
    combined_warnings: List[str] = list(current.warnings)
    for p_warns in defense_warnings_by_provider.values():
        for w in p_warns:
            if w not in combined_warnings:
                combined_warnings.append(w)

    if any(pr.carried_forward_event_count > 0 for pr in final_provider_results):
        c_msg = "Bir veya daha fazla sağlayıcı için önceki takvimden taşınan (carried-forward) kayıtlar kullanıldı."
        if c_msg not in combined_warnings:
            combined_warnings.append(c_msg)

    seen_warns = set()
    deduped_warnings = []
    for w in combined_warnings:
        w_s = w.strip()
        if w_s and w_s not in seen_warns:
            seen_warns.add(w_s)
            deduped_warnings.append(w_s)

    return RollingEconomicCalendar(
        schema_version="2.0",
        generated_at=current.generated_at,
        coverage_start=current.coverage_start,
        coverage_end=current.coverage_end,
        timezone="Europe/Istanbul",
        status=overall_status,
        provider_results=final_provider_results,
        events=sorted_events,
        warnings=deduped_warnings
    )


class SupabaseEconomicCalendarStateStore:
    """Read persisted state from Supabase and stage refresh results in memory."""

    state_source = "supabase://economic_events"

    def __init__(self) -> None:
        self._pending: Optional[RollingEconomicCalendar] = None

    def load(self) -> Optional[RollingEconomicCalendar]:
        return self._pending or database_calendar_reader.load_economic()

    def stage(self, calendar: RollingEconomicCalendar) -> RollingEconomicCalendar:
        self._pending = calendar.model_copy(deep=True)
        return self._pending

    def is_stale(self, as_of: Optional[datetime] = None, max_age_hours: float = 36.0) -> bool:
        calendar = self.load()
        if not calendar:
            return True

        tz_tr = ZoneInfo(calendar.timezone or settings.TIMEZONE)
        now = as_of or datetime.now(tz_tr)
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz_tr)
        else:
            now = now.astimezone(tz_tr)

        try:
            gen_dt = datetime.fromisoformat(calendar.generated_at)
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=tz_tr)
            else:
                gen_dt = gen_dt.astimezone(tz_tr)

            age = (now - gen_dt).total_seconds() / 3600.0
            return age > max_age_hours
        except Exception as e:
            logger.warning(f"Could not parse generated_at in calendar state: {e}")
            return True

    def events_between(
        self,
        start_date: date,
        end_date: date,
        as_of: Optional[date] = None
    ) -> Tuple[List[EconomicEvent], List[str], Optional[RollingEconomicCalendar]]:
        """
        Query stored events scheduled between start_date and end_date (inclusive).
        Calculates dynamic days_remaining relative to as_of (default today in Europe/Istanbul).
        """
        calendar = self.load()
        if not calendar:
            return [], ["Rolling economic calendar state is unavailable."], None

        tz_tr = ZoneInfo(calendar.timezone or settings.TIMEZONE)
        today = as_of or datetime.now(tz_tr).date()

        filtered: List[EconomicEvent] = []
        for ev in calendar.events:
            try:
                dt_ev = datetime.fromisoformat(ev.scheduled_at_istanbul)
                if dt_ev.tzinfo is None:
                    dt_ev = dt_ev.replace(tzinfo=tz_tr)
                else:
                    dt_ev = dt_ev.astimezone(tz_tr)

                ev_date = dt_ev.date()
                if start_date <= ev_date <= end_date:
                    ev_copy = ev.model_copy()
                    ev_copy.days_remaining = (ev_date - today).days
                    filtered.append(ev_copy)
            except Exception as e:
                logger.debug(f"Error parsing event date for query: {e}")

        filtered.sort(key=lambda x: (
            x.scheduled_at_istanbul,
            x.country,
            x.event_code or x.name,
            x.provider_id or x.source
        ))

        return filtered, list(calendar.warnings), calendar

economic_calendar_state_store = SupabaseEconomicCalendarStateStore()
