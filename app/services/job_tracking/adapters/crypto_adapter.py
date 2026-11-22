from datetime import datetime
from typing import Optional

from app.models.crypto_prices import BinanceCryptoPricesJobResult
from app.models.job_tracking import JobRunOutcome
from app.services.job_tracking.adapters.base import JobResultAdapter


def _parse_dt(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


class BinanceCryptoPricesResultAdapter(JobResultAdapter[BinanceCryptoPricesJobResult]):
    """Adapter for converting BinanceCryptoPricesJobResult into JobRunOutcome."""

    def to_outcome(self, result: BinanceCryptoPricesJobResult) -> JobRunOutcome:
        status = result.status

        sync_action: str
        if status == "skipped":
            sync_action = "skipped"
        elif status == "failed":
            sync_action = "failed_no_write"
        elif status == "partial":
            sync_action = "partial_update"
        elif result.dry_run:
            sync_action = "dry_run"
        elif result.inserted_or_updated_count == 0 and result.matched_count > 0:
            sync_action = "no_op"
        else:
            sync_action = "updated"

        stale_marked = (
            result.price_not_published_stale_count + result.instrument_inactive_stale_count
        )
        db_write_exec = (not result.dry_run) and (
            result.inserted_or_updated_count > 0 or stale_marked > 0
        )

        metrics = {
            "requested_count": result.requested_active_count,
            "response_count": result.response_count,
            "valid_count": result.valid_count,
            "matched_count": result.matched_count,
            "missing_count": result.missing_count,
            "inserted_or_updated_count": result.inserted_or_updated_count,
            "unchanged_count": result.unchanged_count,
            "stale_skipped_count": result.stale_skipped_count,
            "provider_request_executed": getattr(result, "provider_request_executed", False),
            "database_write_executed": db_write_exec,
            "dry_run": result.dry_run,
            # Crypto specific details
            "duplicates_count": result.duplicate_count,
        }

        return JobRunOutcome(
            status=status,
            sync_action=sync_action,
            skip_reason=None,
            finished_at=_parse_dt(result.finished_at),
            duration_seconds=result.duration_seconds,
            warnings=list(result.warnings or []),
            metrics=metrics,
            error=result.error,
        )
