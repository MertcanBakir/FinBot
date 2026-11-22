import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional

from app.core.database import get_db_connection
from app.models.crypto_prices import (
    CryptoPricesJobResult,
)
from app.models.job_tracking import JobRunSpecification
from app.providers.crypto.binance_price_provider import (
    BinancePriceProvider,
)
from app.repositories.crypto_latest_prices_repository import (
    CryptoLatestPricesRepository,
)
from app.services.crypto_prices_service import (
    COMPLETENESS_THRESHOLD,
    CryptoPricesService,
    compute_crypto_catalog_fingerprint,
    plan_crypto_price_upserts,
    validate_crypto_catalog,
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

logger = logging.getLogger(__name__)


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
        lambda: _execute_crypto_prices_job(
            args=args,
            provider=provider,
            service=service,
            email_service=email_svc,
        ),
        dry_run=is_dry_run,
        no_email=args.no_email,
    )
    _print_summary(result)
    return result


def _execute_crypto_prices_job(
    args: argparse.Namespace,
    provider: Optional[BinancePriceProvider] = None,
    service: Optional[CryptoPricesService] = None,
    email_service: Optional[EmailService] = None,
) -> CryptoPricesJobResult:
    is_dry_run = not args.apply_db
    provider = provider or BinancePriceProvider()
    service = service or CryptoPricesService()
    email_svc = email_service or EmailService()

    started_at = datetime.now(timezone.utc)
    all_warnings: List[str] = []
    provider_called = False

    # 1. Read crypto catalog from Supabase (read-only, outside transaction)
    try:
        with get_db_connection() as conn:
            all_instruments = CryptoLatestPricesRepository.get_all_crypto_instruments(conn)
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.error("Failed to read CRYPTO instruments from database (error_type=%s)", type(exc).__name__)
        safe_err = sanitize_error_message(exc)
        return CryptoPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            error=safe_err,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    # 2. Validate crypto catalog integrity
    is_cat_valid, cat_errors = validate_crypto_catalog(all_instruments, expected_total_count=100)
    if not is_cat_valid:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        err_msg = f"Catalog validation failed: {'; '.join(cat_errors)}"
        logger.error("Catalog validation failed for CRYPTO instruments: %s", err_msg)
        return CryptoPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            requested_active_count=len(all_instruments),
            warnings=cat_errors,
            error=err_msg,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    initial_fingerprint = compute_crypto_catalog_fingerprint(all_instruments)
    active_instruments = [i for i in all_instruments if i.is_active]
    inactive_instruments = [i for i in all_instruments if not i.is_active]
    requested_active_count = len(active_instruments)
    expected_pairs = {i.source_pair for i in active_instruments}

    # 3. Fetch 24hr ticker data from Binance public API (outside DB transaction)
    provider_called = True
    try:
        raw_tickers = provider.fetch_24hr_tickers(sorted(list(expected_pairs)))
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.error("Failed to fetch Binance 24hr tickers (error_type=%s)", type(exc).__name__)
        safe_err = sanitize_error_message(exc)
        return CryptoPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            requested_active_count=requested_active_count,
            error=safe_err,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    response_count = len(raw_tickers)

    # 4. Normalize and validate ticker responses
    valid_prices, val_warns, invalid_count, duplicate_count, unrequested_pairs = service.normalize_and_validate_tickers(
        raw_tickers, expected_pairs=expected_pairs
    )
    all_warnings.extend(val_warns)

    # 5. Match normalized prices with active instruments
    sync_items, missing_active, unmatched_pairs = service.match_prices_with_instruments(
        active_instruments, valid_prices, unrequested_pairs=unrequested_pairs
    )

    matched_count = len(sync_items)
    missing_count = len(missing_active)
    unmatched_count = len(unmatched_pairs)

    # 6. Evaluate completeness ratio and determine status
    if requested_active_count > 0:
        completeness_ratio = Decimal(matched_count) / Decimal(requested_active_count)
    else:
        completeness_ratio = Decimal("0")

    if completeness_ratio < COMPLETENESS_THRESHOLD or not sync_items:
        status = "failed"
    elif (
        completeness_ratio == Decimal("1.0")
        and matched_count == requested_active_count
        and invalid_count == 0
        and duplicate_count == 0
        and unmatched_count == 0
    ):
        status = "success"
    else:
        status = "partial"
        if missing_count > 0:
            all_warnings.append(
                f"{missing_count} active cryptocurrencies had no valid price from Binance (completeness: {completeness_ratio * 100:.1f}%)."
            )
        if duplicate_count > 0:
            all_warnings.append(f"{duplicate_count} duplicate ticker(s) found in Binance response.")
        if unmatched_count > 0:
            all_warnings.append(f"{unmatched_count} unrequested or unlinked ticker(s) found in Binance response.")
        if invalid_count > 0:
            all_warnings.append(f"{invalid_count} invalid ticker entry/entries skipped.")

    # 7. Execution branch: DRY-RUN vs APPLY-DB
    if is_dry_run:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()

        if status == "failed":
            err_msg = (
                f"Completeness safety check failed: only {matched_count}/{requested_active_count} "
                f"({completeness_ratio * 100:.1f}%) matched, required >= 80%."
            )
            result = CryptoPricesJobResult(
                status="failed",
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                duration_seconds=duration,
                requested_active_count=requested_active_count,
                response_count=response_count,
                valid_count=len(valid_prices),
                invalid_count=invalid_count,
                matched_count=matched_count,
                unmatched_count=unmatched_count,
                duplicate_count=duplicate_count,
                missing_count=missing_count,
                warnings=all_warnings,
                error=err_msg,
                dry_run=True,
                provider_request_executed=provider_called,
                database_write_executed=False,
            )
            _print_summary(result)
            return result

        # Read stored prices read-only to calculate realistic dry-run counters
        try:
            with get_db_connection() as conn:
                stored_map = CryptoLatestPricesRepository.get_stored_latest_prices(conn)
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()
            logger.error("Failed to read stored latest prices in dry-run mode (error_type=%s)", type(exc).__name__)
            safe_err = sanitize_error_message(exc)
            result = CryptoPricesJobResult(
                status="failed",
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                duration_seconds=duration,
                requested_active_count=requested_active_count,
                response_count=response_count,
                valid_count=len(valid_prices),
                invalid_count=invalid_count,
                matched_count=matched_count,
                unmatched_count=unmatched_count,
                duplicate_count=duplicate_count,
                missing_count=missing_count,
                warnings=all_warnings,
                error=safe_err,
                dry_run=True,
                provider_request_executed=provider_called,
                database_write_executed=False,
            )
            _print_summary(result)
            return result

        to_upsert, unchanged_cnt, stale_skip_cnt = plan_crypto_price_upserts(stored_map, sync_items)

        # Calculate realistic would-mark stale counts
        would_mark_pnp = sum(
            1 for inst in missing_active
            if inst.id in stored_map
            and (stored_map[inst.id]["is_stale"] is not True or stored_map[inst.id]["stale_reason"] != "price_not_published")
        )
        would_mark_inactive = sum(
            1 for inst in inactive_instruments
            if inst.id in stored_map
            and (stored_map[inst.id]["is_stale"] is not True or stored_map[inst.id]["stale_reason"] != "instrument_inactive")
        )

        result = CryptoPricesJobResult(
            status=status,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            requested_active_count=requested_active_count,
            response_count=response_count,
            valid_count=len(valid_prices),
            invalid_count=invalid_count,
            matched_count=matched_count,
            unmatched_count=unmatched_count,
            duplicate_count=duplicate_count,
            missing_count=missing_count,
            inserted_or_updated_count=len(to_upsert),
            unchanged_count=unchanged_cnt,
            stale_skipped_count=stale_skip_cnt,
            price_not_published_stale_count=would_mark_pnp,
            instrument_inactive_stale_count=would_mark_inactive,
            warnings=all_warnings,
            dry_run=True,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )
        _print_summary(result)
        return result

    # APPLY-DB branch
    if status == "failed":
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        err_msg = (
            f"Completeness safety check failed: only {matched_count}/{requested_active_count} "
            f"({completeness_ratio * 100:.1f}%) matched, required >= 80%."
        )
        return CryptoPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            requested_active_count=requested_active_count,
            response_count=response_count,
            valid_count=len(valid_prices),
            invalid_count=invalid_count,
            matched_count=matched_count,
            unmatched_count=unmatched_count,
            duplicate_count=duplicate_count,
            missing_count=missing_count,
            warnings=all_warnings,
            error=err_msg,
            dry_run=False,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    # Perform atomic transactional write to Supabase
    inserted_or_updated_count = 0
    unchanged_count = 0
    stale_skipped_count = 0
    marked_pnp_count = 0
    marked_inactive_count = 0

    try:
        with get_db_connection() as conn:
            with conn.transaction():
                # Re-verify catalog has not drifted during network call
                current_insts = CryptoLatestPricesRepository.get_all_crypto_instruments(conn)
                is_curr_valid, curr_errors = validate_crypto_catalog(current_insts, expected_total_count=100)
                if not is_curr_valid:
                    raise RuntimeError(f"Safety check failed: re-validated crypto catalog is invalid ({'; '.join(curr_errors)}).")
                current_fingerprint = compute_crypto_catalog_fingerprint(current_insts)
                if current_fingerprint != initial_fingerprint:
                    raise RuntimeError("Safety check failed: crypto catalog drifted during execution.")

                # Read existing rows in transaction
                stored_map = CryptoLatestPricesRepository.get_stored_latest_prices(conn)

                # Upsert valid prices
                if sync_items:
                    ins_upd, unch, st_skip = CryptoLatestPricesRepository.upsert_prices(
                        conn, sync_items, stored_map=stored_map
                    )
                    inserted_or_updated_count = ins_upd
                    unchanged_count = unch
                    stale_skipped_count = st_skip

                # If partial, mark missing active instruments as price_not_published
                if status == "partial" and missing_active:
                    marked_pnp = CryptoLatestPricesRepository.mark_stale_prices(
                        conn,
                        [i.id for i in missing_active],
                        reason="price_not_published",
                        stored_map=stored_map,
                    )
                    marked_pnp_count = marked_pnp

                # Mark inactive instruments as instrument_inactive
                if inactive_instruments:
                    marked_inact = CryptoLatestPricesRepository.mark_stale_prices(
                        conn,
                        [i.id for i in inactive_instruments],
                        reason="instrument_inactive",
                        stored_map=stored_map,
                    )
                    marked_inactive_count = marked_inact

    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.error("Database transaction failed during crypto prices apply (error_type=%s)", type(exc).__name__)
        safe_err = sanitize_error_message(exc)
        return CryptoPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            requested_active_count=requested_active_count,
            response_count=response_count,
            valid_count=len(valid_prices),
            invalid_count=invalid_count,
            matched_count=matched_count,
            unmatched_count=unmatched_count,
            duplicate_count=duplicate_count,
            missing_count=missing_count,
            warnings=all_warnings,
            error=safe_err,
            dry_run=False,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    return CryptoPricesJobResult(
        status=status,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration_seconds=duration,
        requested_active_count=requested_active_count,
        response_count=response_count,
        valid_count=len(valid_prices),
        invalid_count=invalid_count,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
        duplicate_count=duplicate_count,
        missing_count=missing_count,
        inserted_or_updated_count=inserted_or_updated_count,
        unchanged_count=unchanged_count,
        stale_skipped_count=stale_skipped_count,
        price_not_published_stale_count=marked_pnp_count,
        instrument_inactive_stale_count=marked_inactive_count,
        warnings=all_warnings,
        dry_run=False,
        provider_request_executed=provider_called,
        database_write_executed=True,
    )


if __name__ == "__main__":
    res = run_job(sys.argv[1:])
    sys.exit(0 if res.status in ("success", "partial") else 1)
