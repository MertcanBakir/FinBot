import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_connection
from app.core.job_names import JOB_NAME_ECONOMIC_CALENDAR_REFRESH
from app.models.economic_calendar import (
    EconomicProviderResult,
    RollingEconomicCalendar,
)
from app.models.economic_calendar_refresh import (
    EconomicCalendarRefreshJobResult,
    EconomicCalendarRefreshStatus,
    EconomicEventChangeDetail,
    EconomicEventDiff,
    EconomicProviderRefreshOutcome,
    EconomicSyncAction,
)
from app.models.job_tracking import (
    JobExecutionContext,
    JobRunSpecification,
)
from app.models.market import EconomicEvent
from app.services.database_refresh_sync import sync_economic_refresh_to_database
from app.services.economic_calendar import (
    EconomicCalendarService,
    economic_calendar_service,
)
from app.services.economic_calendar_state_store import (
    EconomicCalendarStateStore,
    economic_calendar_state_store,
    merge_partial_economic_calendar,
)
from app.services.economic_event_identity import (
    canonical_istanbul_iso,
    economic_occurrence_key,
    economic_provider_key,
    economic_stable_identity,
)
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.economic_calendar_adapter import (
    EconomicCalendarRefreshResultAdapter,
)
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import (
    EconomicCalendarEmailRenderer,
)
from app.services.job_tracking.runner import TrackedJobRunner

logger = logging.getLogger(__name__)

STATE_WRITE_FAILED_WARNING = (
    "Rolling economic calendar state save failed to write to disk; "
    "no new calendar data was persisted."
)


