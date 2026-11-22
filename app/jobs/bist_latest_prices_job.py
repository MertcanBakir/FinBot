import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import settings
from app.core.database import get_db_connection
from app.models.bist_prices import (
    BistPricesJobResult,
)
from app.models.job_tracking import JobRunSpecification
from app.providers.bist.yahoo_bist_provider import (
    YahooBistCalendarError,
    YahooBistPriceFetchError,
    YahooBistPriceProvider,
)
from app.repositories.bist_latest_prices_repository import BistLatestPricesRepository
from app.services.bist_prices_service import (
    COMPLETENESS_THRESHOLD,
    BistPricesService,
    compute_bist_catalog_fingerprint,
    plan_bist_price_upserts,
    validate_bist_catalog,
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

logger = logging.getLogger(__name__)


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
        lambda: _execute_bist_prices_job(
            args=args,
            provider=provider,
            service=service,
            email_service=email_svc,
            reference_time=reference_time,
        ),
        dry_run=is_dry_run,
        no_email=args.no_email,
    )
    _print_summary(result)
    return result


def _execute_bist_prices_job(
    args: argparse.Namespace,
    provider: Optional[YahooBistPriceProvider] = None,
    service: Optional[BistPricesService] = None,
    email_service: Optional[EmailService] = None,
    reference_time: Optional[datetime] = None,
) -> BistPricesJobResult:
    is_dry_run = not args.apply_db
    provider = provider or YahooBistPriceProvider()
    service = service or BistPricesService()
    email_svc = email_service or EmailService()

    started_at = reference_time or datetime.now(timezone.utc)
    all_warnings: List[str] = []
    provider_called = False

    # 1. Read BIST catalog from Supabase (read-only, outside transaction)
    try:
        with get_db_connection() as conn:
            all_instruments = BistLatestPricesRepository.get_all_bist_instruments(conn)
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.error("Failed to read BIST instruments from database (error_type=%s)", type(exc).__name__)
        safe_err = sanitize_error_message(exc)
        return BistPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            error=safe_err,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    # 2. Validate BIST catalog integrity
    is_cat_valid, cat_errors = validate_bist_catalog(all_instruments)
    if not is_cat_valid:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        err_msg = f"Catalog validation failed: {'; '.join(cat_errors)}"
        logger.error("Catalog validation failed for BIST instruments: %s", err_msg)
        active_cnt = sum(1 for i in all_instruments if i.is_active)
        return BistPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            requested_active_count=active_cnt,
            warnings=cat_errors,
            error=err_msg,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    initial_fingerprint = compute_bist_catalog_fingerprint(all_instruments)
    active_instruments = [i for i in all_instruments if i.is_active]
    inactive_instruments = [i for i in all_instruments if not i.is_active]
    requested_active_count = len(active_instruments)

    # 3. Query XIST trading calendar with explicit exception distinction
    now_istanbul = started_at.astimezone(ZoneInfo(settings.TIMEZONE))
    target_trading_date = now_istanbul.date()

    try:
        session_info = provider.get_xist_session(target_trading_date)
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.error("Failed to query XIST calendar (error_type=%s)", type(exc).__name__)
        safe_err = sanitize_error_message(exc)
        return BistPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            trading_date=target_trading_date.isoformat(),
            requested_active_count=requested_active_count,
            error=safe_err,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    if not session_info.is_trading_day:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.info(
            "XIST calendar indicates market is closed today (%s). Skipping BIST price sync.",
            target_trading_date,
        )
        return BistPricesJobResult(
            status="skipped",
            skip_reason="market_closed",
            trading_date=target_trading_date.isoformat(),
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            requested_active_count=requested_active_count,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    expected_tickers = {i.yahoo_ticker for i in active_instruments}

    # 4. Fetch batch intraday data (isolated from daily)
    provider_called = True
    try:
        intraday_df = provider.fetch_intraday(sorted(list(expected_tickers)))
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.error("Failed to fetch BIST intraday prices from Yahoo Finance (error_type=%s)", type(exc).__name__)
        safe_err = sanitize_error_message(exc)
        return BistPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            trading_date=target_trading_date.isoformat(),
            requested_active_count=requested_active_count,
            error=safe_err,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    # 5. Fetch batch daily data (isolated error handling)
    try:
        daily_df = provider.fetch_daily(sorted(list(expected_tickers)))
    except Exception as exc:
        logger.warning("Daily data fetch failed; proceeding with current prices only (error_type=%s)", type(exc).__name__)
        all_warnings.append("Daily historical prices could not be downloaded from Yahoo Finance.")
        daily_df = pd.DataFrame()

    # 6. Normalize and validate ticker responses
    (
        valid_prices,
        val_warns,
        invalid_count,
        duplicate_count,
        unrequested_tickers,
        prev_matched_count,
        prev_missing_count,
        observed_ticker_count,
    ) = service.normalize_bist_prices(
        intraday_df=intraday_df,
        daily_df=daily_df,
        expected_tickers=expected_tickers,
        trading_date=target_trading_date,
        session_info=session_info,
        reference_time=started_at,
    )
    all_warnings.extend(val_warns)

    # 7. Match normalized prices with active instruments
    sync_items, missing_active, unmatched_tickers = service.match_prices_with_instruments(
        active_instruments, valid_prices, unrequested_tickers=unrequested_tickers
    )

    matched_count = len(sync_items)
    missing_count = len(missing_active)
    unmatched_count = len(unmatched_tickers)

    # 8. Evaluate completeness ratio and determine status
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
        and prev_missing_count == 0
    ):
        status = "success"
    else:
        status = "partial"
        if missing_count > 0:
            all_warnings.append(
                f"{missing_count} active BIST stock(s) had no valid price from Yahoo Finance (completeness: {completeness_ratio * 100:.1f}%)."
            )
        if duplicate_count > 0:
            all_warnings.append(f"{duplicate_count} duplicate ticker(s) encountered in Yahoo Finance response.")
        if unmatched_count > 0:
            all_warnings.append(f"{unmatched_count} unrequested ticker(s) encountered in Yahoo Finance response.")
        if invalid_count > 0:
            all_warnings.append(f"{invalid_count} invalid or incomplete ticker price entry/entries skipped.")
        if prev_missing_count > 0:
            all_warnings.append(f"{prev_missing_count} stock(s) missing previous close data.")

    # 9. Execution branch: DRY-RUN vs APPLY-DB
    if is_dry_run:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()

        if status == "failed":
            err_msg = (
                f"Completeness safety check failed: only {matched_count}/{requested_active_count} "
                f"({completeness_ratio * 100:.1f}%) matched, required >= 80%."
            )
            return BistPricesJobResult(
                status="failed",
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                duration_seconds=duration,
                trading_date=target_trading_date.isoformat(),
                requested_active_count=requested_active_count,
                observed_ticker_count=observed_ticker_count,
                response_count=observed_ticker_count,
                valid_count=len(valid_prices),
                invalid_count=invalid_count,
                matched_count=matched_count,
                unmatched_count=unmatched_count,
                duplicate_count=duplicate_count,
                missing_count=missing_count,
                previous_close_matched_count=prev_matched_count,
                previous_close_missing_count=prev_missing_count,
                warnings=all_warnings,
                error=err_msg,
                dry_run=True,
                provider_request_executed=provider_called,
                database_write_executed=False,
            )

        # Read stored prices restricted to BIST instruments (read-only)
        try:
            with get_db_connection() as conn:
                stored_map = BistLatestPricesRepository.get_stored_latest_prices(
                    conn, instrument_ids=[i.id for i in all_instruments]
                )
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()
            logger.error("Failed to read stored latest prices in dry-run mode (error_type=%s)", type(exc).__name__)
            safe_err = sanitize_error_message(exc)
            return BistPricesJobResult(
                status="failed",
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                duration_seconds=duration,
                trading_date=target_trading_date.isoformat(),
                requested_active_count=requested_active_count,
                observed_ticker_count=observed_ticker_count,
                response_count=observed_ticker_count,
                valid_count=len(valid_prices),
                invalid_count=invalid_count,
                matched_count=matched_count,
                unmatched_count=unmatched_count,
                duplicate_count=duplicate_count,
                missing_count=missing_count,
                previous_close_matched_count=prev_matched_count,
                previous_close_missing_count=prev_missing_count,
                warnings=all_warnings,
                error=safe_err,
                dry_run=True,
                provider_request_executed=provider_called,
                database_write_executed=False,
            )

        to_upsert, unchanged_cnt, stale_skip_cnt = plan_bist_price_upserts(stored_map, sync_items)

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

        return BistPricesJobResult(
            status=status,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            trading_date=target_trading_date.isoformat(),
            requested_active_count=requested_active_count,
            observed_ticker_count=observed_ticker_count,
            response_count=observed_ticker_count,
            valid_count=len(valid_prices),
            invalid_count=invalid_count,
            matched_count=matched_count,
            unmatched_count=unmatched_count,
            duplicate_count=duplicate_count,
            missing_count=missing_count,
            previous_close_matched_count=prev_matched_count,
            previous_close_missing_count=prev_missing_count,
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

    # APPLY-DB branch
    if status == "failed":
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        err_msg = (
            f"Completeness safety check failed: only {matched_count}/{requested_active_count} "
            f"({completeness_ratio * 100:.1f}%) matched, required >= 80%."
        )
        return BistPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            trading_date=target_trading_date.isoformat(),
            requested_active_count=requested_active_count,
            observed_ticker_count=observed_ticker_count,
            response_count=observed_ticker_count,
            valid_count=len(valid_prices),
            invalid_count=invalid_count,
            matched_count=matched_count,
            unmatched_count=unmatched_count,
            duplicate_count=duplicate_count,
            missing_count=missing_count,
            previous_close_matched_count=prev_matched_count,
            previous_close_missing_count=prev_missing_count,
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
                current_insts = BistLatestPricesRepository.get_all_bist_instruments(conn)
                is_curr_valid, curr_errors = validate_bist_catalog(current_insts)
                if not is_curr_valid:
                    raise RuntimeError(f"Safety check failed: re-validated BIST catalog is invalid ({'; '.join(curr_errors)}).")
                current_fingerprint = compute_bist_catalog_fingerprint(current_insts)
                if current_fingerprint != initial_fingerprint:
                    raise RuntimeError("Safety check failed: BIST catalog drifted during execution.")

                # Read existing rows in transaction restricted to BIST instruments
                stored_map = BistLatestPricesRepository.get_stored_latest_prices(
                    conn, instrument_ids=[i.id for i in current_insts]
                )

                # Upsert valid prices
                if sync_items:
                    ins_upd, unch, st_skip = BistLatestPricesRepository.upsert_prices(
                        conn, sync_items, stored_map=stored_map
                    )
                    inserted_or_updated_count = ins_upd
                    unchanged_count = unch
                    stale_skipped_count = st_skip

                # If partial, mark missing active instruments as price_not_published
                if status == "partial" and missing_active:
                    marked_pnp = BistLatestPricesRepository.mark_stale_prices(
                        conn,
                        [i.id for i in missing_active],
                        reason="price_not_published",
                        stored_map=stored_map,
                    )
                    marked_pnp_count = marked_pnp

                # Mark inactive instruments as instrument_inactive
                if inactive_instruments:
                    marked_inact = BistLatestPricesRepository.mark_stale_prices(
                        conn,
                        [i.id for i in inactive_instruments],
                        reason="instrument_inactive",
                        stored_map=stored_map,
                    )
                    marked_inactive_count = marked_inact

    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.error("Database transaction failed during BIST prices apply (error_type=%s)", type(exc).__name__)
        safe_err = sanitize_error_message(exc)
        return BistPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            trading_date=target_trading_date.isoformat(),
            requested_active_count=requested_active_count,
            observed_ticker_count=observed_ticker_count,
            response_count=observed_ticker_count,
            valid_count=len(valid_prices),
            invalid_count=invalid_count,
            matched_count=matched_count,
            unmatched_count=unmatched_count,
            duplicate_count=duplicate_count,
            missing_count=missing_count,
            previous_close_matched_count=prev_matched_count,
            previous_close_missing_count=prev_missing_count,
            warnings=all_warnings,
            error=safe_err,
            dry_run=False,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    return BistPricesJobResult(
        status=status,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration_seconds=duration,
        trading_date=target_trading_date.isoformat(),
        requested_active_count=requested_active_count,
        observed_ticker_count=observed_ticker_count,
        response_count=observed_ticker_count,
        valid_count=len(valid_prices),
        invalid_count=invalid_count,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
        duplicate_count=duplicate_count,
        missing_count=missing_count,
        previous_close_matched_count=prev_matched_count,
        previous_close_missing_count=prev_missing_count,
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
    sys.exit(0 if res.status in ("success", "partial", "skipped") else 1)
