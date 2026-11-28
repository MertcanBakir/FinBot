import argparse
import sys
from typing import Any, List, Optional

from app.core.database import get_db_connection
from app.core.error_sanitization import contains_sensitive_price_error
from app.models.job_tracking import JobRunContext, JobRunSpecification
from app.models.tefas_prices import (
    TefasPricesJobResult,
)
from app.providers.tefas.tefas_price_provider import TefasPriceProvider
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.tefas_adapter import (
    TefasPricesResultAdapter,
)
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import (
    TefasPricesEmailRenderer,
)
from app.services.job_tracking.runner import TrackedJobRunner
from app.services.tefas_prices_service import TefasPricesService
from app.use_cases.prices.tefas_price import (
    execute_tefas_price_refresh as _execute_tefas_prices_job,
)


def sanitize_error_message(exc: Exception) -> str:
    """Convert raw exception into a clean, safe, secret-free message."""
    if not exc:
        return "Bilinmeyen bir hata oluştu."
    msg = str(exc)

    # Check for any sensitive credentials/DSN/token/URL patterns FIRST
    lower_msg = msg.lower()
    if contains_sensitive_price_error(msg):
        return "Veritabanı veya servis erişim hatası."

    # Safe domain validation/logic messages (only if free of secrets)
    if isinstance(exc, (ValueError, RuntimeError)):
        if "Safety check failed" in msg:
            return msg
        if "No valid matched prices" in msg or "No TEFAS" in msg or "No active TEFAS" in msg:
            return msg

    if "timeout" in lower_msg or "timed out" in lower_msg:
        return "Bağlantı zaman aşımına uğradı."
    if "connection" in lower_msg or "could not connect" in lower_msg or "unreachable" in lower_msg:
        return "Veritabanı bağlantısı kurulamadı."
    if "transaction" in lower_msg or "rollback" in lower_msg:
        return "Veritabanı işlem (transaction) hatası."
    return "Operasyonel hata oluştu."


def run_job(
    argv: Optional[List[str]] = None,
    provider: Optional[TefasPriceProvider] = None,
    service: Optional[TefasPricesService] = None,
    email_service: Optional[EmailService] = None,
    db_connection_factory: Optional[Any] = None,
    run_context: Optional[JobRunContext] = None,
) -> TefasPricesJobResult:
    parser = argparse.ArgumentParser(
        description="Fetch, reconcile catalog, and synchronize latest TEFAS fund prices with change tracking to Supabase."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and calculate reconciliation/prices/changes without database writes or emails (default).",
    )
    modes.add_argument(
        "--apply-db",
        action="store_true",
        help="Apply verified catalog changes, latest prices, and stale statuses to Supabase in a single transaction and email on partial/failed.",
    )
    parser.add_argument("--no-email", action="store_true", help="Disable email delivery.")
    args = parser.parse_args(argv)
    return execute_tracked_job(
        dry_run=not args.apply_db, no_email=args.no_email,
        provider=provider, service=service, email_service=email_service, db_connection_factory=db_connection_factory, run_context=run_context,
    )


def execute_tracked_job(
    *,
    dry_run: bool = True,
    no_email: bool = False,
    provider: Optional[TefasPriceProvider] = None,
    service: Optional[TefasPricesService] = None,
    email_service: Optional[EmailService] = None,
    db_connection_factory: Optional[Any] = None,
    run_context: Optional[JobRunContext] = None,
) -> TefasPricesJobResult:


    email_svc = email_service or EmailService()

    runner = TrackedJobRunner(
        spec=JobRunSpecification(
            job_name="tefas_latest_prices_job",
            operation_type="prices_sync",
            scope="TEFAS",
            provider_code="TEFAS",
        ),
        adapter=TefasPricesResultAdapter(),
        renderer=TefasPricesEmailRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=email_svc,
        db_connection_factory=db_connection_factory or get_db_connection,
    )

    result = runner.run(
        lambda _ctx: _execute_tefas_prices_job(
            apply_db=not dry_run,
            provider=provider,
            service=service,
            db_connection_factory=db_connection_factory or get_db_connection,
            sanitize_error_message=sanitize_error_message,
            summary_printer=_print_summary,
        ),
        dry_run=dry_run,
        no_email=no_email,
        context=run_context,
    )
    _print_summary(result)
    return result

def _print_summary(result: TefasPricesJobResult) -> None:
    """Print structured CLI execution summary."""
    mode = "DRY RUN" if result.dry_run else "APPLY DB"
    print(f"Mode: {mode}")
    print(f"Status: {result.status.upper()}")
    print(f"Current Data Date: {result.tefas_data_date or 'N/A'}")
    print(f"Previous Data Date: {result.previous_data_date or 'N/A'}")
    print(f"Duration: {result.duration_seconds:.2f}s")
    print(
        f"Catalog Observed: {result.catalog_observed_count} | "
        f"Added: {result.catalog_added_count} (Active: {result.catalog_active_added_count}, Inactive: {result.catalog_unpriced_added_count}) | "
        f"Renamed: {result.catalog_name_updated_count} | Reactivated: {result.catalog_reactivated_count} | "
        f"Deactivated: {result.catalog_deactivated_count} (Unpriced: {result.catalog_unpriced_deactivated_count}, Disappeared: {result.catalog_disappeared_deactivated_count})"
    )
    if result.catalog_identity_invalid_count > 0 or result.catalog_disappearance_reconciliation_skipped:
        print(
            f"Catalog Identity Invalid: {result.catalog_identity_invalid_count} | "
            f"Disappearance Reconciliation: SKIPPED (Reason: {result.catalog_disappearance_skip_reason})"
        )
    print(
        f"Fetched: {result.fetched_count} | Valid: {result.valid_count} | "
        f"Invalid: {result.invalid_count} | Duplicates: {result.duplicate_count}"
    )
    print(
        f"Matched: {result.matched_count} | Previous Matched: {result.previous_price_matched_count} | "
        f"Previous Missing: {result.previous_price_missing_count} | Unmatched: {result.unmatched_count} | "
        f"Missing Active: {result.missing_price_count}"
    )
    if not result.dry_run:
        print(
            f"Database Price Writes: {result.inserted_or_updated_count} "
            f"(Unchanged: {result.unchanged_count}, Stale Skipped: {result.stale_skipped_count})"
        )
        print(
            f"Stale Marked: {result.price_not_published_stale_count} (price_not_published) | "
            f"{result.instrument_inactive_stale_count} (instrument_inactive)"
        )
        if result.email_status:
            print(f"Email Delivery: {result.email_status}")
    else:
        print(
            f"Would Insert/Update: {result.inserted_or_updated_count} | "
            f"Would Remain Unchanged: {result.unchanged_count} | "
            f"Would Skip As Older: {result.stale_skipped_count}"
        )
        print(
            f"Would Mark price_not_published: {result.price_not_published_stale_count} | "
            f"Would Mark instrument_inactive: {result.instrument_inactive_stale_count}"
        )
        print("Database writes executed: 0 (dry-run)")
        print("Email delivery executed: 0 (dry-run)")
    if result.error:
        print(f"Error: {result.error}")
    if result.warnings:
        print(f"Warnings ({len(result.warnings)}):")
        for w in result.warnings[:5]:
            print(f"  • {w}")
        if len(result.warnings) > 5:
            print(f"  ... and {len(result.warnings) - 5} more warnings")


def main() -> None:
    result = run_job(sys.argv[1:])
    exit_code = 1 if result.status == "failed" else 0
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