def calculate_economic_diff(
    previous: Optional[RollingEconomicCalendar],
    fetched: RollingEconomicCalendar,
    final: RollingEconomicCalendar
) -> EconomicEventDiff:
    prev_events = previous.events if previous else []
    final_events = final.events

    diff = EconomicEventDiff(
        previous_event_count=len(prev_events),
        fetched_event_count=len(fetched.events),
        final_event_count=len(final_events)
    )

    if not previous:
        for ev in final_events:
            diff.added_count += 1
            diff.details.append(EconomicEventChangeDetail(
                name=ev.name,
                country=ev.country,
                change_type="added",
                provider_id=ev.provider_id,
                event_code=ev.event_code,
                reference_period=ev.reference_period,
                source_event_id=ev.source_event_id,
                new_scheduled_at_istanbul=ev.scheduled_at_istanbul
            ))
        return diff

    cov_start = date.fromisoformat(final.coverage_start)
    cov_end = date.fromisoformat(final.coverage_end)
    tz_tr = ZoneInfo(final.timezone)

    # 1. Classify previous events into aged_out vs in_coverage
    prev_in_coverage: List[EconomicEvent] = []
    for ev in prev_events:
        try:
            dt_ev = datetime.fromisoformat(ev.scheduled_at_istanbul)
            if dt_ev.tzinfo is None:
                dt_ev = dt_ev.replace(tzinfo=tz_tr)
            else:
                dt_ev = dt_ev.astimezone(tz_tr)
            
            if ev_date := dt_ev.date():
                if ev_date < cov_start or ev_date > cov_end:
                    diff.aged_out_count += 1
                else:
                    prev_in_coverage.append(ev)
        except Exception:
            continue

    # Build maps for previous in coverage with duplicate stable identity conflict protection
    prev_stable_groups: Dict[Tuple[str, ...], List[EconomicEvent]] = {}
    prev_exact_map: Dict[Tuple[Any, ...], EconomicEvent] = {}
    for ev in prev_in_coverage:
        if s_key := economic_stable_identity(ev):
            prev_stable_groups.setdefault(s_key, []).append(ev)
        prev_exact_map[economic_occurrence_key(ev)] = ev

    prev_stable_map: Dict[Tuple[str, ...], EconomicEvent] = {}
    for s_key, group in prev_stable_groups.items():
        distinct_ts = {canonical_istanbul_iso(e.scheduled_at_istanbul) for e in group}
        if len(distinct_ts) == 1:
            prev_stable_map[s_key] = group[0]

    # Track matched previous events
    matched_prev_exact: Set[Tuple[Any, ...]] = set()
    matched_prev_stable: Set[Tuple[str, ...]] = set()

    # Process final events
    for ev in final_events:
        occ_key = economic_occurrence_key(ev)
        s_key = economic_stable_identity(ev)

        # 1. Check if carried forward from previous state_store
        if ev.carried_forward:
            diff.carried_forward_count += 1
            diff.details.append(EconomicEventChangeDetail(
                name=ev.name,
                country=ev.country,
                change_type="carried_forward",
                provider_id=ev.provider_id,
                event_code=ev.event_code,
                reference_period=ev.reference_period,
                source_event_id=ev.source_event_id,
                new_scheduled_at_istanbul=ev.scheduled_at_istanbul
            ))
            # Mark previous counterpart as matched so it won't be marked as removed
            if s_key and s_key in prev_stable_map:
                prev_ev = prev_stable_map[s_key]
                matched_prev_stable.add(s_key)
                matched_prev_exact.add(economic_occurrence_key(prev_ev))
            elif occ_key in prev_exact_map:
                matched_prev_exact.add(occ_key)
            continue

        # 2. Fresh events: Check stable identity match
        if s_key and s_key in prev_stable_map:
            prev_ev = prev_stable_map[s_key]
            matched_prev_stable.add(s_key)
            matched_prev_exact.add(economic_occurrence_key(prev_ev))

            if canonical_istanbul_iso(prev_ev.scheduled_at_istanbul) != canonical_istanbul_iso(ev.scheduled_at_istanbul):
                diff.changed_count += 1
                diff.details.append(EconomicEventChangeDetail(
                    name=ev.name,
                    country=ev.country,
                    change_type="changed",
                    provider_id=ev.provider_id,
                    event_code=ev.event_code,
                    reference_period=ev.reference_period,
                    source_event_id=ev.source_event_id or prev_ev.source_event_id,
                    old_scheduled_at_istanbul=prev_ev.scheduled_at_istanbul,
                    new_scheduled_at_istanbul=ev.scheduled_at_istanbul
                ))
            else:
                diff.unchanged_count += 1

        elif occ_key in prev_exact_map:
            matched_prev_exact.add(occ_key)
            diff.unchanged_count += 1

        else:
            diff.added_count += 1
            diff.details.append(EconomicEventChangeDetail(
                name=ev.name,
                country=ev.country,
                change_type="added",
                provider_id=ev.provider_id,
                event_code=ev.event_code,
                reference_period=ev.reference_period,
                source_event_id=ev.source_event_id,
                new_scheduled_at_istanbul=ev.scheduled_at_istanbul
            ))

    # Process remaining previous events that were removed
    for ev in prev_in_coverage:
        occ_key = economic_occurrence_key(ev)
        s_key = economic_stable_identity(ev)

        if occ_key not in matched_prev_exact and (not s_key or s_key not in matched_prev_stable):
            diff.removed_count += 1
            diff.details.append(EconomicEventChangeDetail(
                name=ev.name,
                country=ev.country,
                change_type="removed",
                provider_id=ev.provider_id,
                event_code=ev.event_code,
                reference_period=ev.reference_period,
                source_event_id=ev.source_event_id,
                old_scheduled_at_istanbul=ev.scheduled_at_istanbul
            ))

    return diff


