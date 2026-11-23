"""Transactional apply path shared by fixed-catalog price jobs."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PriceSyncTransactionOutcome:
    inserted_or_updated_count: int = 0
    unchanged_count: int = 0
    stale_skipped_count: int = 0
    price_not_published_stale_count: int = 0
    instrument_inactive_stale_count: int = 0


def apply_price_sync_transaction(
    *,
    connection_factory: Callable[[], Any],
    catalog_name: str,
    catalog_reader: Callable[[Any], Sequence[Any]],
    catalog_validator: Callable[[Sequence[Any]], tuple[bool, list[str]]],
    catalog_fingerprint: Callable[[Sequence[Any]], str],
    expected_fingerprint: str,
    stored_price_reader: Callable[[Any, Sequence[Any]], dict[Any, Any]],
    upsert_prices: Callable[[Any, Sequence[Any], dict[Any, Any]], tuple[int, int, int]],
    mark_stale_prices: Callable[[Any, Sequence[Any], str, dict[Any, Any]], int],
    sync_items: Sequence[Any],
    missing_instruments: Sequence[Any],
    inactive_instruments: Sequence[Any],
    status: str,
) -> PriceSyncTransactionOutcome:
    """Revalidate the catalog and atomically apply prices and stale statuses."""
    inserted_or_updated_count = 0
    unchanged_count = 0
    stale_skipped_count = 0
    price_not_published_stale_count = 0
    instrument_inactive_stale_count = 0

    with connection_factory() as conn:
        with conn.transaction():
            current_instruments = catalog_reader(conn)
            is_valid, validation_errors = catalog_validator(current_instruments)
            if not is_valid:
                errors = "; ".join(validation_errors)
                raise RuntimeError(
                    f"Safety check failed: re-validated {catalog_name} catalog is invalid ({errors})."
                )

            current_fingerprint = catalog_fingerprint(current_instruments)
            if current_fingerprint != expected_fingerprint:
                raise RuntimeError(
                    f"Safety check failed: {catalog_name} catalog drifted during execution."
                )

            stored_prices = stored_price_reader(conn, current_instruments)

            if sync_items:
                (
                    inserted_or_updated_count,
                    unchanged_count,
                    stale_skipped_count,
                ) = upsert_prices(conn, sync_items, stored_prices)

            if status == "partial" and missing_instruments:
                price_not_published_stale_count = mark_stale_prices(
                    conn,
                    [instrument.id for instrument in missing_instruments],
                    "price_not_published",
                    stored_prices,
                )

            if inactive_instruments:
                instrument_inactive_stale_count = mark_stale_prices(
                    conn,
                    [instrument.id for instrument in inactive_instruments],
                    "instrument_inactive",
                    stored_prices,
                )

    return PriceSyncTransactionOutcome(
        inserted_or_updated_count=inserted_or_updated_count,
        unchanged_count=unchanged_count,
        stale_skipped_count=stale_skipped_count,
        price_not_published_stale_count=price_not_published_stale_count,
        instrument_inactive_stale_count=instrument_inactive_stale_count,
    )
