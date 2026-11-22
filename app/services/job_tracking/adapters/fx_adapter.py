from datetime import datetime, timezone

from app.models.fx_prices import FxPriceJobResult
from app.models.job_tracking import JobRunOutcome
from app.services.job_tracking.adapters.base import JobResultAdapter


class FxPriceResultAdapter(JobResultAdapter[FxPriceJobResult]):
    """Adapter for converting FxPriceJobResult into JobRunOutcome."""

    def to_outcome(self, result: FxPriceJobResult) -> JobRunOutcome:
        status = result.status

        sync_action: str
        if status == "failed":
            sync_action = "failed_no_write"
        elif status == "partial":
            sync_action = "partial_update"
        elif result.dry_run:
            sync_action = "dry_run"
        elif result.was_unchanged:
            sync_action = "no_op"
        else:
            sync_action = "updated"

        db_write_exec = (not result.dry_run) and (result.database_writes > 0)

        metrics = {
            "requested_count": 1,
            "response_count": 1 if result.price is not None else 0,
            "valid_count": 1 if result.price is not None else 0,
            "matched_count": 1 if result.price is not None else 0,
            "missing_count": 0 if result.price is not None else 1,
            "inserted_or_updated_count": 1 if (db_write_exec and not result.was_unchanged) else 0,
            "unchanged_count": 1 if result.was_unchanged else 0,
            "stale_skipped_count": 0,
            "stale_marked_count": 0,
            "provider_request_executed": getattr(result, "provider_request_executed", False),
            "database_write_executed": db_write_exec,
            "dry_run": result.dry_run,
            # FX specific details
            "instrument_id": str(result.instrument_id) if result.instrument_id else None,
            "price": str(result.price) if result.price is not None else None,
            "previous_close": str(result.previous_close) if result.previous_close is not None else None,
            "price_at": result.price_at.isoformat() if result.price_at else None,
        }

        return JobRunOutcome(
            status=status,
            sync_action=sync_action,
            finished_at=datetime.now(timezone.utc),
            warnings=[],
            metrics=metrics,
            error=result.error,
        )
