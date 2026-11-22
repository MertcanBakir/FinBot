import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import settings
from app.models.bist_prices import (
    BistDbInstrument,
    BistNormalizedPrice,
    BistPriceSyncItem,
    XistSessionInfo,
)

logger = logging.getLogger(__name__)

PRICE_SCALE = Decimal("0.000000000001")      # 12 decimal places (numeric(30, 12))
PERCENT_SCALE = Decimal("0.00000001")        # 8 decimal places (numeric(20, 8))
COMPLETENESS_THRESHOLD = Decimal("0.80")     # 80% completeness threshold

# Tightened Freshness tolerances
MAX_INTRADAY_DELAY_MINUTES = 60              # Max allowable delay for quote during active session (60m)
FUTURE_TOLERANCE_MINUTES = 5                 # Clock skew tolerance (5m)
POST_SESSION_MAX_EARLY_MINUTES = 60          # Max distance before market_close for closing bar after session (60m)
POST_SESSION_MAX_LATE_MINUTES = 15           # Max distance after market_close for closing bar (15m)
PRE_SESSION_MAX_EARLY_MINUTES = 15           # Bar cannot be earlier than open - 15m


KNOWN_PRICE_FIELDS = {"OPEN", "HIGH", "LOW", "CLOSE", "ADJ CLOSE", "VOLUME"}


def compute_bist_catalog_fingerprint(instruments: Sequence[BistDbInstrument]) -> str:
    """Compute deterministic SHA-256 fingerprint for a BIST instruments catalog snapshot.

    Includes: instrument_id, normalized symbol, derived yahoo_ticker, currency, and is_active.
    Deterministic sorting by instrument UUID string.
    """
    sorted_insts = sorted(instruments, key=lambda i: str(i.id))
    lines: List[str] = []
    for inst in sorted_insts:
        sym = inst.symbol.strip().upper()
        ticker = inst.yahoo_ticker
        curr = inst.currency.strip().upper()
        lines.append(f"{inst.id}:{sym}:{ticker}:{curr}:{inst.is_active}")
    raw_str = "\n".join(lines)
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


BIST_SYMBOL_REGEX = re.compile(r"^[A-Z0-9]{1,10}$")


def is_valid_bist_symbol(sym: str) -> bool:
    """Validate BIST stock symbol format for database storage.

    Rules:
    - Stripped and uppercase.
    - Length between 1 and 10 alphanumeric characters.
    - Contains ONLY uppercase ASCII letters and digits [A-Z0-9].
    - Must NOT contain '.IS' suffix, spaces, slashes, dots, hyphens or punctuation.
    """
    if not sym or not isinstance(sym, str):
        return False
    if sym.endswith(".IS") or "." in sym or "-" in sym or "/" in sym:
        return False
    return bool(BIST_SYMBOL_REGEX.match(sym))


def validate_bist_catalog(
    instruments: Sequence[BistDbInstrument],
) -> Tuple[bool, List[str]]:
    """Validate BIST instruments catalog read from public.instruments.

    Enforces:
    - Non-empty catalog (len(instruments) > 0).
    - All IDs unique UUIDs.
    - All symbols unique, non-empty, and valid BIST symbol format (no '.IS' in database symbol).
    - All derived Yahoo tickers unique, non-empty, and end with '.IS'.
    - All currencies strictly 'TRY'.
    - At least one active instrument (active_count > 0).

    Returns:
        (is_valid, validation_errors)
    """
    errors: List[str] = []

    if not instruments:
        errors.append("Catalog is empty: 0 instruments found")

    seen_ids: Set[UUID] = set()
    seen_symbols: Set[str] = set()
    seen_tickers: Set[str] = set()
    active_count = 0

    for inst in instruments:
        if inst.id in seen_ids:
            errors.append(f"Duplicate instrument_id: {inst.id}")
        seen_ids.add(inst.id)

        raw_sym = inst.symbol if inst.symbol is not None else ""
        sym = raw_sym.strip().upper()
        if not sym:
            errors.append(f"Empty symbol for instrument {inst.id}")
        elif not is_valid_bist_symbol(sym):
            errors.append(f"Invalid BIST symbol format '{raw_sym}' for instrument {inst.id}")
        elif sym in seen_symbols:
            errors.append(f"Duplicate symbol: {sym}")
        seen_symbols.add(sym)

        ticker = inst.yahoo_ticker
        if not ticker:
            errors.append(f"Empty yahoo_ticker for symbol {sym}")
        elif not ticker.endswith(".IS"):
            errors.append(f"yahoo_ticker does not end with '.IS': {ticker}")
        elif ticker != f"{sym}.IS":
            errors.append(f"Derived yahoo_ticker '{ticker}' does not match '{sym}.IS'")
        elif ticker in seen_tickers:
            errors.append(f"Duplicate yahoo_ticker: {ticker}")
        seen_tickers.add(ticker)

        if inst.currency != "TRY":
            errors.append(f"Invalid currency '{inst.currency}' for symbol {sym}; expected 'TRY'")

        if inst.is_active:
            active_count += 1

    if instruments and active_count == 0:
        errors.append("No active BIST instruments found in catalog")

    return len(errors) == 0, errors


