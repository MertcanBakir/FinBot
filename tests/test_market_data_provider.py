from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.providers import yahoo_provider


def _ticker(**fast_info):
    return SimpleNamespace(fast_info=fast_info, info={})


def test_yahoo_market_data_maps_complete_provider_response(monkeypatch):
    tickers = {
        "XU100.IS": _ticker(
            lastPrice=10_100,
            previousClose=10_000,
            open=10_010,
            dayHigh=10_150,
            dayLow=9_980,
        ),
        "USDTRY=X": _ticker(lastPrice=40.5),
        "EURTRY=X": _ticker(lastPrice=47.25),
        "BTC-USD": _ticker(lastPrice=115_000),
        "^GSPC": _ticker(lastPrice=6_100, previousClose=6_000),
        "^IXIC": _ticker(lastPrice=21_200, previousClose=21_000),
    }
    monkeypatch.setattr(
        yahoo_provider.yf,
        "Tickers",
        lambda _symbols: SimpleNamespace(tickers=tickers),
    )
    monkeypatch.setattr(yahoo_provider, "get_us_market_status", lambda: "OPEN")

    result = yahoo_provider._fetch_sync_data()

    assert result["bist100"] == {
        "symbol": "XU100.IS",
        "price": 10_100.0,
        "previous_close": 10_000.0,
        "open": 10_010.0,
        "high": 10_150.0,
        "low": 9_980.0,
        "change_percent": 1.0,
    }
    assert result["usdtry"] == {"price": 40.5}
    assert result["eurtry"] == {"price": 47.25}
    assert result["bitcoin"] == {"usd": 115_000.0}
    assert result["us_market"] == {
        "sp500": {
            "price": 6_100.0,
            "previous_close": 6_000.0,
            "change_percent": 1.67,
        },
        "nasdaq": {
            "price": 21_200.0,
            "previous_close": 21_000.0,
            "change_percent": 0.95,
        },
        "status": "OPEN",
    }
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_yahoo_provider_runs_synchronous_fetch_off_event_loop(monkeypatch):
    expected = {"bist100": {"price": 10_100.0}}
    to_thread = AsyncMock(return_value=expected)
    monkeypatch.setattr(yahoo_provider.asyncio, "to_thread", to_thread)

    result = await yahoo_provider.YahooProvider().fetch_market_data()

    assert result == expected
    to_thread.assert_awaited_once_with(yahoo_provider._fetch_sync_data)
