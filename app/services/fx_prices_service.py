from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

import pandas as pd

from app.models.fx_prices import FxPrice

PRICE_SCALE = Decimal("0.000000000001")
PERCENT_SCALE = Decimal("0.00000001")


def _close_series(frame: pd.DataFrame) -> Optional[pd.Series]:
    if frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        matches = [col for col in frame.columns if any(str(v).upper() == "CLOSE" for v in col)]
        if len(matches) != 1:
            return None
        value = frame[matches[0]]
    else:
        matches = [col for col in frame.columns if str(col).upper() == "CLOSE"]
        if len(matches) != 1:
            return None
        value = frame[matches[0]]
    return value if isinstance(value, pd.Series) else None


def _decimal(value: object) -> Optional[Decimal]:
    try:
        if value is None or pd.isna(value):
            return None
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)


class FxPricesService:
    @staticmethod
    def normalize(
        instrument_id,
        intraday: pd.DataFrame,
        daily: pd.DataFrame,
        reference_time: Optional[datetime] = None,
    ) -> FxPrice:
        now = reference_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)

        intraday_close = _close_series(intraday)
        if intraday_close is None:
            raise ValueError("FX intraday close column is ambiguous or missing")

        candidates = []
        for index, raw in intraday_close.items():
            price = _decimal(raw)
            try:
                timestamp = pd.Timestamp(index)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.tz_localize("UTC")
                price_at = timestamp.tz_convert("UTC").to_pydatetime()
            except Exception:
                continue
            if price is not None and price_at <= now + timedelta(minutes=5):
                candidates.append((price_at, price))
        if not candidates:
            raise ValueError("No valid USDTRY price was found")

        price_at, price = max(candidates, key=lambda item: item[0])
        if now - price_at > timedelta(hours=96):
            raise ValueError("USDTRY price is too old")

        previous_close = None
        daily_close = _close_series(daily)
        if daily_close is not None:
            previous = []
            for index, raw in daily_close.items():
                parsed = _decimal(raw)
                try:
                    day = pd.Timestamp(index).date()
                except Exception:
                    continue
                if parsed is not None and day < price_at.date():
                    previous.append((day, parsed))
            if previous:
                previous_close = max(previous, key=lambda item: item[0])[1]

        change_amount = None
        change_percent = None
        if previous_close is not None:
            change_amount = (price - previous_close).quantize(
                PRICE_SCALE, rounding=ROUND_HALF_UP
            )
            change_percent = (
                (price - previous_close) / previous_close * Decimal("100")
            ).quantize(PERCENT_SCALE, rounding=ROUND_HALF_UP)

        return FxPrice(
            instrument_id=instrument_id,
            price=price,
            previous_close=previous_close,
            change_amount=change_amount,
            change_percent=change_percent,
            price_at=price_at,
        )
