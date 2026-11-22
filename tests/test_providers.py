from unittest.mock import patch

import pytest

from app.providers.gold_provider import GoldProvider, _clean_price
from app.providers.yahoo_provider import _clean_val


def test_clean_price_validation():
    assert _clean_price(100.5) == 100.5
    assert _clean_price(0) is None
    assert _clean_price(-50.0) is None
    assert _clean_price("invalid") is None
    assert _clean_price(None) is None

def test_clean_val_validation():
    assert _clean_val(14220.42) == 14220.42
    assert _clean_val(-10) is None
    assert _clean_val("abc") is None
    assert _clean_val(None) is None

@pytest.mark.asyncio
async def test_gold_provider_failure_returns_none_and_warning():
    provider = GoldProvider(timeout=1.0)
    # Patch httpx and yfinance to simulate network failure
    with patch("httpx.AsyncClient.get", side_effect=Exception("Network error")):
        with patch("asyncio.to_thread", side_effect=Exception("YF error")):
            price, warning = await provider.fetch_xau_usd()
            assert price is None
            assert warning == "XAUUSD data could not be retrieved."
