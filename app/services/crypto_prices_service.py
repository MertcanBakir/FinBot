import hashlib
import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from app.models.crypto_prices import (
    CryptoDbInstrument,
    CryptoNormalizedPrice,
    CryptoPriceSyncItem,
)

logger = logging.getLogger(__name__)

PRICE_SCALE = Decimal("0.000000000001")    # 12 decimal places (numeric(30, 12))
PERCENT_SCALE = Decimal("0.00000001")      # 8 decimal places (numeric(20, 8))
COMPLETENESS_THRESHOLD = Decimal("0.80")   # 80% completeness threshold


def compute_crypto_catalog_fingerprint(instruments: Sequence[CryptoDbInstrument]) -> str:
    """Compute deterministic SHA-256 fingerprint for a crypto instruments catalog snapshot.

    Includes: instrument_id, normalized symbol, derived source_pair, currency, and is_active.
    Deterministic sorting by instrument UUID string.
    """
    sorted_insts = sorted(instruments, key=lambda i: str(i.id))
    lines: List[str] = []
    for inst in sorted_insts:
        sym = inst.symbol.strip().upper()
        pair = inst.source_pair
        curr = inst.currency.strip().upper()
        lines.append(f"{inst.id}:{sym}:{pair}:{curr}:{inst.is_active}")
    raw_str = "\n".join(lines)
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


def validate_crypto_catalog(
    instruments: Sequence[CryptoDbInstrument],
    expected_total_count: int = 100,
) -> Tuple[bool, List[str]]:
    """Validate crypto instruments catalog read from public.instruments.

    Enforces:
    - Exactly expected_total_count (100) records present.
    - All IDs unique.
    - All symbols unique.
    - All source pairs unique, non-empty, and end with 'USDT'.
    - All currencies strictly 'USD'.
    - At least one active instrument.

    Returns:
        (is_valid, validation_errors)
    """
    errors: List[str] = []

    if len(instruments) != expected_total_count:
        errors.append(
            f"Catalog count mismatch: expected {expected_total_count} instruments, found {len(instruments)}"
        )

    seen_ids: Set[UUID] = set()
    seen_symbols: Set[str] = set()
    seen_pairs: Set[str] = set()
    active_count = 0

    for inst in instruments:
        if inst.id in seen_ids:
            errors.append(f"Duplicate instrument_id: {inst.id}")
        seen_ids.add(inst.id)

        sym = inst.symbol.strip().upper()
        if not sym:
            errors.append(f"Empty symbol for instrument {inst.id}")
        elif sym in seen_symbols:
            errors.append(f"Duplicate symbol: {sym}")
        seen_symbols.add(sym)

        pair = inst.source_pair
        if not pair:
            errors.append(f"Empty source_pair for symbol {sym}")
        elif not pair.endswith("USDT"):
            errors.append(f"source_pair does not end with 'USDT': {pair}")
        elif pair in seen_pairs:
            errors.append(f"Duplicate source_pair: {pair}")
        seen_pairs.add(pair)

        if inst.currency != "USD":
            errors.append(f"Invalid currency '{inst.currency}' for symbol {sym}; expected 'USD'")

        if inst.is_active:
            active_count += 1

    if active_count == 0:
        errors.append("No active CRYPTO instruments found in catalog")

    return len(errors) == 0, errors


def plan_crypto_price_upserts(
    stored_map: Dict[UUID, dict],
    incoming_items: Sequence[CryptoPriceSyncItem],
) -> Tuple[List[CryptoPriceSyncItem], int, int]:
    """Calculate the exact list of items to upsert and count unchanged / stale skipped items.

    Enforces idempotent merge rules:
    - If incoming price_at is newer than stored: write incoming (clear stale).
    - If incoming price_at matches stored:
      - Compare all fields (price, previous_close, change_amount, change_percent, currency, provider_code, is_stale).
      - If different or stored was stale: upsert item (clear stale).
      - If identical: mark as unchanged (no-op).
    - If incoming price_at is older than stored: skip as older (stale_skipped_count += 1).

    Returns:
        (to_upsert_items, unchanged_count, stale_skipped_count)
    """
    to_upsert: List[CryptoPriceSyncItem] = []
    unchanged_count = 0
    stale_skipped_count = 0

    for item in incoming_items:
        stored = stored_map.get(item.instrument_id)
        if stored is None:
            to_upsert.append(item)
            continue

        inc_at = item.price_at
        stored_at = stored["price_at"]

        if inc_at > stored_at:
            to_upsert.append(item)
        elif inc_at == stored_at:
            is_different = (
                item.price != stored["price"]
                or item.previous_close != stored["previous_close"]
                or item.change_amount != stored["change_amount"]
                or item.change_percent != stored["change_percent"]
                or item.currency != stored["currency"]
                or item.provider_code != stored["provider_code"]
                or stored["is_stale"] is True
                or stored["stale_reason"] is not None
            )

            if is_different:
                to_upsert.append(item)
            else:
                unchanged_count += 1
        else:
            # inc_at < stored_at: incoming is older than stored
            stale_skipped_count += 1

    return to_upsert, unchanged_count, stale_skipped_count