def plan_bist_price_upserts(
    stored_map: Dict[UUID, dict],
    incoming_items: Sequence[BistPriceSyncItem],
) -> Tuple[List[BistPriceSyncItem], int, int]:
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
    to_upsert: List[BistPriceSyncItem] = []
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


def _detect_multiindex_levels(
    columns: pd.MultiIndex,
    expected_tickers: Set[str],
) -> Tuple[Optional[int], Optional[int]]:
    """Determine which MultiIndex level contains tickers vs price field names."""
    if not isinstance(columns, pd.MultiIndex) or len(columns.levels) < 2:
        return None, None

    level0_set = {str(val).strip().upper() for val in columns.levels[0]}
    level1_set = {str(val).strip().upper() for val in columns.levels[1]}

    exp_norm = {t.strip().upper() for t in expected_tickers}

    overlap0 = len(level0_set.intersection(exp_norm))
    overlap1 = len(level1_set.intersection(exp_norm))

    if overlap0 > overlap1:
        return 0, 1
    elif overlap1 > overlap0:
        return 1, 0
    elif overlap0 > 0 and overlap0 == overlap1:
        # Check price field names in levels
        price_fields_in_0 = len(level0_set.intersection(KNOWN_PRICE_FIELDS))
        price_fields_in_1 = len(level1_set.intersection(KNOWN_PRICE_FIELDS))
        if price_fields_in_1 > price_fields_in_0:
            return 0, 1
        elif price_fields_in_0 > price_fields_in_1:
            return 1, 0

    return None, None


def extract_ticker_dataframe(
    df: pd.DataFrame,
    ticker: str,
    expected_tickers_count: int = 100,
) -> Tuple[Optional[pd.DataFrame], bool]:
    """Extract single-ticker DataFrame from a yfinance batch download DataFrame.

    Returns:
        (ticker_df, has_duplicate_columns)

    Safety rules:
    - If df has single-level columns, it is ONLY accepted if expected_tickers_count == 1.
    - If expected_tickers_count > 1, single-level columns are rejected as ambiguous structural errors.
    - If multiple columns exist for the ticker or for Close, has_duplicate_columns is True.
    """
    if df is None or df.empty:
        return None, False

    norm_ticker = ticker.strip().upper()

    if isinstance(df.columns, pd.MultiIndex):
        ticker_lvl, price_lvl = _detect_multiindex_levels(df.columns, {norm_ticker})
        if ticker_lvl is None:
            # Ambiguous MultiIndex
            return None, False

        try:
            if ticker_lvl == 0:
                # Level 0 is ticker
                if norm_ticker in [str(x).strip().upper() for x in df.columns.levels[0]]:
                    actual_key = next((x for x in df.columns.levels[0] if str(x).strip().upper() == norm_ticker), None)
                    if actual_key is not None:
                        sub = df[actual_key]
                        has_dup = False
                        if isinstance(sub, pd.DataFrame):
                            if "Close" in sub.columns:
                                close_col = sub["Close"]
                                if isinstance(close_col, pd.DataFrame):
                                    has_dup = True
                            return sub, has_dup
            else:
                # Level 1 is ticker
                actual_key = next((x for x in df.columns.levels[1] if str(x).strip().upper() == norm_ticker), None)
                if actual_key is not None:
                    sub = df.xs(actual_key, axis=1, level=1)
                    has_dup = False
                    if isinstance(sub, pd.DataFrame):
                        if "Close" in sub.columns:
                            close_col = sub["Close"]
                            if isinstance(close_col, pd.DataFrame):
                                has_dup = True
                        return sub, has_dup
        except Exception:
            return None, False

        return None, False

    # Single-level columns
    if expected_tickers_count == 1:
        has_dup = False
        if "Close" in df.columns:
            close_col = df["Close"]
            if isinstance(close_col, pd.DataFrame):
                has_dup = True
            return df, has_dup

    # When expected_tickers_count > 1, single-level DataFrame cannot be safely mapped
    return None, False


