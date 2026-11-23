import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import settings
from app.core.database import get_db_connection
from app.models.bist_prices import (
    BistPricesJobResult,
)
from app.providers.bist.yahoo_bist_provider import (
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
from app.services.price_sync_transaction import apply_price_sync_transaction

logger = logging.getLogger(__name__)


def execute_bist_price_refresh(
    *,
    apply_db: bool,
    provider: Optional[YahooBistPriceProvider] = None,
    service: Optional[BistPricesService] = None,
    reference_time: Optional[datetime] = None,
    db_connection_factory: Optional[Any] = None,
    sanitize_error_message: Callable[[Exception], str],
) -> BistPricesJobResult:
    is_dry_run = not apply_db
    connection_factory = db_connection_factory or get_db_connection
    provider = provider or YahooBistPriceProvider()
    service = service or BistPricesService()

    started_at = reference_time or datetime.now(timezone.utc)
    all_warnings: List[str] = []
    provider_called = False

    # 1. Read BIST catalog from Supabase (read-only, outside transaction)
    try:
        with connection_factory() as conn:
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
            with connection_factory() as conn:
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

    try:
        apply_outcome = apply_price_sync_transaction(
            connection_factory=connection_factory,
            catalog_name="BIST",
            catalog_reader=BistLatestPricesRepository.get_all_bist_instruments,
            catalog_validator=validate_bist_catalog,
            catalog_fingerprint=compute_bist_catalog_fingerprint,
            expected_fingerprint=initial_fingerprint,
            stored_price_reader=lambda conn, instruments: BistLatestPricesRepository.get_stored_latest_prices(
                conn, instrument_ids=[instrument.id for instrument in instruments]
            ),
            upsert_prices=lambda conn, items, stored: BistLatestPricesRepository.upsert_prices(
                conn, items, stored_map=stored
            ),
            mark_stale_prices=lambda conn, ids, reason, stored: BistLatestPricesRepository.mark_stale_prices(
                conn, ids, reason=reason, stored_map=stored
            ),
            sync_items=sync_items,
            missing_instruments=missing_active,
            inactive_instruments=inactive_instruments,
            status=status,
        )

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

    inserted_or_updated_count = apply_outcome.inserted_or_updated_count
    unchanged_count = apply_outcome.unchanged_count
    stale_skipped_count = apply_outcome.stale_skipped_count
    marked_pnp_count = apply_outcome.price_not_published_stale_count
    marked_inactive_count = apply_outcome.instrument_inactive_stale_count

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

