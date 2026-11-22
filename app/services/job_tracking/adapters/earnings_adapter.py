from datetime import datetime, timezone
from typing import List, Optional

from app.models.earnings_refresh import EarningsRefreshJobResult
from app.models.job_tracking import (
    JobRunOutcome,
    ProviderRunOutcome,
    SyncAction,
    TerminalJobStatus,
)
from app.repositories.job_tracking_repository import sanitize_tracking_error
from app.services.database_refresh_sync import resolve_official_ir_aggregate_status
from app.services.job_tracking.adapters.base import JobResultAdapter


def _parse_dt(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class EarningsRefreshResultAdapter(JobResultAdapter[EarningsRefreshJobResult]):
    """Adapter for converting EarningsRefreshJobResult into JobRunOutcome with provider breakdowns."""

    def to_outcome(self, result: EarningsRefreshJobResult) -> JobRunOutcome:
        status: TerminalJobStatus = "failed" if result.overall_status == "failed" else ("partial" if result.overall_status == "partial" else "success")
        error: Optional[str] = None

        if result.database_status == "failed":
            status = "failed"
            error = sanitize_tracking_error(result.database_error) or "Database synchronization failed"

        sync_action: Optional[SyncAction]
        if status == "failed":
            sync_action = "failed_no_write"
        elif result.bist.sync_action == "replaced" or result.nasdaq.sync_action == "replaced":
            sync_action = "replaced"
        elif result.bist.sync_action == "merged_and_updated" or result.nasdaq.sync_action == "merged_and_updated":
            sync_action = "merged_and_updated"
        elif result.bist.sync_action == "initial_partial_written" or result.nasdaq.sync_action == "initial_partial_written":
            sync_action = "initial_partial_written"
        else:
            sync_action = "preserved_previous"

        bist_diff = result.bist.diff
        nasdaq_diff = result.nasdaq.diff
        fresh_event_count = (bist_diff.fetched_event_count if bist_diff else 0) + (
            nasdaq_diff.fetched_event_count if nasdaq_diff else 0
        )
        final_event_count = (bist_diff.final_event_count if bist_diff else 0) + (
            nasdaq_diff.final_event_count if nasdaq_diff else 0
        )

        metrics = {
            "fresh_event_count": fresh_event_count,
            "final_event_count": final_event_count,
            "bist_sync_action": result.bist.sync_action,
            "nasdaq_sync_action": result.nasdaq.sync_action,
            "bist_fetch_status": result.bist.fetch_status,
            "nasdaq_fetch_status": result.nasdaq.fetch_status,
            "coverage_start": result.coverage_start,
            "coverage_end": result.coverage_end,
            "database_status": result.database_status,
        }

        # Build provider breakdowns: KAP, OFFICIAL_IR, YAHOO_FINANCE
        provider_outcomes: List[ProviderRunOutcome] = []

        if result.bist.official_ir_summary is not None:
            oir_summary = result.bist.official_ir_summary

            # 1. KAP (BIST100)
            kap_fresh = oir_summary.kap_fetched_event_count
            raw_kap_status = oir_summary.kap_fetch_status or result.bist.fetch_status
            kap_status: TerminalJobStatus = (
                "failed"
                if raw_kap_status == "failed"
                else ("partial" if raw_kap_status == "partial" else "success")
            )
            kap_warnings = oir_summary.kap_warnings

            provider_outcomes.append(
                ProviderRunOutcome(
                    provider_code="KAP",
                    scope="BIST100",
                    status=kap_status,
                    structure_validated=True,
                    observed_count=result.bist.total_symbol_count or 0,
                    candidate_count=result.bist.completed_symbol_count or 0,
                    fresh_event_count=kap_fresh,
                    carried_forward_count=bist_diff.carried_forward_count if bist_diff else 0,
                    malformed_count=len(result.bist.unavailable_symbols),
                    retry_count=0,
                    warnings=list(dict.fromkeys(kap_warnings or [])),
                    error=sanitize_tracking_error(result.bist.error),
                    metrics={},
                )
            )

            # 2. OFFICIAL_IR (BIST100)
            agg_status_raw = resolve_official_ir_aggregate_status(oir_summary.providers)
            agg_status: TerminalJobStatus = (
                "success"
                if agg_status_raw == "success"
                else ("partial" if agg_status_raw == "partial" else "failed")
            )
            all_oir_warnings: List[str] = []
            for p in oir_summary.providers:
                all_oir_warnings.extend(p.warnings or [])

            provider_outcomes.append(
                ProviderRunOutcome(
                    provider_code="OFFICIAL_IR",
                    scope="BIST100",
                    status=agg_status,
                    structure_validated=True,
                    observed_count=len(oir_summary.providers),
                    candidate_count=sum(p.accepted_row_count for p in oir_summary.providers),
                    fresh_event_count=oir_summary.ir_fetched_event_count,
                    carried_forward_count=oir_summary.ir_carried_forward_count,
                    malformed_count=sum(p.rejected_row_count for p in oir_summary.providers),
                    retry_count=sum(p.retry_count for p in oir_summary.providers),
                    warnings=list(dict.fromkeys(all_oir_warnings)),
                    error=None if agg_status != "failed" else "Official IR providers failed",
                    metrics={},
                )
            )

        # 3. YAHOO_FINANCE (NASDAQ100)
        raw_nasdaq_status = result.nasdaq.fetch_status
        nasdaq_status: TerminalJobStatus = (
            "failed"
            if raw_nasdaq_status == "failed"
            else ("partial" if raw_nasdaq_status == "partial" else "success")
        )
        provider_outcomes.append(
            ProviderRunOutcome(
                provider_code="YAHOO_FINANCE",
                scope="NASDAQ100",
                status=nasdaq_status,
                structure_validated=True,
                observed_count=result.nasdaq.total_symbol_count or 0,
                candidate_count=result.nasdaq.completed_symbol_count or 0,
                fresh_event_count=nasdaq_diff.fetched_event_count if nasdaq_diff else 0,
                carried_forward_count=nasdaq_diff.carried_forward_count if nasdaq_diff else 0,
                malformed_count=len(result.nasdaq.unavailable_symbols),
                retry_count=0,
                warnings=list(result.nasdaq.warnings or []),
                error=sanitize_tracking_error(result.nasdaq.error),
                metrics={},
            )
        )

        all_job_warnings = list(result.bist.warnings or []) + list(result.nasdaq.warnings or [])
        finished_dt = _parse_dt(result.finished_at)

        return JobRunOutcome(
            status=status,
            sync_action=sync_action,
            finished_at=finished_dt,
            duration_seconds=result.duration_seconds,
            coverage_start=result.coverage_start,
            coverage_end=result.coverage_end,
            fresh_event_count=fresh_event_count,
            final_event_count=final_event_count,
            warnings=all_job_warnings,
            metrics=metrics,
            error=error,
            provider_outcomes=provider_outcomes,
        )