def _to_utc_datetime(ts: Any, timezone_name: str = settings.TIMEZONE) -> datetime:
    """Convert a timestamp/datetime/string into a timezone-aware UTC datetime."""
    if isinstance(ts, pd.Timestamp):
        if ts.tz is None:
            dt = ts.tz_localize(ZoneInfo(timezone_name))
        else:
            dt = ts
        return dt.tz_convert(timezone.utc).to_pydatetime()

    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            dt = ts.replace(tzinfo=ZoneInfo(timezone_name))
        else:
            dt = ts
        return dt.astimezone(timezone.utc)

    if isinstance(ts, date):
        dt = datetime(ts.year, ts.month, ts.day, 15, 0, tzinfo=ZoneInfo(timezone_name))
        return dt.astimezone(timezone.utc)

    pdt = pd.to_datetime(ts)
    return _to_utc_datetime(pdt, timezone_name=timezone_name)


def is_price_bar_fresh(
    bar_time_utc: datetime,
    session: Optional[XistSessionInfo],
    reference_time_utc: datetime,
) -> Tuple[bool, str]:
    """Verify freshness of an intraday price bar timestamp against trading session and reference time."""
    bar_date = bar_time_utc.astimezone(ZoneInfo(settings.TIMEZONE)).date()
    ref_date = reference_time_utc.astimezone(ZoneInfo(settings.TIMEZONE)).date()

    if session is not None and bar_date != session.trading_date:
        return False, f"Bar date ({bar_date}) does not match session trading date ({session.trading_date})."
    elif session is None and bar_date != ref_date:
        return False, f"Bar date ({bar_date}) does not match reference date ({ref_date})."

    # Future check with tolerance
    if bar_time_utc > reference_time_utc + timedelta(minutes=FUTURE_TOLERANCE_MINUTES):
        return False, f"Bar timestamp ({bar_time_utc.isoformat()}) is in the future relative to reference time."

    if session is not None and session.market_close is not None and session.market_open is not None:
        if reference_time_utc <= session.market_close:
            # During active session or pre-session
            if reference_time_utc >= session.market_open:
                if bar_time_utc < session.market_open - timedelta(minutes=PRE_SESSION_MAX_EARLY_MINUTES):
                    return False, "Bar is from before market open."
                age_minutes = (reference_time_utc - bar_time_utc).total_seconds() / 60.0
                if age_minutes > MAX_INTRADAY_DELAY_MINUTES:
                    return False, f"Bar is stale ({int(age_minutes)}m old during active session, max allowable {MAX_INTRADAY_DELAY_MINUTES}m)."
        else:
            # After market close
            if bar_time_utc < session.market_close - timedelta(minutes=POST_SESSION_MAX_EARLY_MINUTES):
                gap_early = (session.market_close - bar_time_utc).total_seconds() / 60.0
                return False, f"Closing bar is too early before market close ({int(gap_early)}m before session close, max {POST_SESSION_MAX_EARLY_MINUTES}m)."
            if bar_time_utc > session.market_close + timedelta(minutes=POST_SESSION_MAX_LATE_MINUTES):
                gap_late = (bar_time_utc - session.market_close).total_seconds() / 60.0
                return False, f"Bar is too late after market close ({int(gap_late)}m after session close, max {POST_SESSION_MAX_LATE_MINUTES}m)."

    return True, "OK"


