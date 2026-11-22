from datetime import datetime, timezone
from typing import List, Optional

from app.models.economic_calendar_refresh import EconomicCalendarRefreshJobResult
from app.models.job_tracking import (
    JobRunOutcome,
    JobTrackingFinalizeError,
    ProviderRunOutcome,
    SyncAction,
    TerminalJobStatus,
)
from app.repositories.job_tracking_repository import sanitize_tracking_error
from app.services.job_tracking.adapters.base import JobResultAdapter
from app.services.provider_code_resolver import resolve_economic_provider_code


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


class EconomicCalendarRefreshResultAdapter(JobResultAdapter[EconomicCalendarRefreshJobResult]):
    """Adapter for converting EconomicCalendarRefreshJobResult into JobRunOutcome with provider breakdowns."""

    def to_outcome(self, result: EconomicCalendarRefreshJobResult) -> JobRunOutcome:
        status: TerminalJobStatus = "failed" if result.status == "failed" else ("partial" if result.status == "partial" else "success")
        error: Optional[str] = None

        if result.database_status == "failed":
            status = "failed"
            error = sanitize_tracking_error(result.database_error) or "Database synchronization failed"

        sync_action: Optional[SyncAction]
        if status == "failed":
            sync_action = "failed_no_write"
        elif result.sync_action == "dry_run":
            sync_action = "dry_run"
        elif result.sync_action in (
            "replaced",
            "merged_and_updated",
            "initial_partial_written",
            "preserved_previous",
            "write_failed",
            "not_written",
        ):
            sync_action = result.sync_action
        else:
            sync_action = "no_op"

        fresh_cnt = result.diff.fetched_event_count if result.diff else 0
        final_cnt = result.diff.final_event_count if result.diff else 0

        metrics = {
            "state_source": result.state_source,
            "fresh_event_count": fresh_cnt,
            "final_event_count": final_cnt,
            "diff": result.diff.model_dump(mode="json") if result.diff else {},
            "coverage_start": result.coverage_start,
            "coverage_end": result.coverage_end,
            "database_status": result.database_status,
        }

        provider_outcomes: List[ProviderRunOutcome] = []
        for po in result.provider_outcomes or []:
            p_code = resolve_economic_provider_code(po.provider_id, po.provider_name)
            if not p_code:
                raise JobTrackingFinalizeError(
                    f"Unknown economic provider: id='{po.provider_id}', name='{po.provider_name}'"
                )
            po_status: TerminalJobStatus = "failed" if po.status == "failed" else ("partial" if po.status == "partial" else "success")
            provider_outcomes.append(
                ProviderRunOutcome(
                    provider_code=p_code,
                    scope="ECONOMIC",
                    status=po_status,
                    structure_validated=po.structure_validated,
                    observed_count=po.observed_candidate_count,
                    candidate_count=po.candidate_count,
                    fresh_event_count=po.event_count,
                    carried_forward_count=po.carried_forward_count,
                    malformed_count=po.malformed_count,
                    retry_count=0,
                    warnings=list(po.warnings or []),
                    error=sanitize_tracking_error(po.error),
                    metrics={},
                )
            )

        finished_dt = _parse_dt(getattr(result, "finished_at", None))

        return JobRunOutcome(
            status=status,
            sync_action=sync_action,
            finished_at=finished_dt,
            coverage_start=result.coverage_start,
            coverage_end=result.coverage_end,
            fresh_event_count=fresh_cnt,
            final_event_count=final_cnt,
            warnings=list(result.warnings or []),
            metrics=metrics,
            error=error,
            provider_outcomes=provider_outcomes,
        )
