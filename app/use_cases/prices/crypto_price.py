import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, List, Optional

from app.core.database import get_db_connection
from app.models.crypto_prices import (
    CryptoPricesJobResult,
)
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
from app.services.price_sync_transaction import apply_price_sync_transaction

logger = logging.getLogger(__name__)


def execute_crypto_price_refresh(
    *,
    apply_db: bool,
    provider: Optional[BinancePriceProvider] = None,
    service: Optional[CryptoPricesService] = None,
    db_connection_factory: Optional[Any] = None,
    sanitize_error_message: Callable[[Exception], str],
    summary_printer: Callable[[CryptoPricesJobResult], None],
) -> CryptoPricesJobResult:
    is_dry_run = not apply_db
    connection_factory = db_connection_factory or get_db_connection
    provider = provider or BinancePriceProvider()
    service = service or CryptoPricesService()

    started_at = datetime.now(timezone.utc)
    all_warnings: List[str] = []
    provider_called = False

    # 1. Read crypto catalog from Supabase (read-only, outside transaction)
    try:
        with connection_factory() as conn:
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
            summary_printer(result)
            return result

        # Read stored prices read-only to calculate realistic dry-run counters
        try:
            with connection_factory() as conn:
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
            summary_printer(result)
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
        summary_printer(result)
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

    try:
        apply_outcome = apply_price_sync_transaction(
            connection_factory=connection_factory,
            catalog_name="crypto",
            catalog_reader=CryptoLatestPricesRepository.get_all_crypto_instruments,
            catalog_validator=lambda instruments: validate_crypto_catalog(
                instruments, expected_total_count=100
            ),
            catalog_fingerprint=compute_crypto_catalog_fingerprint,
            expected_fingerprint=initial_fingerprint,
            stored_price_reader=lambda conn, _instruments: CryptoLatestPricesRepository.get_stored_latest_prices(
                conn
            ),
            upsert_prices=lambda conn, items, stored: CryptoLatestPricesRepository.upsert_prices(
                conn, items, stored_map=stored
            ),
            mark_stale_prices=lambda conn, ids, reason, stored: CryptoLatestPricesRepository.mark_stale_prices(
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

    inserted_or_updated_count = apply_outcome.inserted_or_updated_count
    unchanged_count = apply_outcome.unchanged_count
    stale_skipped_count = apply_outcome.stale_skipped_count
    marked_pnp_count = apply_outcome.price_not_published_stale_count
    marked_inactive_count = apply_outcome.instrument_inactive_stale_count

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

