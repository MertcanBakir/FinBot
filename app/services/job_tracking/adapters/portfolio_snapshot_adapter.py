from datetime import datetime, timezone
from typing import Optional

from app.models.job_tracking import JobRunOutcome, JobStatus, SyncAction
from app.models.portfolio_snapshots import PortfolioSnapshotJobResult
from app.repositories.job_tracking_repository import sanitize_tracking_error
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


class PortfolioSnapshotResultAdapter(JobResultAdapter[PortfolioSnapshotJobResult]):
    """Adapter for converting PortfolioSnapshotJobResult into JobRunOutcome."""

    def to_outcome(self, result: PortfolioSnapshotJobResult) -> JobRunOutcome:
        status: JobStatus = result.status

        sync_action: Optional[SyncAction]
        if status == "failed":
            sync_action = "failed_no_write"
        elif status == "partial":
            sync_action = "partial_update"
        elif result.dry_run:
            sync_action = "dry_run"
        elif result.completed_count == result.reused_count:
            sync_action = "no_op"
        else:
            sync_action = "updated"

        db_write_exec = (not result.dry_run) and (
            result.completed_count > result.reused_count
        )

        metrics = {
            "requested_count": result.requested_count,
            "response_count": result.requested_count,
            "valid_count": result.completed_count,
            "matched_count": result.completed_count,
            "missing_count": result.failed_count + result.missing_snapshot_count,
            "inserted_or_updated_count": result.completed_count - result.reused_count,
            "unchanged_count": result.reused_count,
            "stale_skipped_count": 0,
            "stale_marked_count": 0,
            "provider_request_executed": False,
            "database_write_executed": db_write_exec,
            "dry_run": result.dry_run,
            # Snapshot specific details
            "snapshot_date": result.snapshot_date.isoformat(),
            "completed_count": result.completed_count,
            "partial_count": result.partial_count,
            "failed_count": result.failed_count,
            "reused_count": result.reused_count,
            "missing_snapshot_count": result.missing_snapshot_count,
            "failures_count": len(result.failures),
        }

        err_msg = "; ".join(result.errors) if result.errors else None

        return JobRunOutcome(
            status=status,
            sync_action=sync_action,
            finished_at=_parse_dt(result.finished_at),
            duration_seconds=result.duration_seconds,
            warnings=list(result.warnings or []),
            metrics=metrics,
            error=sanitize_tracking_error(err_msg),
        )
