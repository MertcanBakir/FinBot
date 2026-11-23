import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

from app.core.database import get_db_connection
from app.models.tefas_prices import (
    TefasCatalogObservationResult,
    TefasCatalogReconciliationPlan,
    TefasNormalizationResult,
    TefasPricesJobResult,
    TefasPriceSyncItem,
)
from app.providers.tefas.tefas_price_provider import TefasPriceProvider
from app.repositories.tefas_latest_prices_repository import TefasLatestPricesRepository
from app.services.tefas_price_transaction import apply_tefas_price_transaction
from app.services.tefas_prices_service import TefasPricesService, plan_price_upserts

logger = logging.getLogger(__name__)


def execute_tefas_price_refresh(
    *,
    apply_db: bool,
    provider: Optional[TefasPriceProvider] = None,
    service: Optional[TefasPricesService] = None,
    db_connection_factory: Optional[Any] = None,
    sanitize_error_message: Callable[[Exception], str],
    summary_printer: Callable[[TefasPricesJobResult], None],
) -> TefasPricesJobResult:
    is_dry_run = not apply_db
    connection_factory = db_connection_factory or get_db_connection
    provider = provider or TefasPriceProvider()
    service = service or TefasPricesService()

    started_at = datetime.now(timezone.utc)
    all_warnings: List[str] = []
    provider_called = False

    # 1. Fetch current and previous raw data from TEFAS (outside DB transaction)
    provider_called = True
    try:
        current_df, current_date, prev_df, prev_date = provider.fetch_current_and_previous_prices()
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        logger.error("Failed to fetch TEFAS current prices (type: %s)", type(exc).__name__)
        safe_err = sanitize_error_message(exc)
        return TefasPricesJobResult(
            status="failed",
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=duration,
            error=safe_err,
            dry_run=is_dry_run,
            provider_request_executed=provider_called,
            database_write_executed=False,
        )

    if prev_date is None or prev_df is None:
        all_warnings.append("Could not fetch previous valid TEFAS prices. Change metrics will be NULL.")

    # 2. Extract catalog observations and verify catalog identity completeness
    obs_result: TefasCatalogObservationResult = service.extract_catalog_observations(current_df)
    observed_funds = obs_result.observed_funds
    conflicting_codes = obs_result.conflicting_codes
    identity_invalid_count = obs_result.identity_invalid_count
    identity_complete = obs_result.identity_complete
    all_warnings.extend(obs_result.warnings)

    # 3. Normalize and validate current and previous price rows into 3 explicit classes
    norm_result: TefasNormalizationResult = service.normalize_and_validate_dataframe(
        current_df, data_date=current_date
    )
    valid_current_prices = norm_result.valid_prices
    positive_price_codes = norm_result.positive_price_codes
    clean_nonpositive_price_codes = norm_result.clean_nonpositive_price_codes
    ambiguous_price_codes = norm_result.ambiguous_or_invalid_price_codes
    invalid_count = norm_result.invalid_count
    duplicate_count = norm_result.duplicate_count
    all_warnings.extend(norm_result.warnings)

    valid_prev_map: Dict[str, Decimal] = {}
    if prev_df is not None and prev_date is not None:
        valid_prev_map, prev_val_warns, _, _ = service.normalize_and_validate_previous_prices(
            prev_df, previous_date=prev_date
        )
        all_warnings.extend(prev_val_warns)

    # State variables for execution
    catalog_plan: Optional[TefasCatalogReconciliationPlan] = None
    sync_items: List[TefasPriceSyncItem] = []
    unmatched_count = 0
    missing_price_count = 0
    previous_price_matched_count = 0
    previous_price_missing_count = 0
    inserted_or_updated_count = 0
    unchanged_count = 0
    stale_skipped_count = 0
    price_not_published_stale_count = 0
    instrument_inactive_stale_count = 0

    if is_dry_run:
        # Dry-run: Read DB read-only, build plan with 3-way class awareness & disappearance safety, match prices, calculate realistic stale and upsert plans
        try:
            with connection_factory() as conn:
                db_instruments = TefasLatestPricesRepository.get_all_tefas_instruments(conn)
                stored_map = TefasLatestPricesRepository.get_stored_latest_prices(conn)

            stored_price_ids = set(stored_map.keys())

            catalog_plan = service.build_catalog_reconciliation_plan(
                observed_funds,
                db_instruments,
                conflicting_codes=conflicting_codes,
                positive_price_codes=positive_price_codes,
                clean_nonpositive_price_codes=clean_nonpositive_price_codes,
                ambiguous_price_codes=ambiguous_price_codes,
                stored_price_ids=stored_price_ids,
                identity_complete=identity_complete,
            )

            # Construct dry-run active instruments map (with temporary UUIDs for new additions)
            active_instruments_map: Dict[str, UUID] = {}
            deactivated_symbols = set(catalog_plan.to_deactivate_symbols)
            for code, inst in db_instruments.items():
                if inst.is_active and code not in deactivated_symbols:
                    active_instruments_map[code] = inst.id
                elif not inst.is_active and code in catalog_plan.to_reactivate_symbols:
                    active_instruments_map[code] = inst.id

            for item in catalog_plan.to_add_active:
                active_instruments_map[item.fund_code] = uuid4()

            (
                sync_items,
                match_warns,
                unmatched_count,
                missing_price_count,
                previous_price_matched_count,
                previous_price_missing_count,
            ) = service.match_with_active_instruments(
                valid_current_prices,
                active_instruments_map,
                current_date=current_date,
                valid_previous_prices=valid_prev_map,
            )
            all_warnings.extend(match_warns)

            if not sync_items:
                raise RuntimeError("No valid matched prices available for active TEFAS instruments.")

            # Calculate realistic dry-run price upsert and stale counts
            to_upsert, unchanged_count, stale_skipped_count = plan_price_upserts(stored_map, sync_items)
            inserted_or_updated_count = len(to_upsert)

            valid_active_symbols = set(p.fund_code for p in valid_current_prices)
            missing_price_symbols = set(active_instruments_map.keys()) - valid_active_symbols
            missing_ids = [active_instruments_map[s] for s in missing_price_symbols if s in active_instruments_map]

            price_not_published_stale_count = sum(
                1
                for uid in missing_ids
                if uid in stored_map
                and (not stored_map[uid]["is_stale"] or stored_map[uid]["stale_reason"] != "price_not_published")
            )
            instrument_inactive_stale_count = sum(
                1
                for uid in catalog_plan.to_deactivate_ids
                if uid in stored_map
                and (not stored_map[uid]["is_stale"] or stored_map[uid]["stale_reason"] != "instrument_inactive")
            )

        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()
            logger.error("Dry-run execution failed (type: %s)", type(exc).__name__)
            safe_err = sanitize_error_message(exc)
            result = TefasPricesJobResult(
                status="failed",
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                duration_seconds=duration,
                tefas_data_date=current_date.isoformat(),
                previous_data_date=prev_date.isoformat() if prev_date else None,
                fetched_count=len(current_df),
                valid_count=len(valid_current_prices),
                invalid_count=invalid_count,
                duplicate_count=duplicate_count,
                catalog_observed_count=len(observed_funds),
                catalog_identity_invalid_count=identity_invalid_count,
                warnings=all_warnings,
                error=safe_err,
                dry_run=True,
                provider_request_executed=provider_called,
                database_write_executed=False,
            )
            summary_printer(result)
            return result
    else:
        # Keep TEFAS catalog reconciliation and price writes in one transaction.
        try:
            apply_outcome = apply_tefas_price_transaction(
                connection_factory=connection_factory,
                service=service,
                observed_funds=observed_funds,
                conflicting_codes=conflicting_codes,
                positive_price_codes=positive_price_codes,
                clean_nonpositive_price_codes=clean_nonpositive_price_codes,
                ambiguous_price_codes=ambiguous_price_codes,
                identity_complete=identity_complete,
                valid_current_prices=valid_current_prices,
                current_date=current_date,
                valid_previous_prices=valid_prev_map,
                warning_sink=all_warnings,
            )
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()
            logger.error("Apply-db execution failed (all changes rolled back; type: %s)", type(exc).__name__)
            safe_err = sanitize_error_message(exc)
            return TefasPricesJobResult(
                status="failed",
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                duration_seconds=duration,
                tefas_data_date=current_date.isoformat(),
                previous_data_date=prev_date.isoformat() if prev_date else None,
                fetched_count=len(current_df),
                valid_count=len(valid_current_prices),
                invalid_count=invalid_count,
                duplicate_count=duplicate_count,
                catalog_observed_count=len(observed_funds),
                catalog_identity_invalid_count=identity_invalid_count,
                warnings=all_warnings,
                error=safe_err,
                dry_run=False,
                provider_request_executed=provider_called,
                database_write_executed=False,
            )

        catalog_plan = apply_outcome.catalog_plan
        sync_items = apply_outcome.sync_items
        unmatched_count = apply_outcome.unmatched_count
        missing_price_count = apply_outcome.missing_price_count
        previous_price_matched_count = apply_outcome.previous_price_matched_count
        previous_price_missing_count = apply_outcome.previous_price_missing_count
        inserted_or_updated_count = apply_outcome.inserted_or_updated_count
        unchanged_count = apply_outcome.unchanged_count
        stale_skipped_count = apply_outcome.stale_skipped_count
        price_not_published_stale_count = apply_outcome.price_not_published_stale_count
        instrument_inactive_stale_count = apply_outcome.instrument_inactive_stale_count

    # 4. Determine overall status (previous_price_missing_count > 0 or identity_invalid_count > 0 enforces partial)
    is_partial = (
        invalid_count > 0
        or duplicate_count > 0
        or identity_invalid_count > 0
        or unmatched_count > 0
        or missing_price_count > 0
        or previous_price_missing_count > 0
        or stale_skipped_count > 0
        or prev_date is None
        or (catalog_plan is not None and len(catalog_plan.conflicting_codes) > 0)
        or (catalog_plan is not None and len(catalog_plan.ambiguous_codes) > 0)
        or (catalog_plan is not None and catalog_plan.disappearance_reconciliation_skipped)
        or price_not_published_stale_count > 0
    )
    overall_status = "partial" if is_partial else "success"

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    unpriced_deact_cnt = len(catalog_plan.unpriced_deactivated_ids) if catalog_plan else 0
    disappeared_deact_cnt = len(catalog_plan.disappeared_deactivated_ids) if catalog_plan else 0
    active_add_cnt = len(catalog_plan.to_add_active) if catalog_plan else 0
    unpriced_add_cnt = len(catalog_plan.to_add_inactive) if catalog_plan else 0
    disapp_skipped = catalog_plan.disappearance_reconciliation_skipped if catalog_plan else False
    disapp_reason = catalog_plan.disappearance_skip_reason if catalog_plan else None

    return TefasPricesJobResult(
        status=overall_status,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration_seconds=duration,
        tefas_data_date=current_date.isoformat(),
        previous_data_date=prev_date.isoformat() if prev_date else None,
        fetched_count=len(current_df),
        valid_count=len(valid_current_prices),
        invalid_count=invalid_count,
        duplicate_count=duplicate_count,
        matched_count=len(sync_items),
        unmatched_count=unmatched_count,
        missing_price_count=missing_price_count,
        previous_price_matched_count=previous_price_matched_count,
        previous_price_missing_count=previous_price_missing_count,
        inserted_or_updated_count=inserted_or_updated_count,
        unchanged_count=unchanged_count,
        stale_skipped_count=stale_skipped_count,
        price_not_published_stale_count=price_not_published_stale_count,
        instrument_inactive_stale_count=instrument_inactive_stale_count,
        catalog_observed_count=catalog_plan.observed_count if catalog_plan else len(observed_funds),
        catalog_added_count=len(catalog_plan.to_add) if catalog_plan else 0,
        catalog_active_added_count=active_add_cnt,
        catalog_unpriced_added_count=unpriced_add_cnt,
        catalog_name_updated_count=len(catalog_plan.to_update_names) if catalog_plan else 0,
        catalog_reactivated_count=len(catalog_plan.to_reactivate_ids) if catalog_plan else 0,
        catalog_deactivated_count=len(catalog_plan.to_deactivate_ids) if catalog_plan else 0,
        catalog_unpriced_deactivated_count=unpriced_deact_cnt,
        catalog_disappeared_deactivated_count=disappeared_deact_cnt,
        catalog_identity_invalid_count=identity_invalid_count,
        catalog_disappearance_reconciliation_skipped=disapp_skipped,
        catalog_disappearance_skip_reason=disapp_reason,
        warnings=all_warnings,
        error=None,
        dry_run=is_dry_run,
        provider_request_executed=provider_called,
        database_write_executed=(not is_dry_run),
    )

