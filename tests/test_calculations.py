from unittest.mock import AsyncMock

import pytest

from app.providers.gold_provider import GoldProvider
from app.providers.yahoo_provider import YahooProvider
from app.services.market_summary import OUNCE_TO_GRAM_FACTOR, MarketSummaryService


@pytest.mark.asyncio
async def test_bist_change_percent_calculation():
    mock_yahoo = AsyncMock(spec=YahooProvider)
    mock_gold = AsyncMock(spec=GoldProvider)

    mock_yahoo.fetch_market_data.return_value = {
        "bist100": {
            "symbol": "XU100.IS",
            "price": 14220.42,
            "previous_close": 14077.10,
            "open": 14100.20,
            "high": 14280.17,
            "low": 14030.42,
            "change_percent": 1.02
        },
        "usdtry": {"price": 46.84},
        "eurtry": {"price": 54.96},
        "bitcoin": {"usd": 118450.25},
        "warnings": []
    }
    mock_gold.fetch_xau_usd.return_value = (4051.25, None)

    service = MarketSummaryService(yahoo_provider=mock_yahoo, gold_provider=mock_gold)
    snapshot = await service.generate()

    assert snapshot.market.bist100.change_percent == 1.02
    assert snapshot.market.bist100.price == 14220.42

@pytest.mark.asyncio
async def test_gram_gold_calculation():
    mock_yahoo = AsyncMock(spec=YahooProvider)
    mock_gold = AsyncMock(spec=GoldProvider)

    xau_usd = 4051.25
    usd_try = 46.84
    expected_gram = round((xau_usd * usd_try) / OUNCE_TO_GRAM_FACTOR, 2)

    mock_yahoo.fetch_market_data.return_value = {
        "bist100": {"price": 10000.0, "previous_close": 10000.0, "change_percent": 0.0},
        "usdtry": {"price": usd_try},
        "eurtry": {"price": 50.0},
        "bitcoin": {"usd": 100000.0},
        "warnings": []
    }
    mock_gold.fetch_xau_usd.return_value = (xau_usd, None)

    service = MarketSummaryService(yahoo_provider=mock_yahoo, gold_provider=mock_gold)
    snapshot = await service.generate()

    assert snapshot.market.gold.xauusd == xau_usd
    assert snapshot.market.gold.gram_try == expected_gram
    assert snapshot.market.gold.gram_try_type == "calculated_spot"

@pytest.mark.asyncio
async def test_missing_xauusd_results_in_null_gram_gold():
    mock_yahoo = AsyncMock(spec=YahooProvider)
    mock_gold = AsyncMock(spec=GoldProvider)

    mock_yahoo.fetch_market_data.return_value = {
        "bist100": {"price": 10000.0, "previous_close": 10000.0, "change_percent": 0.0},
        "usdtry": {"price": 46.84},
        "eurtry": {"price": 54.96},
        "bitcoin": {"usd": 100000.0},
        "warnings": []
    }
    mock_gold.fetch_xau_usd.return_value = (None, "XAUUSD data could not be retrieved.")

    service = MarketSummaryService(yahoo_provider=mock_yahoo, gold_provider=mock_gold)
    snapshot = await service.generate()

    assert snapshot.market.gold.xauusd is None
    assert snapshot.market.gold.gram_try is None
    assert "XAUUSD data could not be retrieved." in snapshot.warnings

@pytest.mark.asyncio
async def test_btc_usd_added_to_model_and_btc_try_calculation():
    mock_yahoo = AsyncMock(spec=YahooProvider)
    mock_gold = AsyncMock(spec=GoldProvider)

    btc_usd = 118450.25
    usd_try = 46.84
    expected_btc_try = round(btc_usd * usd_try, 2)

    mock_yahoo.fetch_market_data.return_value = {
        "bist100": {"price": 10000.0, "previous_close": 10000.0, "change_percent": 0.0},
        "usdtry": {"price": usd_try},
        "eurtry": {"price": 54.96},
        "bitcoin": {"usd": btc_usd},
        "warnings": []
    }
    mock_gold.fetch_xau_usd.return_value = (4000.0, None)

    service = MarketSummaryService(yahoo_provider=mock_yahoo, gold_provider=mock_gold)
    snapshot = await service.generate()

    assert snapshot.market.bitcoin.usd == btc_usd
    assert snapshot.market.bitcoin.try_price == expected_btc_try

@pytest.mark.asyncio
async def test_missing_usdtry_results_in_null_btc_try():
    mock_yahoo = AsyncMock(spec=YahooProvider)
    mock_gold = AsyncMock(spec=GoldProvider)

    mock_yahoo.fetch_market_data.return_value = {
        "bist100": {"price": 10000.0},
        "usdtry": {"price": None},
        "eurtry": {"price": 54.96},
        "bitcoin": {"usd": 118450.25},
        "warnings": ["USD/TRY data could not be retrieved."]
    }
    mock_gold.fetch_xau_usd.return_value = (4000.0, None)

    service = MarketSummaryService(yahoo_provider=mock_yahoo, gold_provider=mock_gold)
    snapshot = await service.generate()

    assert snapshot.market.bitcoin.usd == 118450.25
    assert snapshot.market.bitcoin.try_price is None

@pytest.mark.asyncio
async def test_missing_btcusd_results_in_null_btc_try():
    mock_yahoo = AsyncMock(spec=YahooProvider)
    mock_gold = AsyncMock(spec=GoldProvider)

    mock_yahoo.fetch_market_data.return_value = {
        "bist100": {"price": 10000.0},
        "usdtry": {"price": 46.84},
        "eurtry": {"price": 54.96},
        "bitcoin": {"usd": None},
        "warnings": ["Bitcoin (BTC-USD) data could not be retrieved from Yahoo Finance."]
    }
    mock_gold.fetch_xau_usd.return_value = (4000.0, None)

    service = MarketSummaryService(yahoo_provider=mock_yahoo, gold_provider=mock_gold)
    snapshot = await service.generate()

    assert snapshot.market.bitcoin.usd is None
    assert snapshot.market.bitcoin.try_price is None
    assert "Bitcoin (BTC-USD) data could not be retrieved from Yahoo Finance." in snapshot.warnings

@pytest.mark.asyncio
async def test_bitcoin_provider_failure_does_not_crash_summary():
    mock_yahoo = AsyncMock(spec=YahooProvider)
    mock_gold = AsyncMock(spec=GoldProvider)

    # Yahoo provider encounters exception in BTC section
    mock_yahoo.fetch_market_data.return_value = {
        "bist100": {"price": 10000.0, "previous_close": 10000.0, "change_percent": 0.0},
        "usdtry": {"price": 46.84},
        "eurtry": {"price": 54.96},
        "bitcoin": {"usd": None},
        "warnings": ["Error fetching Bitcoin (BTC-USD): Connection timeout"]
    }
    mock_gold.fetch_xau_usd.return_value = (4000.0, None)

    service = MarketSummaryService(yahoo_provider=mock_yahoo, gold_provider=mock_gold)
    snapshot = await service.generate()

    assert snapshot.success is True
    assert snapshot.market.bist100.price == 10000.0
    assert snapshot.market.bitcoin.usd is None
    assert snapshot.market.bitcoin.try_price is None
    assert "Error fetching Bitcoin (BTC-USD): Connection timeout" in snapshot.warnings
