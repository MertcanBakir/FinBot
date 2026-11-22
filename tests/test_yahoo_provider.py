from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.providers.earnings.yahoo_provider import (
    YahooEarningsProvider,
    normalize_session,
    parse_date_val,
)


def test_normalize_session():
    assert normalize_session("Pre-Market") == ("pre_market", "Pre-Market")
    assert normalize_session("Before Market Open") == ("pre_market", "Pre-Market")
    assert normalize_session("BMO") == ("pre_market", "Pre-Market")

    assert normalize_session("After Hours") == ("after_hours", "After Hours")
    assert normalize_session("After Market Close") == ("after_hours", "After Hours")
    assert normalize_session("AMC") == ("after_hours", "After Hours")

    assert normalize_session(None) == ("time_not_supplied", "Time Not Supplied")
    assert normalize_session("Unknown Timing") == ("time_not_supplied", "Time Not Supplied")

def test_parse_date_val():
    assert parse_date_val("2026-08-13") == "2026-08-13"
    assert parse_date_val("2026-08-13T00:00:00Z") == "2026-08-13"
    assert parse_date_val(None) is None

def test_yahoo_provider_fetch_global_calendar():
    provider = YahooEarningsProvider()
    whitelist = {"NVDA", "AAPL", "MSFT"}

    df_page1 = pd.DataFrame([
        {
            "Symbol": "NVDA",
            "Company": "NVIDIA Corp",
            "Earnings Date": "2026-08-13",
            "Timing": "After Hours"
        },
        {
            "Symbol": "INVALID_TICKER",
            "Company": "Non-Nasdaq Stock",
            "Earnings Date": "2026-08-13",
            "Timing": "Pre-Market"
        }
    ])

    mock_calendars = MagicMock()
    mock_calendars.get_earnings_calendar.side_effect = [df_page1, None]

    with patch("yfinance.Calendars", return_value=mock_calendars):
        events, warnings = provider._fetch_global_calendar_sync("2026-08-10", "2026-08-16", whitelist)
        
        # Verify filter_most_active=False argument was passed
        mock_calendars.get_earnings_calendar.assert_called_with(
            start="2026-08-10",
            end="2026-08-16",
            filter_most_active=False,
            limit=100,
            offset=0
        )

        assert len(events) == 1
        assert events[0].symbol == "NVDA"
        assert events[0].company_name == "NVIDIA Corp"
        assert events[0].earnings_date == "2026-08-13"
        assert events[0].session == "after_hours"
        assert events[0].session_label == "After Hours"

@pytest.mark.asyncio
async def test_nasdaq100_whitelist_enforcement():
    """Ensure events for symbols outside data/nasdaq100_symbols.json are discarded by service."""
    from unittest.mock import AsyncMock

    from app.models.earnings import NasdaqEarningsEvent
    from app.services.nasdaq100_earnings import Nasdaq100EarningsService

    provider = YahooEarningsProvider()
    service = Nasdaq100EarningsService(provider=provider)

    mock_raw_events = [
        NasdaqEarningsEvent(
            symbol="NVDA",
            company_name="NVIDIA Corporation",
            earnings_date="2026-08-13",
            session="after_hours",
            session_label="After Hours"
        ),
        NasdaqEarningsEvent(
            symbol="NON_NASDAQ_TICKER",
            company_name="Random Non-Nasdaq Stock",
            earnings_date="2026-08-13",
            session="pre_market",
            session_label="Pre-Market"
        )
    ]

    with patch.object(provider, "get_earnings_events", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = (mock_raw_events, [])
        with patch.object(provider, "_load_nasdaq100_symbols", return_value=(["NVDA"], {"NVDA"})):
            calendar = await service.get_week_events(offset=0, ref_date=datetime(2026, 8, 10))
            assert len(calendar.events) == 1
            assert calendar.events[0].symbol == "NVDA"
            assert not any(ev.symbol == "NON_NASDAQ_TICKER" for ev in calendar.events)