class EconomicCalendarRefreshJob:
    def __init__(
        self,
        service: Optional[EconomicCalendarService] = None,
        state_store: Optional[EconomicCalendarStateStore] = None,
        email_service: Optional[EmailService] = None,
        dry_run: bool = False,
        no_email: bool = False,
        db_connection_factory: Optional[Any] = None,
    ):
        self.service = service or economic_calendar_service
        self.state_store = state_store or economic_calendar_state_store
        self.email_service = email_service or EmailService()
        self.dry_run = dry_run
        self.no_email = no_email
        self.db_connection_factory = db_connection_factory or get_db_connection

    async def _execute_core(
        self,
        context: JobExecutionContext,
        as_of: Optional[datetime] = None,
    ) -> EconomicCalendarRefreshJobResult:
        tz_tr = ZoneInfo(settings.TIMEZONE)
        if as_of is None:
            ref_dt = datetime.now(tz_tr)
        elif as_of.tzinfo is None:
            ref_dt = as_of.replace(tzinfo=tz_tr)
        else:
            ref_dt = as_of.astimezone(tz_tr)

        logger.info(f"Starting EconomicCalendarRefreshJob as_of={ref_dt.isoformat()} (dry_run={self.dry_run}, no_email={self.no_email})")

        # 1. Load previous state_store first
        previous_calendar = self.state_store.load()

        # 2. Fetch current rolling calendar with service exception isolation
        cov_start_d = ref_dt.date()
        cov_end_d = cov_start_d + timedelta(days=90)

        try:
            current_calendar = await self.service.get_rolling_calendar(as_of=ref_dt)
        except Exception as exc:
            logger.error("Economic calendar service threw unexpected exception (error_type=%s)", type(exc).__name__)
            error_message = f"Economic calendar service exception: {type(exc).__name__}"

            expected_p_list = []
            if self.service and hasattr(self.service, "providers") and self.service.providers:
                for p in self.service.providers:
                    pid = getattr(p, "provider_id", p.__class__.__name__)
                    pname = getattr(p, "provider_name", p.__class__.__name__)
                    expected_p_list.append((pid, pname))
            else:
                expected_p_list = [
                    ("TUIK", "TÜİK Veri Takvimi"),
                    ("TCMB", "TCMB Takvimi"),
                    ("BLS", "U.S. Bureau of Labor Statistics Release Calendar"),
                    ("BEA", "U.S. Bureau of Economic Analysis"),
                    ("FED", "Federal Reserve FOMC Calendar")
                ]

            fallback_provider_results = [
                EconomicProviderResult(
                    provider_id=pid,
                    provider_name=pname,
                    status="failed",
                    structure_validated=False,
                    event_count=0,
                    carried_forward_event_count=0,
                    observed_candidate_count=0,
                    candidate_count=0,
                    malformed_count=0,
                    warnings=[error_message],
                    error=error_message
                )
                for pid, pname in expected_p_list
            ]

            current_calendar = RollingEconomicCalendar(
                schema_version="2.0",
                generated_at=ref_dt.isoformat(),
                coverage_start=cov_start_d.isoformat(),
                coverage_end=cov_end_d.isoformat(),
                timezone="Europe/Istanbul",
                status="failed",
                provider_results=fallback_provider_results,
                events=[],
                warnings=[error_message]
            )

        logger.info(f"Current fetch status: '{current_calendar.status}', total events: {len(current_calendar.events)}")

        # 3. Decide synchronization action and target final calendar
        final_calendar: Optional[RollingEconomicCalendar] = None
        sync_action: EconomicSyncAction = "not_written"
        job_status: EconomicCalendarRefreshStatus = current_calendar.status
        merged_calendar: Optional[RollingEconomicCalendar] = None

        if self.dry_run:
            sync_action = "dry_run"
            if current_calendar.status == "partial" and previous_calendar:
                merged_calendar = merge_partial_economic_calendar(previous_calendar, current_calendar)
                final_calendar = merged_calendar
            else:
                final_calendar = current_calendar

        elif current_calendar.status == "success":
            # Success replaces previous completely (healthy empty supported)
            save_ok = self.state_store.save(current_calendar)
            if save_ok:
                sync_action = "replaced"
                final_calendar = current_calendar
            else:
                logger.error(STATE_WRITE_FAILED_WARNING)
                if STATE_WRITE_FAILED_WARNING not in current_calendar.warnings:
                    current_calendar.warnings.append(STATE_WRITE_FAILED_WARNING)
                sync_action = "write_failed"
                job_status = "failed"
                final_calendar = previous_calendar if previous_calendar else current_calendar

        elif current_calendar.status == "partial":
            if previous_calendar is not None:
                # Merge fresh partial with previous reliable state_store
                merged_calendar = merge_partial_economic_calendar(previous_calendar, current_calendar)
                if merged_calendar.status != "failed" and len(merged_calendar.events) > 0:
                    save_ok = self.state_store.save(merged_calendar)
                    if save_ok:
                        sync_action = "merged_and_updated"
                        final_calendar = merged_calendar
                    else:
                        logger.error(STATE_WRITE_FAILED_WARNING)
                        if STATE_WRITE_FAILED_WARNING not in current_calendar.warnings:
                            current_calendar.warnings.append(STATE_WRITE_FAILED_WARNING)
                        sync_action = "write_failed"
                        job_status = "failed"
                        final_calendar = previous_calendar
                else:
                    # Failed or empty merged calendar: DO NOT CALL state_store.save!
                    logger.warning("Partial economic calendar merge produced no usable calendar; previous state_store was preserved.")
                    unusable_warn = "Partial economic calendar merge produced no usable calendar; previous state_store was preserved."
                    if unusable_warn not in current_calendar.warnings:
                        current_calendar.warnings.append(unusable_warn)
                    sync_action = "preserved_previous"
                    job_status = "failed"
                    final_calendar = previous_calendar
            else:
                # No previous state_store exists
                if len(current_calendar.events) > 0:
                    save_ok = self.state_store.save(current_calendar)
                    if save_ok:
                        sync_action = "initial_partial_written"
                        final_calendar = current_calendar
                    else:
                        logger.error(STATE_WRITE_FAILED_WARNING)
                        if STATE_WRITE_FAILED_WARNING not in current_calendar.warnings:
                            current_calendar.warnings.append(STATE_WRITE_FAILED_WARNING)
                        sync_action = "write_failed"
                        job_status = "failed"
                        final_calendar = current_calendar
                else:
                    # RULE C: Partial status with 0 events and no previous state_store -> Do not write corrupt empty file!
                    logger.error("RULE C TRIGGERED: Partial status with 0 events and no previous state_store. File will NOT be written. Job status set to 'failed'.")
                    sync_action = "not_written"
                    job_status = "failed"
                    final_calendar = current_calendar

        else:  # current_calendar.status == "failed"
            if previous_calendar is not None:
                sync_action = "preserved_previous"
                final_calendar = previous_calendar
            else:
                sync_action = "not_written"
                final_calendar = current_calendar

        # 4. Handle diff calculation
        if sync_action == "preserved_previous" and previous_calendar:
            carried_reporting_events = []
            for pev in previous_calendar.events:
                try:
                    dt_p = datetime.fromisoformat(pev.scheduled_at_istanbul).astimezone(tz_tr).date()
                    if cov_start_d <= dt_p <= cov_end_d:
                        carried_reporting_events.append(pev.model_copy(update={"carried_forward": True, "stale": True}))
                except Exception:
                    pass
            reporting_final = previous_calendar.model_copy(update={
                "events": carried_reporting_events,
                "coverage_start": cov_start_d.isoformat(),
                "coverage_end": cov_end_d.isoformat()
            })
            diff = calculate_economic_diff(previous_calendar, current_calendar, reporting_final)

        elif sync_action == "write_failed" and not previous_calendar:
            reporting_final = current_calendar.model_copy(update={"events": []})
            diff = calculate_economic_diff(None, current_calendar, reporting_final)

        else:
            diff = calculate_economic_diff(previous_calendar, current_calendar, final_calendar)

        # 5. Build provider outcomes based on current run and final reporting view
        merged_pr_map = {pr.provider_id: pr for pr in merged_calendar.provider_results} if merged_calendar else {}

        carried_count_by_provider: Dict[str, int] = {}
        if sync_action == "preserved_previous" and previous_calendar:
            for pev in previous_calendar.events:
                try:
                    dt_p = datetime.fromisoformat(pev.scheduled_at_istanbul).astimezone(tz_tr).date()
                    if cov_start_d <= dt_p <= cov_end_d:
                        p_k = economic_provider_key(pev)
                        carried_count_by_provider[p_k] = carried_count_by_provider.get(p_k, 0) + 1
                except Exception:
                    pass

        provider_outcomes: List[EconomicProviderRefreshOutcome] = []
        for pr in current_calendar.provider_results:
            p_id = pr.provider_id
            if sync_action == "preserved_previous":
                c_count = carried_count_by_provider.get(p_id, 0)
            elif sync_action == "merged_and_updated" and p_id in merged_pr_map:
                c_count = merged_pr_map[p_id].carried_forward_event_count
            else:
                c_count = pr.carried_forward_event_count

            provider_outcomes.append(EconomicProviderRefreshOutcome(
                provider_id=pr.provider_id,
                provider_name=pr.provider_name,
                status=pr.status,
                structure_validated=pr.structure_validated,
                event_count=pr.event_count,
                carried_forward_count=c_count,
                observed_candidate_count=pr.observed_candidate_count,
                candidate_count=pr.candidate_count,
                malformed_count=pr.malformed_count,
                warnings=pr.warnings,
                error=pr.error
            ))

        result = EconomicCalendarRefreshJobResult(
            schema_version="2.0",
            generated_at=ref_dt.isoformat(),
            coverage_start=ref_dt.date().isoformat(),
            coverage_end=(ref_dt.date() + timedelta(days=90)).isoformat(),
            status=job_status,
            sync_action=sync_action,
            state_source=self.state_store.file_path,
            diff=diff,
            provider_outcomes=provider_outcomes,
            warnings=list(current_calendar.warnings)
        )

        # 6. Database Parallel Synchronization (using existing refresh_run_id from TrackedJobRunner)
        try:
            db_status, db_error = await asyncio.to_thread(
                sync_economic_refresh_to_database,
                result=result,
                final_calendar=final_calendar,
                refresh_run_id=context.run_id,
                dry_run=self.dry_run,
            )
            result.database_status = db_status
            result.database_error = db_error
            if db_status == "failed":
                logger.error(f"Database synchronization failed: {db_error}")
            elif db_status == "success":
                logger.info("Database synchronization completed successfully.")
        except AssertionError:
            raise
        except Exception as db_err:
            logger.error(f"Unexpected error during database sync: {type(db_err).__name__}")
            result.database_status = "failed"
            result.database_error = f"{type(db_err).__name__}: database synchronization error"

        return result

    async def run(self, as_of: Optional[datetime] = None) -> EconomicCalendarRefreshJobResult:
        runner = TrackedJobRunner(
            spec=JobRunSpecification(
                job_name=JOB_NAME_ECONOMIC_CALENDAR_REFRESH,
                operation_type="calendar_refresh",
                scope="ECONOMIC",
                provider_code=None,
            ),
            adapter=EconomicCalendarRefreshResultAdapter(),
            renderer=EconomicCalendarEmailRenderer(),
            notification_rule=JobNotificationRule.ALWAYS_ON_FINISH,
            email_service=self.email_service,
            db_connection_factory=self.db_connection_factory,
        )

        result = await runner.run_async(
            lambda ctx: self._execute_core(context=ctx, as_of=as_of),
            dry_run=self.dry_run,
            no_email=self.no_email,
        )

        # Sync email_sent boolean flag from email_status for backward compatibility
        if result.email_status == "sent":
            result.email_sent = True
        elif self.no_email and result.email_status in ("disabled", "skipped_no_email"):
            result.email_status = "skipped_no_email"
            result.email_sent = False
        else:
            result.email_sent = False

        return result


