"""Atomic TEFAS catalog reconciliation and price application."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.models.tefas_prices import (
    TefasCatalogReconciliationPlan,
    TefasPriceSyncItem,
)
from app.repositories.tefas_latest_prices_repository import TefasLatestPricesRepository
from app.services.tefas_prices_service import TefasPricesService


@dataclass(frozen=True)
class TefasPriceTransactionOutcome:
    catalog_plan: TefasCatalogReconciliationPlan
    sync_items: list[TefasPriceSyncItem]
    unmatched_count: int
    missing_price_count: int
    previous_price_matched_count: int
    previous_price_missing_count: int
    inserted_or_updated_count: int
    unchanged_count: int
    stale_skipped_count: int
    price_not_published_stale_count: int
    instrument_inactive_stale_count: int


def apply_tefas_price_transaction(
    *,
    connection_factory: Callable[[], Any],
    service: TefasPricesService,
    observed_funds: Sequence[Any],
    conflicting_codes: Sequence[str],
    positive_price_codes: set[str],
    clean_nonpositive_price_codes: set[str],
    ambiguous_price_codes: set[str],
    identity_complete: bool,
    valid_current_prices: Sequence[Any],
    current_date: date,
    valid_previous_prices: Mapping[str, Any],
    warning_sink: list[str],
) -> TefasPriceTransactionOutcome:
    """Read, reconcile, and write TEFAS catalog/prices atomically."""
    with connection_factory() as conn:
        with conn.transaction():
            db_instruments = TefasLatestPricesRepository.get_all_tefas_instruments(conn)
            stored_prices = TefasLatestPricesRepository.get_stored_latest_prices(conn)
            stored_price_ids = set(stored_prices)

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

            newly_added_ids = TefasLatestPricesRepository.apply_catalog_reconciliation(
                conn, catalog_plan
            )

            active_instruments: dict[str, Any] = {}
            deactivated_symbols = set(catalog_plan.to_deactivate_symbols)
            for code, instrument in db_instruments.items():
                if instrument.is_active and code not in deactivated_symbols:
                    active_instruments[code] = instrument.id
                elif not instrument.is_active and code in catalog_plan.to_reactivate_symbols:
                    active_instruments[code] = instrument.id

            for item in catalog_plan.to_add_active:
                if item.fund_code in newly_added_ids:
                    active_instruments[item.fund_code] = newly_added_ids[item.fund_code]

            (
                sync_items,
                match_warnings,
                unmatched_count,
                missing_price_count,
                previous_price_matched_count,
                previous_price_missing_count,
            ) = service.match_with_active_instruments(
                valid_current_prices,
                active_instruments,
                current_date=current_date,
                valid_previous_prices=valid_previous_prices,
            )
            warning_sink.extend(match_warnings)
            if not sync_items:
                raise RuntimeError("No valid matched prices available for active TEFAS instruments.")

            inserted_or_updated_count, unchanged_count, stale_skipped_count = (
                TefasLatestPricesRepository.upsert_prices(conn, sync_items)
            )

            valid_active_symbols = {price.fund_code for price in valid_current_prices}
            missing_price_symbols = set(active_instruments) - valid_active_symbols
            missing_price_ids = [
                active_instruments[symbol]
                for symbol in missing_price_symbols
                if symbol in active_instruments
            ]
            price_not_published_stale_count = TefasLatestPricesRepository.mark_stale_prices(
                conn, missing_price_ids, "price_not_published"
            )
            instrument_inactive_stale_count = TefasLatestPricesRepository.mark_stale_prices(
                conn, catalog_plan.to_deactivate_ids, "instrument_inactive"
            )

    return TefasPriceTransactionOutcome(
        catalog_plan=catalog_plan,
        sync_items=sync_items,
        unmatched_count=unmatched_count,
        missing_price_count=missing_price_count,
        previous_price_matched_count=previous_price_matched_count,
        previous_price_missing_count=previous_price_missing_count,
        inserted_or_updated_count=inserted_or_updated_count,
        unchanged_count=unchanged_count,
        stale_skipped_count=stale_skipped_count,
        price_not_published_stale_count=price_not_published_stale_count,
        instrument_inactive_stale_count=instrument_inactive_stale_count,
    )