class CryptoPricesService:
    """Core domain service for Binance crypto prices validation, calculation, and matching."""

    @staticmethod
    def normalize_and_validate_tickers(
        tickers: Sequence[dict],
        expected_pairs: Set[str],
    ) -> Tuple[List[CryptoNormalizedPrice], List[str], int, int, List[str]]:
        """Normalize raw Binance 24hr ticker dicts and perform deterministic Decimal calculations.

        Returns:
            (valid_prices, warnings, invalid_count, duplicate_count, unrequested_pairs)
        """
        valid_prices: List[CryptoNormalizedPrice] = []
        warnings: List[str] = []
        unrequested_pairs: List[str] = []
        invalid_count = 0
        duplicate_count = 0
        seen_pairs: Set[str] = set()

        for ticker in tickers:
            if not isinstance(ticker, dict):
                invalid_count += 1
                warnings.append("Malformed ticker entry skipped (not a dict).")
                continue

            pair = str(ticker.get("symbol") or "").strip().upper()
            if not pair:
                invalid_count += 1
                warnings.append("Ticker entry missing 'symbol' skipped.")
                continue

            if expected_pairs and pair not in expected_pairs:
                unrequested_pairs.append(pair)
                warnings.append(f"Unrequested ticker symbol '{pair}' skipped.")
                continue

            if pair in seen_pairs:
                duplicate_count += 1
                warnings.append(f"Duplicate ticker for pair '{pair}' skipped.")
                continue

            raw_last_price = ticker.get("lastPrice")
            raw_open_price = ticker.get("openPrice")
            raw_close_time = ticker.get("closeTime")

            try:
                if raw_last_price is None or raw_open_price is None or raw_close_time is None:
                    raise ValueError("Missing required price or closeTime fields")

                last_price = Decimal(str(raw_last_price))
                open_price = Decimal(str(raw_open_price))

                if not last_price.is_finite() or last_price <= 0:
                    raise ValueError(f"lastPrice '{raw_last_price}' is non-positive or non-finite")
                if not open_price.is_finite() or open_price <= 0:
                    raise ValueError(f"openPrice '{raw_open_price}' is non-positive or non-finite")

                close_time_ms = int(raw_close_time)
                if close_time_ms <= 0:
                    raise ValueError(f"closeTime '{raw_close_time}' is non-positive")

                # Integer division to get seconds and milliseconds without float precision loss
                seconds, ms = divmod(close_time_ms, 1000)
                # Platform datetime upper limit guard (~ year 9999 is 253402300799 seconds)
                if not (0 < seconds < 253402300799):
                    raise OverflowError(f"closeTime '{close_time_ms}' is out of platform datetime range")

                price_at_dt = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=ms * 1000)

                # Deterministic Decimal calculations
                price_dec = last_price.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                prev_close_dec = open_price.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                change_amt_dec = (last_price - open_price).quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                change_pct_dec = (((last_price - open_price) / open_price) * Decimal("100")).quantize(
                    PERCENT_SCALE, rounding=ROUND_HALF_UP
                )

                # Derive base symbol
                base_symbol = pair[:-4] if pair.endswith("USDT") else pair

                item = CryptoNormalizedPrice(
                    symbol=base_symbol,
                    source_pair=pair,
                    price=price_dec,
                    previous_close=prev_close_dec,
                    change_amount=change_amt_dec,
                    change_percent=change_pct_dec,
                    price_at=price_at_dt,
                    currency="USD",
                    provider_code="BINANCE",
                )

                valid_prices.append(item)
                seen_pairs.add(pair)

            except (InvalidOperation, ValueError, TypeError, OverflowError, OSError) as exc:
                invalid_count += 1
                warnings.append(f"Pair '{pair}': invalid price or timestamp data ({type(exc).__name__}).")

        return valid_prices, warnings, invalid_count, duplicate_count, unrequested_pairs

    @staticmethod
    def match_prices_with_instruments(
        active_instruments: Sequence[CryptoDbInstrument],
        normalized_prices: Sequence[CryptoNormalizedPrice],
        unrequested_pairs: Optional[Sequence[str]] = None,
    ) -> Tuple[List[CryptoPriceSyncItem], List[CryptoDbInstrument], List[str]]:
        """Match normalized Binance prices with active Supabase crypto instruments.

        Returns:
            (matched_sync_items, missing_active_instruments, unmatched_pairs)
        """
        price_map: Dict[str, CryptoNormalizedPrice] = {
            p.source_pair: p for p in normalized_prices
        }

        matched_items: List[CryptoPriceSyncItem] = []
        missing_active: List[CryptoDbInstrument] = []
        matched_pairs: Set[str] = set()

        for inst in active_instruments:
            pair = inst.source_pair
            price_entry = price_map.get(pair)
            if price_entry is not None:
                matched_items.append(
                    CryptoPriceSyncItem(
                        instrument_id=inst.id,
                        symbol=inst.symbol,
                        source_pair=pair,
                        price=price_entry.price,
                        currency=price_entry.currency,
                        previous_close=price_entry.previous_close,
                        change_amount=price_entry.change_amount,
                        change_percent=price_entry.change_percent,
                        price_at=price_entry.price_at,
                        provider_code=price_entry.provider_code,
                        is_stale=False,
                        stale_reason=None,
                    )
                )
                matched_pairs.add(pair)
            else:
                missing_active.append(inst)

        unmatched = list(unrequested_pairs or [])
        for p in normalized_prices:
            if p.source_pair not in matched_pairs and p.source_pair not in unmatched:
                unmatched.append(p.source_pair)

        return matched_items, missing_active, unmatched