def exit_code_for_result(result: EconomicCalendarRefreshJobResult) -> int:
    if result.status not in ("success", "partial") or result.database_status == "failed":
        return 1
    return 0


async def run_economic_calendar_refresh_job(dry_run: bool = False, no_email: bool = False) -> EconomicCalendarRefreshJobResult:
    job = EconomicCalendarRefreshJob(dry_run=dry_run, no_email=no_email)
    return await job.run()


def print_smoke_report(result: EconomicCalendarRefreshJobResult):
    print("\n" + "=" * 105)
    print("FINBOT ECONOMIC CALENDAR REFRESH SMOKE REPORT (READ-ONLY)")
    print("=" * 105)
    print(f"Overall Status: {result.status.upper()} | Sync Action: {result.sync_action}")
    print(f"Coverage: {result.coverage_start} -> {result.coverage_end} (Europe/Istanbul)")
    print("-" * 105)
    print(f"{'Provider':<30} | {'Status':<8} | {'Valid?':<6} | {'Observed':<8} | {'Coverage':<8} | {'Malformed':<9} | {'Fresh':<6}")
    print("-" * 105)
    for po in result.provider_outcomes:
        val_str = "YES" if po.structure_validated else "NO"
        print(f"{po.provider_name:<30} | {po.status:<8} | {val_str:<6} | {po.observed_candidate_count:<8} | {po.candidate_count:<8} | {po.malformed_count:<9} | {po.event_count:<6}")
        if po.warnings:
            for w in po.warnings:
                print(f"  [WARN] {w}")
        if po.error:
            print(f"  [ERR]  {po.error}")
    print("-" * 105)
    provider_fresh_total = sum(po.event_count for po in result.provider_outcomes)
    print(f"Provider Fresh Total:        {provider_fresh_total}")
    print(f"Total Fresh Events in Diff:  {result.diff.fetched_event_count}")
    print(f"Total Final Events in Diff:  {result.diff.final_event_count}")
    count_sync_ok = (provider_fresh_total == result.diff.fetched_event_count == result.diff.final_event_count)
    print(f"Count Invariant Check:       {'PASSED (All counts equal)' if count_sync_ok else 'FAILED (Count mismatch detected)'}")
    print("=" * 105 + "\n")


