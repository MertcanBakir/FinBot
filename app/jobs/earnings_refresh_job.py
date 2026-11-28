import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_connection
from app.core.job_names import JOB_NAME_EARNINGS_REFRESH
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    Nasdaq100EarningsCalendar,
)
from app.models.earnings_refresh import (
    EarningsRefreshJobResult,
    MarketRefreshOutcome,
    OfficialIrProviderSummary,
    OfficialIrRefreshSummary,
    RefreshStatus,
    SyncAction,
)
from app.models.job_tracking import (
    JobExecutionContext,
    JobRunSpecification,
)
from app.services.bist100_earnings import (
    Bist100EarningsService,
    bist100_earnings_service,
)
from app.services.calendar_state_store import CalendarStateStore, calendar_state_store
from app.services.database_refresh_sync import sync_earnings_refresh_to_database
from app.services.earnings_refresh_report import (
    compare_bist_calendars,
    compare_nasdaq_calendars,
)
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.earnings_adapter import (
    EarningsRefreshResultAdapter,
)
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import (
    EarningsEmailRenderer,
)
from app.services.job_tracking.runner import TrackedJobRunner
from app.services.nasdaq100_earnings import (
    Nasdaq100EarningsService,
    nasdaq100_earnings_service,
)
from app.services.prepared_refresh_sync import sync_prepared_refresh

logger = logging.getLogger(__name__)

def exit_code_for_result(result: EarningsRefreshJobResult) -> int:
    """Return CLI exit code: 1 if overall_status or database_status is 'failed', 0 otherwise."""
    if result.overall_status == "failed" or result.database_status == "failed":
        return 1
    return 0


