import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.core.database import get_db_connection
from app.models.job_tracking import JobRunSpecification
from app.models.tefas_prices import (
    TefasCatalogObservationResult,
    TefasCatalogReconciliationPlan,
    TefasNormalizationResult,
    TefasPricesJobResult,
    TefasPriceSyncItem,
)
from app.providers.tefas.tefas_price_provider import TefasPriceProvider
from app.repositories.tefas_latest_prices_repository import TefasLatestPricesRepository
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.tefas_adapter import (
    TefasPricesResultAdapter,
)
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import (
    TefasPricesEmailRenderer,
)
from app.services.job_tracking.runner import TrackedJobRunner
from app.services.tefas_prices_service import TefasPricesService, plan_price_upserts

logger = logging.getLogger(__name__)


def sanitize_error_message(exc: Exception) -> str:
    """Convert raw exception into a clean, safe, secret-free message."""
    if not exc:
        return "Bilinmeyen bir hata oluştu."
    msg = str(exc)

    # Check for any sensitive credentials/DSN/token/URL patterns FIRST
    lower_msg = msg.lower()
    if any(k in lower_msg for k in ("password", "auth", "credential", "dsn", "secret", "token", "key", "http:", "https:", "://", "@")):
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
) -> TefasPricesJobResult:
    """Execute the daily TEFAS dynamic catalog reconciliation, pricing, and change tracking job."""
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

    is_dry_run = not args.apply_db
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
        lambda: _execute_tefas_prices_job(
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


def _execute_tefas_prices_job(
    args: argparse.Namespace,
    provider: Optional[TefasPriceProvider] = None,
    service: Optional[TefasPricesService] = None,
    email_service: Optional[EmailService] = None,
) -> TefasPricesJobResult:
    is_dry_run = not args.apply_db
    provider = provider or TefasPriceProvider()
    service = service or TefasPricesService()
    email_svc = email_service or EmailService()

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
            with get_db_connection() as conn:
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
            _print_summary(result)
            return result
    else:
        # Apply DB: Single connection and single atomic transaction for read, reconciliation, price upsert, and stale marking
        try:
            with get_db_connection() as conn:
                with conn.transaction():
                    # 1. Read current instruments and stored prices inside transaction
                    db_instruments = TefasLatestPricesRepository.get_all_tefas_instruments(conn)
                    stored_map = TefasLatestPricesRepository.get_stored_latest_prices(conn)
                    stored_price_ids = set(stored_map.keys())

                    # 2. Build reconciliation plan with 3-way price class awareness, disappearance safety & 80% check
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

                    # 3. Apply catalog reconciliation mutations
                    newly_added_ids = TefasLatestPricesRepository.apply_catalog_reconciliation(
                        conn, catalog_plan
                    )

                    # 4. Construct live active instruments map using real UUIDs
                    active_instruments_map = {}
                    deactivated_symbols = set(catalog_plan.to_deactivate_symbols)
                    for code, inst in db_instruments.items():
                        if inst.is_active and code not in deactivated_symbols:
                            active_instruments_map[code] = inst.id
                        elif not inst.is_active and code in catalog_plan.to_reactivate_symbols:
                            active_instruments_map[code] = inst.id

                    for item in catalog_plan.to_add_active:
                        sym = item.fund_code
                        if sym in newly_added_ids:
                            active_instruments_map[sym] = newly_added_ids[sym]

                    # 5. Match valid prices against live active instruments and calculate change metrics
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

                    # 6. Upsert prices with previous_close and change metrics
                    inserted_or_updated_count, unchanged_count, stale_skipped_count = (
                        TefasLatestPricesRepository.upsert_prices(conn, sync_items)
                    )

                    # 7. Mark stale for active funds with missing current price
                    valid_active_symbols = set(p.fund_code for p in valid_current_prices)
                    missing_price_symbols = set(active_instruments_map.keys()) - valid_active_symbols
                    missing_price_inst_ids = [
                        active_instruments_map[s]
                        for s in missing_price_symbols
                        if s in active_instruments_map
                    ]
                    price_not_published_stale_count = TefasLatestPricesRepository.mark_stale_prices(
                        conn, missing_price_inst_ids, "price_not_published"
                    )

                    # 8. Mark stale for deactivated funds (only if any deactivated)
                    instrument_inactive_stale_count = TefasLatestPricesRepository.mark_stale_prices(
                        conn, catalog_plan.to_deactivate_ids, "instrument_inactive"
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
