from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pandas as pd

from app.repositories.fx_latest_price_repository import FxLatestPriceRepository
from app.services.fx_prices_service import FxPricesService

INSTRUMENT_ID = UUID("20000000-0000-0000-0000-000000000099")


def test_fx_normalization_calculates_previous_close_and_change_without_float_drift():
    intraday = pd.DataFrame(
        {"Close": ["40.123456789012", "40.500000000000"]},
        index=pd.to_datetime(["2026-08-24T20:00:00Z", "2026-08-25T20:00:00Z"]),
    )
    daily = pd.DataFrame(
        {"Close": ["39.500000000000", "40.000000000000"]},
        index=pd.to_datetime(["2026-08-23", "2026-08-24"]),
    )

    item = FxPricesService.normalize(
        INSTRUMENT_ID,
        intraday,
        daily,
        reference_time=datetime(2026, 8, 25, 21, tzinfo=timezone.utc),
    )

    assert item.price == Decimal("40.500000000000")
    assert item.previous_close == Decimal("40.000000000000")
    assert item.change_amount == Decimal("0.500000000000")
    assert item.change_percent == Decimal("1.25000000")
    assert item.price_at == datetime(2026, 8, 25, 20, tzinfo=timezone.utc)


def test_fx_normalization_handles_yfinance_multiindex_and_missing_previous_close():
    columns = pd.MultiIndex.from_tuples([("Close", "TRY=X"), ("Open", "TRY=X")])
    intraday = pd.DataFrame(
        [["41.250000000000", "41.0"]],
        index=pd.to_datetime(["2026-08-25T20:00:00Z"]),
        columns=columns,
    )
    item = FxPricesService.normalize(
        INSTRUMENT_ID,
        intraday,
        pd.DataFrame(),
        reference_time=datetime(2026, 8, 25, 21, tzinfo=timezone.utc),
    )
    assert item.price == Decimal("41.250000000000")
    assert item.previous_close is None
    assert item.change_percent is None


def test_fx_normalization_rejects_stale_or_ambiguous_data():
    stale = pd.DataFrame(
        {"Close": ["40"]},
        index=pd.to_datetime(["2026-08-20T10:00:00Z"]),
    )
    try:
        FxPricesService.normalize(
            INSTRUMENT_ID,
            stale,
            pd.DataFrame(),
            reference_time=datetime(2026, 8, 25, 21, tzinfo=timezone.utc),
        )
    except ValueError as exc:
        assert str(exc) == "USDTRY price is too old"
    else:
        raise AssertionError("stale FX data was accepted")


def test_fx_idempotency_comparison_includes_stale_state():
    intraday = pd.DataFrame(
        {"Close": ["40"]}, index=pd.to_datetime(["2026-08-25T20:00:00Z"])
    )
    item = FxPricesService.normalize(
        INSTRUMENT_ID,
        intraday,
        pd.DataFrame(),
        reference_time=datetime(2026, 8, 25, 21, tzinfo=timezone.utc),
    )
    stored = {
        "price": item.price,
        "previous_close": None,
        "change_amount": None,
        "change_percent": None,
        "price_at": item.price_at,
        "provider_code": item.provider_code,
        "is_stale": False,
        "stale_reason": None,
    }
    assert FxLatestPriceRepository.is_unchanged(stored, item)
    stored["is_stale"] = True
    stored["stale_reason"] = "price_not_published"
    assert not FxLatestPriceRepository.is_unchanged(stored, item)