def validate_smoke_quality_gate(result: EconomicCalendarRefreshJobResult) -> Tuple[bool, List[str]]:
    expected_providers = {"TUIK", "TCMB", "BLS", "BEA", "FED"}
    reasons = []

    outcome_pids = [po.provider_id for po in result.provider_outcomes]
    outcome_pid_set = set(outcome_pids)

    # 1. Exact set match
    if outcome_pid_set != expected_providers:
        missing = expected_providers - outcome_pid_set
        extra = outcome_pid_set - expected_providers
        if missing:
            reasons.append(f"Missing expected providers: {missing}")
        if extra:
            reasons.append(f"Unexpected extra providers: {extra}")

    # 2. No duplicates
    if len(outcome_pids) != len(outcome_pid_set):
        reasons.append("Duplicate provider outcomes found in result.")

    # 3. Every provider status == success and structure_validated is True
    for po in result.provider_outcomes:
        if po.status != "success":
            reasons.append(f"Provider {po.provider_id} status is '{po.status}' (expected 'success').")
        if not po.structure_validated:
            reasons.append(f"Provider {po.provider_id} structure_validated is False.")

    # 4. Overall status == success
    if result.status != "success":
        reasons.append(f"Overall job status is '{result.status}' (expected 'success').")

    # 5. Fresh events > 0
    if result.diff.fetched_event_count == 0:
        reasons.append("Total fresh event count is 0.")

    # 6. Count Invariant: Provider fresh total == Diff fetched event count == Diff final event count
    reported_fresh_total = sum(po.event_count for po in result.provider_outcomes)
    if reported_fresh_total != result.diff.fetched_event_count:
        reasons.append(
            f"Provider fresh total mismatch: sum(provider_outcomes.event_count) = {reported_fresh_total} != diff.fetched_event_count = {result.diff.fetched_event_count}."
        )
    if result.diff.fetched_event_count != result.diff.final_event_count:
        reasons.append(
            f"Dry-run event loss: fetched_event_count = {result.diff.fetched_event_count} != final_event_count = {result.diff.final_event_count}."
        )

    return len(reasons) == 0, reasons


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FinBot Daily Economic Calendar Refresh CLI")
    parser.add_argument("--dry-run", action="store_true", help="Fetch live providers without writing to state_store or disk")
    parser.add_argument("--no-email", action="store_true", help="Do not send operational email report")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    job = EconomicCalendarRefreshJob(dry_run=args.dry_run, no_email=args.no_email)
    res = asyncio.run(job.run())

    if args.dry_run:
        print_smoke_report(res)
        passed, fail_reasons = validate_smoke_quality_gate(res)
        if not passed:
            for r in fail_reasons:
                logger.error(f"Smoke quality gate FAILED: {r}")
            sys.exit(1)
        else:
            logger.info("Smoke check PASSED quality gate: all 5 live providers returned success with valid structure, events, and preserved count invariants.")
            sys.exit(0)

    exit_code = exit_code_for_result(res)
    logger.info(f"Economic Calendar Refresh Job completed with status '{res.status}', sync action '{res.sync_action}', exit code {exit_code}")
    sys.exit(exit_code)
