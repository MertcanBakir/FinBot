from datetime import datetime, timezone
from typing import Optional

from app.models.job_tracking import JobRunOutcome, JobStatus, SyncAction
from app.models.market_summary_job import MarketSummaryJobResult
from app.repositories.job_tracking_repository import (
    sanitize_recursive,
    sanitize_tracking_error,
)
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


class MarketSummaryResultAdapter(JobResultAdapter[MarketSummaryJobResult]):
    """Adapter for converting MarketSummaryJobResult into JobRunOutcome without provider outcomes."""

    def to_outcome(self, result: MarketSummaryJobResult) -> JobRunOutcome:
        status: JobStatus = result.status

        sync_action: SyncAction
        if status == "failed":
            sync_action = "failed_no_write"
        elif result.dry_run:
            sync_action = "dry_run"
        elif status == "partial":
            sync_action = "partial_update"
        elif status == "skipped":
            sync_action = "skipped"
        else:
            sync_action = "no_op"

        # Sanitize metrics and ensure email content/body is never included in metrics JSON
        raw_metrics = dict(result.metrics or {})
        raw_metrics.pop("subject", None)
        raw_metrics.pop("body_text", None)
        raw_metrics.pop("body_html", None)
        raw_metrics["report_type"] = result.report_type
        raw_metrics["warning_count"] = len(result.warnings)
        if result.dry_run:
            raw_metrics["dry_run"] = True
        if result.no_email:
            raw_metrics["no_email"] = True

        metrics = sanitize_recursive(raw_metrics)

        return JobRunOutcome(
            status=status,
            sync_action=sync_action,
            finished_at=_parse_dt(result.finished_at),
            duration_seconds=result.duration_seconds,
            warnings=list(result.warnings or []),
            metrics=metrics,
            error=sanitize_tracking_error(result.error),
            provider_outcomes=[],
        )