class BistPricesService:
    """Core domain service for BIST stock prices normalization, validation, and calculations."""

    @staticmethod
    def normalize_bist_prices(
        intraday_df: pd.DataFrame,
        daily_df: pd.DataFrame,
        expected_tickers: Set[str],
        trading_date: date,
        session_info: Optional[XistSessionInfo] = None,
        reference_time: Optional[datetime] = None,
        timezone_name: str = settings.TIMEZONE,
    ) -> Tuple[List[BistNormalizedPrice], List[str], int, int, List[str], int, int, int]:
        """Normalize raw yfinance DataFrames and calculate deterministic Decimal pricing metrics without float conversions.

        Returns:
            (valid_prices, warnings, invalid_count, duplicate_count, unrequested_tickers, prev_matched_count, prev_missing_count, observed_ticker_count)
        """
        valid_prices: List[BistNormalizedPrice] = []
        warnings: List[str] = []
        unrequested_tickers: List[str] = []
        observed_tickers: Set[str] = set()
        invalid_count = 0
        duplicate_count = 0
        prev_matched_count = 0
        prev_missing_count = 0

        ref_time_utc = reference_time or datetime.now(timezone.utc)
        if ref_time_utc.tzinfo is None:
            ref_time_utc = ref_time_utc.replace(tzinfo=timezone.utc)

        # Discover all observed tickers from the detected ticker level in intraday_df
        if isinstance(intraday_df.columns, pd.MultiIndex):
            t_lvl, _ = _detect_multiindex_levels(intraday_df.columns, expected_tickers)
            if t_lvl is not None:
                raw_level_vals = {str(x).strip().upper() for x in intraday_df.columns.levels[t_lvl]}
                observed_tickers = {val for val in raw_level_vals if val not in KNOWN_PRICE_FIELDS}
                exp_norm = {t.strip().upper() for t in expected_tickers}
                for obs in sorted(list(observed_tickers)):
                    if obs not in exp_norm:
                        unrequested_tickers.append(obs)
                        warnings.append(f"Unrequested BIST ticker symbol '{obs}' observed in response.")
            else:
                warnings.append("MultiIndex column levels in Yahoo response could not be deterministically identified.")
        elif not intraday_df.empty:
            if len(expected_tickers) == 1:
                observed_tickers = {next(iter(expected_tickers)).strip().upper()}
            else:
                warnings.append("Single-level columns returned for multiple tickers; cannot safely map prices.")

        observed_ticker_count = len(observed_tickers)

        for ticker in sorted(list(expected_tickers)):
            norm_ticker = ticker.strip().upper()

            ticker_intra, has_dup_col = extract_ticker_dataframe(
                intraday_df, norm_ticker, expected_tickers_count=len(expected_tickers)
            )

            # Rule 1: If duplicate Close columns exist, DO NOT use any value. Treat ticker as INVALID.
            if has_dup_col:
                duplicate_count += 1
                invalid_count += 1
                warnings.append(f"Ticker '{norm_ticker}': duplicate Close columns found in response; treating as invalid.")
                continue

            if ticker_intra is None or ticker_intra.empty or "Close" not in ticker_intra.columns:
                invalid_count += 1
                warnings.append(f"Ticker '{norm_ticker}': no intraday price data returned from Yahoo Finance.")
                continue

            # 1. Parse current intraday price on trading_date
            try:
                close_series = ticker_intra["Close"]
                if isinstance(close_series, pd.DataFrame):
                    # Unhandled duplicate DataFrame
                    duplicate_count += 1
                    invalid_count += 1
                    warnings.append(f"Ticker '{norm_ticker}': ambiguous Close dataframe; treating as invalid.")
                    continue

                # Filter valid rows matching target trading_date
                valid_intraday_rows = []
                for idx, raw_val in close_series.items():
                    if raw_val is None or pd.isna(raw_val):
                        continue

                    # Safe Decimal parsing with NO float conversion
                    try:
                        str_val = str(raw_val).strip()
                        dec_val = Decimal(str_val)
                        if not dec_val.is_finite() or dec_val <= 0:
                            continue
                    except (InvalidOperation, ValueError, TypeError):
                        continue

                    # Safe timestamp conversion to UTC
                    try:
                        ts_utc = _to_utc_datetime(idx, timezone_name=timezone_name)
                    except Exception:
                        continue

                    # Check freshness
                    is_fresh, _ = is_price_bar_fresh(ts_utc, session_info, ref_time_utc)
                    if is_fresh:
                        valid_intraday_rows.append({"timestamp": ts_utc, "price": dec_val})

                if not valid_intraday_rows:
                    invalid_count += 1
                    warnings.append(f"Ticker '{norm_ticker}': no fresh intraday data found for trading date {trading_date}.")
                    continue

                # Order-independent selection: select row with the maximum timestamp
                best_bar = max(valid_intraday_rows, key=lambda x: x["timestamp"])
                price_dec = best_bar["price"].quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                price_at_dt = best_bar["timestamp"]

            except (ValueError, TypeError, OverflowError, InvalidOperation) as exc:
                invalid_count += 1
                warnings.append(f"Ticker '{norm_ticker}': intraday price parsing failed ({type(exc).__name__}).")
                continue

            # 2. Parse previous close from daily_df (last completed trading date < trading_date)
            prev_close_dec: Optional[Decimal] = None
            change_amt_dec: Optional[Decimal] = None
            change_pct_dec: Optional[Decimal] = None

            ticker_daily, daily_has_dup = extract_ticker_dataframe(
                daily_df, norm_ticker, expected_tickers_count=len(expected_tickers)
            )

            if ticker_daily is not None and not ticker_daily.empty and "Close" in ticker_daily.columns and not daily_has_dup:
                try:
                    daily_close_series = ticker_daily["Close"]
                    if isinstance(daily_close_series, pd.DataFrame):
                        daily_close_series = None

                    if daily_close_series is not None:
                        valid_daily_rows = []
                        for d_idx, d_val in daily_close_series.items():
                            if d_val is None or pd.isna(d_val):
                                continue

                            try:
                                str_dval = str(d_val).strip()
                                d_dec = Decimal(str_dval)
                                if not d_dec.is_finite() or d_dec <= 0:
                                    continue
                            except (InvalidOperation, ValueError, TypeError):
                                continue

                            try:
                                if isinstance(d_idx, (pd.Timestamp, datetime)):
                                    if d_idx.tz is None if isinstance(d_idx, pd.Timestamp) else d_idx.tzinfo is None:
                                        row_d = d_idx.date() if isinstance(d_idx, pd.Timestamp) else d_idx.date()
                                    else:
                                        row_d = d_idx.tz_convert(ZoneInfo(timezone_name)).date() if isinstance(d_idx, pd.Timestamp) else d_idx.astimezone(ZoneInfo(timezone_name)).date()
                                else:
                                    row_d = pd.to_datetime(d_idx).date()
                            except Exception:
                                continue

                            if row_d < trading_date:
                                valid_daily_rows.append({"date": row_d, "price": d_dec})

                        if valid_daily_rows:
                            # Order-independent selection: select row with the maximum previous date
                            best_prev = max(valid_daily_rows, key=lambda x: x["date"])
                            prev_close_dec = best_prev["price"].quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                            change_amt_dec = (price_dec - prev_close_dec).quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)
                            change_pct_dec = (
                                ((price_dec - prev_close_dec) / prev_close_dec) * Decimal("100")
                            ).quantize(PERCENT_SCALE, rounding=ROUND_HALF_UP)
                            prev_matched_count += 1
                        else:
                            prev_missing_count += 1
                            warnings.append(f"Ticker '{norm_ticker}': no previous trading day close found prior to {trading_date}.")
                    else:
                        prev_missing_count += 1
                        warnings.append(f"Ticker '{norm_ticker}': duplicate daily Close columns skipped.")

                except Exception as exc:
                    prev_missing_count += 1
                    warnings.append(f"Ticker '{norm_ticker}': previous close extraction failed ({type(exc).__name__}).")
            else:
                prev_missing_count += 1
                warnings.append(f"Ticker '{norm_ticker}': daily DataFrame empty or missing Close.")

            # Base symbol without .IS
            base_symbol = norm_ticker[:-3] if norm_ticker.endswith(".IS") else norm_ticker

            item = BistNormalizedPrice(
                symbol=base_symbol,
                yahoo_ticker=norm_ticker,
                price=price_dec,
                currency="TRY",
                previous_close=prev_close_dec,
                change_amount=change_amt_dec,
                change_percent=change_pct_dec,
                price_at=price_at_dt,
                provider_code="YAHOO_FINANCE",
            )
            valid_prices.append(item)

        return (
            valid_prices,
            warnings,
            invalid_count,
            duplicate_count,
            unrequested_tickers,
            prev_matched_count,
            prev_missing_count,
            observed_ticker_count,
        )

    @staticmethod
    def match_prices_with_instruments(
        active_instruments: Sequence[BistDbInstrument],
        normalized_prices: Sequence[BistNormalizedPrice],
        unrequested_tickers: Optional[Sequence[str]] = None,
    ) -> Tuple[List[BistPriceSyncItem], List[BistDbInstrument], List[str]]:
        """Match normalized BIST prices with active Supabase instruments.

        Returns:
            (matched_sync_items, missing_active_instruments, unmatched_tickers)
        """
        price_map: Dict[str, BistNormalizedPrice] = {
            p.yahoo_ticker: p for p in normalized_prices
        }

        matched_items: List[BistPriceSyncItem] = []
        missing_active: List[BistDbInstrument] = []
        matched_tickers: Set[str] = set()

        for inst in active_instruments:
            ticker = inst.yahoo_ticker
            price_entry = price_map.get(ticker)
            if price_entry is not None:
                matched_items.append(
                    BistPriceSyncItem(
                        instrument_id=inst.id,
                        symbol=inst.symbol,
                        yahoo_ticker=ticker,
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
                matched_tickers.add(ticker)
            else:
                missing_active.append(inst)

        unmatched = list(unrequested_tickers or [])
        for p in normalized_prices:
            if p.yahoo_ticker not in matched_tickers and p.yahoo_ticker not in unmatched:
                unmatched.append(p.yahoo_ticker)

        return matched_items, missing_active, unmatched
