from datetime import datetime
from typing import Optional

from app.models.job_tracking import JobRunOutcome
from app.models.tefas_prices import TefasPricesJobResult
from app.services.job_tracking.adapters.base import JobResultAdapter


def _parse_dt(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


class TefasPricesResultAdapter(JobResultAdapter[TefasPricesJobResult]):
    """Adapter for converting TefasPricesJobResult into JobRunOutcome."""

    def to_outcome(self, result: TefasPricesJobResult) -> JobRunOutcome:
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
            result.price_not_published_stale_count
            + result.instrument_inactive_stale_count
            + result.catalog_deactivated_count
        )
        db_write_exec = (not result.dry_run) and (
            result.inserted_or_updated_count > 0
            or stale_marked > 0
            or result.catalog_added_count > 0
            or result.catalog_reactivated_count > 0
            or result.catalog_name_updated_count > 0
        )

        metrics = {
            "requested_count": result.catalog_observed_count,
            "response_count": result.fetched_count,
            "valid_count": result.valid_count,
            "matched_count": result.matched_count,
            "missing_count": result.missing_price_count,
            "inserted_or_updated_count": result.inserted_or_updated_count,
            "unchanged_count": result.unchanged_count,
            "stale_skipped_count": result.stale_skipped_count,
            "stale_marked_count": stale_marked,
            "provider_request_executed": getattr(result, "provider_request_executed", False),
            "database_write_executed": db_write_exec,
            "dry_run": result.dry_run,
            # TEFAS specific reconciliation details
            "tefas_data_date": result.tefas_data_date,
            "previous_data_date": result.previous_data_date,
            "catalog_observed_count": result.catalog_observed_count,
            "catalog_added_count": result.catalog_added_count,
            "catalog_active_added_count": result.catalog_active_added_count,
            "catalog_unpriced_added_count": result.catalog_unpriced_added_count,
            "catalog_reactivated_count": result.catalog_reactivated_count,
            "catalog_deactivated_count": result.catalog_deactivated_count,
            "catalog_disappeared_deactivated_count": result.catalog_disappeared_deactivated_count,
            "catalog_identity_invalid_count": result.catalog_identity_invalid_count,
            "catalog_disappearance_reconciliation_skipped": result.catalog_disappearance_reconciliation_skipped,
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
