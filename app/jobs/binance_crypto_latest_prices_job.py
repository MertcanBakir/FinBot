import argparse
import sys
from typing import Any, List, Optional

from app.core.database import get_db_connection
from app.models.crypto_prices import (
    CryptoPricesJobResult,
)
from app.models.job_tracking import JobRunSpecification
from app.providers.crypto.binance_price_provider import (
    BinancePriceProvider,
)
from app.services.crypto_prices_service import (
    CryptoPricesService,
)
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.crypto_adapter import (
    BinanceCryptoPricesResultAdapter,
)
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import (
    BinanceCryptoPricesEmailRenderer,
)
from app.services.job_tracking.runner import TrackedJobRunner
from app.use_cases.prices.crypto_price import (
    execute_crypto_price_refresh as _execute_crypto_prices_job,
)


def sanitize_error_message(exc: Exception) -> str:
    """Convert raw exception into a clean, safe, secret-free message."""
    if not exc:
        return "Bilinmeyen bir hata oluştu."
    msg = str(exc)

    lower_msg = msg.lower()
    if any(k in lower_msg for k in ("password", "auth", "credential", "dsn", "secret", "token", "key", "http:", "https:", "://", "@")):
        return "Veritabanı veya servis erişim hatası."

    if isinstance(exc, (ValueError, RuntimeError)):
        if "Safety check failed" in msg:
            return msg
        if "Catalog count mismatch" in msg or "Catalog validation failed" in msg:
            return msg
        if "No active CRYPTO" in msg or "No valid matched prices" in msg:
            return msg
        if "Completeness safety check failed" in msg:
            return msg

    if "timeout" in lower_msg or "timed out" in lower_msg:
        return "Bağlantı zaman aşımına uğradı."
    if "connection" in lower_msg or "could not connect" in lower_msg or "unreachable" in lower_msg:
        return "Veritabanı bağlantısı kurulamadı."
    if "transaction" in lower_msg or "rollback" in lower_msg:
        return "Veritabanı işlem (transaction) hatası."
    return "Operasyonel hata oluştu."


def _print_summary(res: CryptoPricesJobResult) -> None:
    """Print clean CLI execution summary to standard output."""
    mode_str = "DRY RUN" if res.dry_run else "APPLY DB"
    status_str = res.status.upper()

    print(f"Mode: {mode_str}")
    print(f"Status: {status_str}")
    print(f"Duration: {res.duration_seconds:.2f}s")
    print(
        f"Requested Active: {res.requested_active_count} | Response: {res.response_count} | "
        f"Valid: {res.valid_count} | Invalid: {res.invalid_count} | Duplicates: {res.duplicate_count}"
    )
    print(
        f"Matched: {res.matched_count} | Missing: {res.missing_count} | Unmatched: {res.unmatched_count}"
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
    provider: Optional[BinancePriceProvider] = None,
    service: Optional[CryptoPricesService] = None,
    email_service: Optional[EmailService] = None,
    db_connection_factory: Optional[Any] = None,
) -> CryptoPricesJobResult:
    """Execute the Binance cryptocurrency latest prices synchronization job."""
    parser = argparse.ArgumentParser(
        description="Fetch and synchronize 24hr cryptocurrency latest prices from Binance to Supabase."
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
            job_name="binance_crypto_latest_prices_job",
            operation_type="prices_sync",
            scope="CRYPTO",
            provider_code="BINANCE",
        ),
        adapter=BinanceCryptoPricesResultAdapter(),
        renderer=BinanceCryptoPricesEmailRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=email_svc,
        db_connection_factory=db_connection_factory or get_db_connection,
    )

    result = runner.run(
        lambda _ctx: _execute_crypto_prices_job(
            apply_db=args.apply_db,
            provider=provider,
            service=service,
            db_connection_factory=db_connection_factory or get_db_connection,
            sanitize_error_message=sanitize_error_message,
            summary_printer=_print_summary,
        ),
        dry_run=is_dry_run,
        no_email=args.no_email,
    )
    _print_summary(result)
    return result

if __name__ == "__main__":
    res = run_job(sys.argv[1:])
    sys.exit(0 if res.status in ("success", "partial") else 1)