class EarningsRefreshJob:
    """Operational job conducting daily BIST100 and NASDAQ100 rolling calendar refresh, state_store updating, and operational email reporting."""

    def __init__(
        self,
        bist_service: Optional[Bist100EarningsService] = None,
        nasdaq_service: Optional[Nasdaq100EarningsService] = None,
        state_store: Optional[CalendarStateStore] = None,
        email_service: Optional[EmailService] = None,
        dry_run: bool = False,
        no_email: bool = False,
        db_connection_factory: Optional[Any] = None,
    ):
        self.bist_service = bist_service or bist100_earnings_service
        self.nasdaq_service = nasdaq_service or nasdaq100_earnings_service
        self.state_store = state_store or calendar_state_store
        self.email_service = email_service or EmailService()
        self.dry_run = dry_run
        self.no_email = no_email
        self.db_connection_factory = db_connection_factory or get_db_connection

    async def _execute_core(
        self,
        context: JobExecutionContext,
        as_of: Optional[datetime] = None,
    ) -> EarningsRefreshJobResult:
        started_at_dt = datetime.now(timezone.utc)
        start_perf = time.perf_counter()

        tz = ZoneInfo(settings.TIMEZONE)
        if as_of is None:
            as_of_dt = datetime.now(tz)
        elif as_of.tzinfo is None:
            as_of_dt = as_of.replace(tzinfo=tz)
        else:
            as_of_dt = as_of.astimezone(tz)

        as_of_date = as_of_dt.date()
        coverage_start = as_of_date
        coverage_end = as_of_date + timedelta(days=90)  # inclusive 91 calendar days

        # 1. Load previous state_store snapshots
        previous_bist = self.state_store.load_bist()
        previous_nasdaq = self.state_store.load_nasdaq()

        # 2. Parallel independent service fetching
        bist_task = self.bist_service.get_refresh_bundle(as_of=as_of_dt)
        nasdaq_task = self.nasdaq_service.get_rolling_calendar(as_of=as_of_dt)

        bist_res, nasdaq_res = await asyncio.gather(bist_task, nasdaq_task, return_exceptions=True)

        # 3. Process BIST outcome and rolling state staging
        bist_fetch: Optional[Bist100EarningsCalendar] = None
        bist_fetch_status: RefreshStatus = "failed"
        bist_error: Optional[str] = None
        bist_sync_action: SyncAction = "not_written"
        bist_state_updated = False
        bist_state_preserved = False
        bist_outcome_status: RefreshStatus = "failed"
        final_bist: Optional[Bist100EarningsCalendar] = previous_bist

        if isinstance(bist_res, Exception):
            bist_error = f"BIST refresh service exception: {type(bist_res).__name__}"
            logger.error("BIST refresh service exception (error_type=%s)", type(bist_res).__name__)
            if previous_bist:
                bist_sync_action = "preserved_previous"
                bist_state_preserved = True
            else:
                bist_sync_action = "not_written"
            bist_outcome_status = "failed"
        else:
            # bist_res is Bist100EarningsRefreshBundle
            rolling_cal = bist_res.rolling_calendar

            bist_fetch = rolling_cal
            bist_fetch_status = rolling_cal.status
            if bist_fetch_status == "failed":
                bist_outcome_status = "failed"
                if previous_bist:
                    bist_sync_action = "preserved_previous"
                    bist_state_preserved = True
                else:
                    bist_sync_action = "not_written"
            else:
                try:
                    final_bist = self.state_store.stage_bist(rolling_cal)
                    if final_bist is None:
                        raise RuntimeError("BIST state candidate was not staged")
                    bist_state_updated = True
                    if bist_fetch_status == "success":
                        bist_sync_action = "replaced"
                        bist_outcome_status = "success"
                    else:
                        bist_sync_action = "merged_and_updated" if previous_bist else "initial_partial_written"
                        bist_outcome_status = "partial"
                except Exception as stage_exc:
                    logger.error("BIST state staging failed (error_type=%s)", type(stage_exc).__name__)
                    bist_sync_action = "preserved_previous" if previous_bist else "not_written"
                    bist_state_preserved = previous_bist is not None
                    bist_outcome_status = "failed"
                    bist_error = f"State staging failed: {type(stage_exc).__name__}"
                    final_bist = previous_bist

        # 4. Process NASDAQ outcome & state staging
        nasdaq_fetch: Optional[Nasdaq100EarningsCalendar] = None
        nasdaq_fetch_status: RefreshStatus = "failed"
        nasdaq_error: Optional[str] = None
        nasdaq_sync_action: SyncAction = "not_written"
        nasdaq_state_updated = False
        nasdaq_state_preserved = False
        nasdaq_outcome_status: RefreshStatus = "failed"
        final_nasdaq: Optional[Nasdaq100EarningsCalendar] = previous_nasdaq

        if isinstance(nasdaq_res, Exception):
            nasdaq_error = f"NASDAQ rolling service exception: {type(nasdaq_res).__name__}"
            logger.error("NASDAQ rolling service exception (error_type=%s)", type(nasdaq_res).__name__)
            if previous_nasdaq:
                nasdaq_sync_action = "preserved_previous"
                nasdaq_state_preserved = True
            else:
                nasdaq_sync_action = "not_written"
            nasdaq_outcome_status = "failed"
        else:
            nasdaq_fetch = nasdaq_res
            nasdaq_fetch_status = nasdaq_res.status
            if nasdaq_fetch_status == "failed" or (nasdaq_fetch_status == "partial" and previous_nasdaq):
                nasdaq_sync_action = "preserved_previous" if previous_nasdaq else "not_written"
                nasdaq_state_preserved = previous_nasdaq is not None
                nasdaq_outcome_status = "partial" if nasdaq_fetch_status == "partial" else "failed"
            else:
                try:
                    final_nasdaq = self.state_store.stage_nasdaq(nasdaq_res)
                    if final_nasdaq is None:
                        raise RuntimeError("NASDAQ state candidate was not staged")
                    nasdaq_state_updated = True
                    if nasdaq_fetch_status == "success":
                        nasdaq_sync_action = "replaced"
                        nasdaq_outcome_status = "success"
                    else:
                        nasdaq_sync_action = "initial_partial_written"
                        nasdaq_outcome_status = "partial"
                except Exception as stage_exc:
                    logger.error("NASDAQ state staging failed (error_type=%s)", type(stage_exc).__name__)
                    nasdaq_sync_action = "preserved_previous" if previous_nasdaq else "not_written"
                    nasdaq_state_preserved = previous_nasdaq is not None
                    nasdaq_outcome_status = "failed"
                    nasdaq_error = f"State staging failed: {type(stage_exc).__name__}"
                    final_nasdaq = previous_nasdaq

        # 5. Overall Status Calculation
        if bist_outcome_status == "success" and nasdaq_outcome_status == "success":
            overall_status: RefreshStatus = "success"
        elif bist_outcome_status == "failed" and nasdaq_outcome_status == "failed":
            overall_status: RefreshStatus = "failed"
        else:
            overall_status: RefreshStatus = "partial"

        # 7. Official IR summary calculation
        official_ir_summary: Optional[OfficialIrRefreshSummary] = None
        if not isinstance(bist_res, Exception) and getattr(bist_res, "combined_result", None):
            comb = bist_res.combined_result
            ir_prov_summaries = []
            total_ir_events = 0
            for ir_res in comb.official_ir_results:
                total_ir_events += len(ir_res.events)
                ir_prov_summaries.append(OfficialIrProviderSummary(
                    symbol=ir_res.symbol,
                    source_provider=ir_res.source_provider,
                    status=ir_res.status,
                    rows_seen=ir_res.rows_seen,
                    accepted_row_count=ir_res.accepted_row_count,
                    rejected_row_count=ir_res.rejected_row_count,
                    parser_failed=ir_res.parser_failed,
                    retry_count=ir_res.retry_count,
                    events_count=len(ir_res.events),
                    warnings=ir_res.warnings
                ))

            # Group raw IR events by unique identity: (symbol, period_start, period_end)
            raw_ir_by_identity: Dict[Tuple[str, Optional[str], Optional[str]], List[BistEarningsEvent]] = {}
            for ir_res in comb.official_ir_results:
                for ir_ev in ir_res.events:
                    key = (ir_ev.symbol, ir_ev.period_start, ir_ev.period_end)
                    raw_ir_by_identity.setdefault(key, []).append(ir_ev)

            kap_events_map = {(e.symbol, e.period_start, e.period_end): e for e in comb.kap_fetch_result.normalized_events}

            # events_added_from_ir_count: unique Official IR identities actually accepted in combined normalized output
            ir_norm_cnt = sum(1 for e in comb.normalized_events if e.source == "Official IR")
            ir_added_count = ir_norm_cnt

            # events_overridden_by_kap_count: unique identities present in both raw IR and KAP normalized
            overlapping_identities = set(raw_ir_by_identity.keys()) & set(kap_events_map.keys())
            overridden_count = len(overlapping_identities)

            # kap_ir_conflicts_count: unique overlapping identities where KAP date differs from any IR date
            conflicts_count = 0
            for key in overlapping_identities:
                kap_ev = kap_events_map[key]
                if any(ir_ev.earnings_date != kap_ev.earnings_date for ir_ev in raw_ir_by_identity[key]):
                    conflicts_count += 1

            # Real calculation of IR dates changed between previous_bist and final_bist
            ir_dates_changed_count = 0
            if previous_bist and final_bist:
                prev_ir_map = {
                    (e.symbol, e.period_start, e.period_end): e.earnings_date
                    for e in previous_bist.events
                    if e.source == "Official IR"
                }
                final_ir_map = {
                    (e.symbol, e.period_start, e.period_end): e.earnings_date
                    for e in final_bist.events
                    if e.source == "Official IR"
                }
                for key, prev_date in prev_ir_map.items():
                    if key in final_ir_map and final_ir_map[key] != prev_date:
                        ir_dates_changed_count += 1

            # Structured event scoping counts
            curr_rolling_events = bist_fetch.events if bist_fetch else []
            kap_norm_cnt = len(comb.kap_fetch_result.normalized_events)
            kap_roll_cnt = sum(1 for e in curr_rolling_events if e.source == "KAP")
            ir_raw_cnt = sum(len(r.events) for r in comb.official_ir_results)
            ir_norm_cnt = sum(1 for e in comb.normalized_events if e.source == "Official IR")
            ir_roll_cnt = sum(1 for e in curr_rolling_events if e.source == "Official IR")
            comb_roll_cnt = len(curr_rolling_events)

            curr_fetch_identities = {(e.symbol, e.period_start, e.period_end) for e in curr_rolling_events}
            ir_carried_cnt = sum(
                1 for e in (final_bist.events if final_bist else [])
                if e.source == "Official IR" and (e.symbol, e.period_start, e.period_end) not in curr_fetch_identities
            )

            official_ir_summary = OfficialIrRefreshSummary(
                providers=ir_prov_summaries,
                events_added_from_ir_count=ir_added_count,
                events_overridden_by_kap_count=overridden_count,
                kap_ir_conflicts_count=conflicts_count,
                ir_dates_changed_count=ir_dates_changed_count,
                kap_fetched_event_count=len(comb.kap_fetch_result.events),
                ir_fetched_event_count=total_ir_events,
                kap_fetch_status=comb.kap_fetch_result.status,
                kap_normalized_event_count=kap_norm_cnt,
                kap_rolling_event_count=kap_roll_cnt,
                ir_provider_raw_event_count=ir_raw_cnt,
                ir_normalized_event_count=ir_norm_cnt,
                ir_rolling_event_count=ir_roll_cnt,
                combined_rolling_event_count=comb_roll_cnt,
                ir_carried_forward_count=ir_carried_cnt,
                kap_warnings=list(comb.kap_fetch_result.warnings),
            )

        # 6. Diff Calculations
        bist_diff = compare_bist_calendars(previous_bist, bist_fetch, final_bist, coverage_start, coverage_end)
        nasdaq_diff = compare_nasdaq_calendars(previous_nasdaq, nasdaq_fetch, final_nasdaq, coverage_start, coverage_end)

        bist_sym_res = bist_fetch.symbol_results if (bist_fetch and hasattr(bist_fetch, "symbol_results")) else []
        bist_unavail_syms = [s.symbol for s in bist_sym_res if (not s.is_complete or s.calendar_status == "unavailable")] if bist_sym_res else getattr(bist_fetch, "unavailable_symbols", [])

        bist_outcome = MarketRefreshOutcome(
            market="BIST100",
            outcome_status=bist_outcome_status,
            fetch_status=bist_fetch_status,
            sync_action=bist_sync_action,
            state_updated=bist_state_updated,
            state_preserved=bist_state_preserved,
            coverage_start=coverage_start.isoformat(),
            coverage_end=coverage_end.isoformat(),
            total_symbol_count=len(bist_sym_res) if bist_sym_res else getattr(bist_fetch, "total_symbol_count", None),
            completed_symbol_count=sum(1 for s in bist_sym_res if s.is_complete) if bist_sym_res else getattr(bist_fetch, "completed_symbol_count", None),
            unavailable_symbol_count=len(bist_unavail_syms) if bist_sym_res else getattr(bist_fetch, "unavailable_symbol_count", None),
            scheduled_symbol_count=sum(1 for s in bist_sym_res if s.calendar_status == "scheduled") if bist_sym_res else getattr(bist_fetch, "scheduled_symbol_count", None),
            healthy_negative_symbol_count=sum(1 for s in bist_sym_res if s.calendar_status == "healthy_negative") if bist_sym_res else getattr(bist_fetch, "healthy_negative_symbol_count", None),
            diff=bist_diff,
            official_ir_summary=official_ir_summary,
            unavailable_symbols=bist_unavail_syms,
            warnings=list(bist_fetch.warnings) if (bist_fetch and hasattr(bist_fetch, "warnings")) else ([bist_error] if bist_error else []),
            error=bist_error,
        )

        nasdaq_outcome = MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status=nasdaq_outcome_status,
            fetch_status=nasdaq_fetch_status,
            sync_action=nasdaq_sync_action,
            state_updated=nasdaq_state_updated,
            state_preserved=nasdaq_state_preserved,
            coverage_start=coverage_start.isoformat(),
            coverage_end=coverage_end.isoformat(),
            total_symbol_count=None,
            completed_symbol_count=None,
            unavailable_symbol_count=None,
            scheduled_symbol_count=None,
            healthy_negative_symbol_count=None,
            diff=nasdaq_diff,
            unavailable_symbols=[],
            warnings=list(nasdaq_fetch.warnings) if (nasdaq_fetch and hasattr(nasdaq_fetch, "warnings")) else ([nasdaq_error] if nasdaq_error else []),
            error=nasdaq_error,
        )

        finished_at_dt = datetime.now(timezone.utc)
        duration_sec = time.perf_counter() - start_perf

        result = EarningsRefreshJobResult(
            started_at=started_at_dt.isoformat(),
            finished_at=finished_at_dt.isoformat(),
            duration_seconds=duration_sec,
            coverage_start=coverage_start.isoformat(),
            coverage_end=coverage_end.isoformat(),
            overall_status=overall_status,
            bist=bist_outcome,
            nasdaq=nasdaq_outcome,
            email_status="pending"
        )

        # 8. Persist the prepared state, then complete the result with DB status.
        return await sync_prepared_refresh(
            sync_earnings_refresh_to_database,
            result,
            final_bist=final_bist,
            final_nasdaq=final_nasdaq,
            refresh_run_id=context.run_id,
            dry_run=self.dry_run,
        )

    async def run(self, as_of: Optional[datetime] = None) -> EarningsRefreshJobResult:
        runner = TrackedJobRunner(
            spec=JobRunSpecification(
                job_name=JOB_NAME_EARNINGS_REFRESH,
                operation_type="calendar_refresh",
                scope="EARNINGS",
                provider_code=None,
            ),
            adapter=EarningsRefreshResultAdapter(),
            renderer=EarningsEmailRenderer(),
            notification_rule=JobNotificationRule.ALWAYS_ON_FINISH,
            email_service=self.email_service,
            db_connection_factory=self.db_connection_factory,
        )

        result = await runner.run_async(
            lambda ctx: self._execute_core(context=ctx, as_of=as_of),
            dry_run=self.dry_run,
            no_email=self.no_email,
        )

        # Sync email_status for backward compatibility
        if self.no_email and result.email_status in ("disabled", "skipped_no_email"):
            result.email_status = "skipped_no_email"

        return result


async def run_earnings_refresh_job(
    as_of: Optional[datetime] = None,
    dry_run: bool = False,
    no_email: bool = False,
) -> EarningsRefreshJobResult:
    """Public helper function to execute daily earnings refresh job."""
    job = EarningsRefreshJob(dry_run=dry_run, no_email=no_email)
    return await job.run(as_of=as_of)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FinBot Daily Earnings Refresh CLI")
    parser.add_argument("--dry-run", action="store_true", help="Fetch live providers without writing to state_store or database")
    parser.add_argument("--no-email", action="store_true", help="Do not send operational email report")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    res = asyncio.run(run_earnings_refresh_job(dry_run=args.dry_run, no_email=args.no_email))
    sys.exit(exit_code_for_result(res))
