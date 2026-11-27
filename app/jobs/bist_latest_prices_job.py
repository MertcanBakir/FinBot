import argparse
import sys
from datetime import datetime
from typing import Any, List, Optional

from app.core.database import get_db_connection
from app.models.bist_prices import (
    BistPricesJobResult,
)
from app.models.job_tracking import JobRunContext, JobRunSpecification
from app.providers.bist.yahoo_bist_provider import (
    YahooBistCalendarError,
    YahooBistPriceFetchError,
    YahooBistPriceProvider,
)
from app.services.bist_prices_service import (
    BistPricesService,
)
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.bist_adapter import (
    BistPricesResultAdapter,
)
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import (
    BistPricesEmailRenderer,
)
from app.services.job_tracking.runner import TrackedJobRunner
from app.use_cases.prices.bist_price import (
    execute_bist_price_refresh as _execute_bist_prices_job,
)


def sanitize_error_message(exc: Exception) -> str:
    """Convert raw exception into a clean, safe, secret-free message."""
    if not exc:
        return "Bilinmeyen bir hata oluştu."
    msg = str(exc)

    lower_msg = msg.lower()
    if any(k in lower_msg for k in ("password", "auth", "credential", "dsn", "secret", "token", "key", "http:", "https:", "://", "@", "cookie", "crumb")):
        return "Veritabanı veya servis erişim hatası."

    if isinstance(exc, (ValueError, RuntimeError, YahooBistCalendarError, YahooBistPriceFetchError)):
        if "Safety check failed" in msg:
            return msg
        if "Catalog validation failed" in msg:
            return msg
        if "No active BIST" in msg or "Completeness safety check failed" in msg:
            return msg
        if "Failed to query XIST calendar" in msg:
            return "Borsa İstanbul takvim servisi sorgulanamadı."
        if "Failed to download BIST" in msg:
            return "Yahoo Finance veri indirme servisi hatası."

    if "timeout" in lower_msg or "timed out" in lower_msg:
        return "Bağlantı zaman aşımına uğradı."
    if "connection" in lower_msg or "could not connect" in lower_msg or "unreachable" in lower_msg:
        return "Veritabanı bağlantısı kurulamadı."
    if "transaction" in lower_msg or "rollback" in lower_msg:
        return "Veritabanı işlem (transaction) hatası."
    return "Operasyonel hata oluştu."


def _print_summary(res: BistPricesJobResult) -> None:
    """Print clean CLI execution summary to standard output."""
    mode_str = "DRY RUN" if res.dry_run else "APPLY DB"
    status_str = res.status.upper()

    print(f"Mode: {mode_str}")
    print(f"Status: {status_str}")
    if res.skip_reason:
        print(f"Skip Reason: {res.skip_reason}")
    if res.trading_date:
        print(f"XIST Trading Date: {res.trading_date}")
    print(f"Duration: {res.duration_seconds:.2f}s")
    print(
        f"Requested Active: {res.requested_active_count} | Response: {res.response_count} | "
        f"Valid: {res.valid_count} | Invalid: {res.invalid_count} | Duplicates: {res.duplicate_count}"
    )
    print(
        f"Matched: {res.matched_count} | Missing: {res.missing_count} | Unmatched: {res.unmatched_count} | "
        f"Prev Close Matched: {res.previous_close_matched_count} | Prev Close Missing: {res.previous_close_missing_count}"
    )
    if res.dry_run:
        print(
            f"Would Insert/Update: {res.inserted_or_updated_count} | "
            f"Would Remain Unchanged: {res.unchanged_count} | "
            f"Would Skip As Older: {res.stale_skipped_count}"
        )
        print(
            f"Would Mark price_not_published: {res.price_not_published_stale_count} | "
            f"Would Mark instrument_inactive: {res.instrument_inactive_stale_count}"
        )
        print("Database writes executed: 0 (dry-run)")
        print("Email delivery executed: 0 (dry-run)")
    else:
        print(
            f"Inserted/Updated: {res.inserted_or_updated_count} | "
            f"Unchanged: {res.unchanged_count} | "
            f"Skipped Older: {res.stale_skipped_count}"
        )
        print(
            f"Marked price_not_published: {res.price_not_published_stale_count} | "
            f"Marked instrument_inactive: {res.instrument_inactive_stale_count}"
        )
        if res.email_status:
            print(f"Email Status: {res.email_status.upper()}")

    if res.error:
        print(f"Error: {res.error}")

    if res.warnings:
        print(f"Warnings ({len(res.warnings)}):")
        for w in res.warnings[:10]:
            print(f"  • {w}")
        if len(res.warnings) > 10:
            print(f"  ... and {len(res.warnings) - 10} more warnings")


def run_job(
    argv: Optional[List[str]] = None,
    provider: Optional[YahooBistPriceProvider] = None,
    service: Optional[BistPricesService] = None,
    email_service: Optional[EmailService] = None,
    reference_time: Optional[datetime] = None,
    db_connection_factory: Optional[Any] = None,
    run_context: Optional[JobRunContext] = None,
) -> BistPricesJobResult:
    """Execute the BIST100 latest stock prices synchronization job."""
    parser = argparse.ArgumentParser(
        description="Fetch and synchronize BIST100 stock latest prices from Yahoo Finance to Supabase."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and calculate pricing/stale metrics without database writes or emails (default).",
    )
    modes.add_argument(
        "--apply-db",
        action="store_true",
        help="Apply verified prices and stale statuses to Supabase in a single transaction and email on partial/failed.",
    )
    parser.add_argument("--no-email", action="store_true", help="Disable email delivery.")
    args = parser.parse_args(argv)

    is_dry_run = not args.apply_db
    email_svc = email_service or EmailService()

    runner = TrackedJobRunner(
        spec=JobRunSpecification(
            job_name="bist_latest_prices_job",
            operation_type="prices_sync",
            scope="BIST100",
            provider_code="YAHOO_FINANCE",
        ),
        adapter=BistPricesResultAdapter(),
        renderer=BistPricesEmailRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=email_svc,
        db_connection_factory=db_connection_factory or get_db_connection,
    )

    result = runner.run(
        lambda _ctx: _execute_bist_prices_job(
            apply_db=args.apply_db,
            provider=provider,
            service=service,
            reference_time=reference_time,
            db_connection_factory=db_connection_factory or get_db_connection,
            sanitize_error_message=sanitize_error_message,
        ),
        dry_run=is_dry_run,
        no_email=args.no_email,
        context=run_context,
    )
    _print_summary(result)
    return result

if __name__ == "__main__":
    res = run_job(sys.argv[1:])
    sys.exit(0 if res.status in ("success", "partial", "skipped") else 1)
