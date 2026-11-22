from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.core.market_session import MarketSession
from app.jobs.closing_job import run_closing_job
from app.jobs.morning_job import run_morning_job
from app.main import app
from app.models.market import (
    Bist100Data,
    BistMovers,
    BitcoinData,
    CurrencyData,
    EconomicEvent,
    GoldData,
    MarketData,
    MarketMover,
    MarketSnapshot,
    USIndexData,
    USMarketData,
)
from tests.conftest import create_mock_db_connection

client = TestClient(app)

def test_health_check_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

@patch("app.services.market_summary.market_summary_service.generate")
@patch("app.services.email_service.EmailService.send_email")
def test_get_market_summary_does_not_send_email_and_includes_us_market(mock_send_email, mock_generate):
    mock_snapshot = MarketSnapshot(
        success=True,
        generated_at="2026-07-31T14:23:42+03:00",
        timezone="Europe/Istanbul",
        session=MarketSession.INTRADAY,
        market=MarketData(
            bist100=Bist100Data(price=14220.42, previous_close=14077.10, change_percent=1.02),
            usdtry=CurrencyData(price=46.84),
            eurtry=CurrencyData(price=54.96),
            gold=GoldData(xauusd=4051.25, gram_try=6098.14),
            bitcoin=BitcoinData(usd=118450.25, try_price=5543210.18),
            us_market=USMarketData(
                sp500=USIndexData(price=6412.0, change_percent=-0.27),
                nasdaq=USIndexData(price=21108.0, change_percent=-0.41),
                status="CLOSED"
            )
        ),
        text="Sample intraday text",
        warnings=[]
    )
    mock_generate.return_value = mock_snapshot

    response = client.get("/api/market-summary")
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert data["market"]["us_market"]["sp500"]["price"] == 6412.0
    assert data["market"]["us_market"]["nasdaq"]["price"] == 21108.0

    # GET API endpoint MUST NOT call email service
    mock_send_email.assert_not_called()

@patch("app.services.bist_movers.bist_movers_service.get_movers")
@patch("app.services.email_service.EmailService.send_email")
def test_get_bist_movers_endpoint_does_not_send_email(mock_send_email, mock_get_movers):
    mock_get_movers.return_value = (
        BistMovers(
            gainers=[MarketMover(symbol="ASELS", price=100.0, change_percent=4.82)],
            losers=[MarketMover(symbol="SAHOL", price=80.0, change_percent=-2.84)]
        ),
        []
    )

    response = client.get("/api/bist-movers")
    assert response.status_code == 200

    data = response.json()
    assert len(data["gainers"]) == 1
    assert data["gainers"][0]["symbol"] == "ASELS"
    assert len(data["losers"]) == 1
    assert data["losers"][0]["symbol"] == "SAHOL"

    # MUST NOT call email service
    mock_send_email.assert_not_called()

@patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load")
@patch("app.services.economic_calendar_state_store.economic_calendar_state_store.events_between")
@patch("app.services.email_service.EmailService.send_email")
def test_get_economic_calendar_endpoint_does_not_send_email_and_no_importance_field(mock_send_email, mock_events_between, mock_load):
    from app.models.economic_calendar import (
        EconomicProviderResult,
        RollingEconomicCalendar,
    )
    cal = RollingEconomicCalendar(
        schema_version="2.0",
        generated_at="2026-08-03T07:30:00+03:00",
        coverage_start="2026-08-03",
        coverage_end="2026-11-01",
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", event_count=1)],
        events=[
            EconomicEvent(
                name="Türkiye TÜFE",
                country="TR",
                scheduled_at="2026-08-03T10:00:00+03:00",
                scheduled_at_istanbul="2026-08-03T10:00:00+03:00",
                source="TÜİK"
            )
        ],
        warnings=[]
    )
    mock_load.return_value = cal
    mock_events_between.return_value = (cal.events, [], cal)

    with patch("app.api.market.datetime") as mock_dt:
        tz = ZoneInfo("Europe/Istanbul")
        mock_dt.now.return_value = datetime(2026, 8, 3, 10, 0, tzinfo=tz)
        mock_dt.fromisoformat = datetime.fromisoformat

        response = client.get("/api/economic-calendar/today")
        assert response.status_code == 200

        data = response.json()
        assert "events" in data
        assert len(data["events"]) == 1
        assert data["events"][0]["name"] == "Türkiye TÜFE"
        assert "importance" not in data["events"][0]

        # MUST NOT call email service
        mock_send_email.assert_not_called()

@patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load", return_value=None)
def test_economic_calendar_api_returns_503_when_cache_missing(mock_load):
    response = client.get("/api/economic-calendar/today")
    assert response.status_code == 503
    assert "Supabase economic calendar is unavailable" in response.json()["detail"]

@pytest.mark.asyncio
@patch("app.services.market_summary.market_summary_service.generate")
@patch("app.jobs.morning_job.morning_calendar_service.get_weekly_calendar", return_value=([], [], [], "database"))
async def test_morning_job_cli_execution(mock_get_calendar, mock_generate):
    mock_snapshot = MarketSnapshot(
        success=True,
        generated_at="2026-07-31T10:25:00+03:00",
        timezone="Europe/Istanbul",
        session=MarketSession.INTRADAY,
        market=MarketData(
            bist100=Bist100Data(price=14120.25, change_percent=0.30),
            usdtry=CurrencyData(price=46.82),
            eurtry=CurrencyData(price=54.91),
            gold=GoldData(xauusd=4044.20, gram_try=6086.0),
            bitcoin=BitcoinData(usd=62986.0)
        ),
        text="Morning summary text",
        warnings=[]
    )
    mock_generate.return_value = mock_snapshot
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    result = await run_morning_job(
        email_service=mock_email,
        db_connection_factory=create_mock_db_connection,
    )
    assert result.status == "success"
    mock_email.send_email.assert_called_once()
    kwargs = mock_email.send_email.call_args.kwargs
    assert kwargs["subject"]
    assert kwargs["body_text"]
    assert kwargs["body_html"]
    assert "raw_data_summary" not in kwargs
    assert "warnings" not in kwargs
    assert "fetch_time" not in kwargs

@pytest.mark.asyncio
@patch("app.services.market_summary.market_summary_service.generate")
@patch("app.services.bist_movers.bist_movers_service.get_movers")
async def test_closing_job_cli_execution(mock_get_movers, mock_generate):
    mock_snapshot = MarketSnapshot(
        success=True,
        generated_at="2026-07-31T18:30:00+03:00",
        timezone="Europe/Istanbul",
        session=MarketSession.CLOSED,
        market=MarketData(
            bist100=Bist100Data(price=14220.42, change_percent=1.02),
            usdtry=CurrencyData(price=46.84),
            eurtry=CurrencyData(price=54.96),
            gold=GoldData(xauusd=4051.25, gram_try=6098.14),
            bitcoin=BitcoinData(usd=62986.0)
        ),
        text="Closing summary text",
        warnings=[]
    )
    mock_generate.return_value = mock_snapshot
    mock_get_movers.return_value = (BistMovers(gainers=[], losers=[]), [])
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    result = await run_closing_job(
        email_service=mock_email,
        db_connection_factory=create_mock_db_connection,
    )
    assert result.status == "success"
    mock_email.send_email.assert_called_once()
    kwargs = mock_email.send_email.call_args.kwargs
    assert kwargs["subject"]
    assert kwargs["body_text"]
    assert kwargs["body_html"]
    assert "raw_data_summary" not in kwargs
    assert "warnings" not in kwargs
    assert "fetch_time" not in kwargs
