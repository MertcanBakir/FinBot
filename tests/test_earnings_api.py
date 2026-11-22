from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import app
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsEvent,
)

client = TestClient(app)
TZ_TR = ZoneInfo("Europe/Istanbul")
NOW_REF = datetime(2026, 8, 10, 10, 0, tzinfo=TZ_TR)


@patch("app.api.earnings._now_istanbul", return_value=NOW_REF)
def test_get_bist100_week_earnings_endpoint(mock_now):
    mock_calendar = Bist100EarningsCalendar(
        schema_version="2.0",
        generated_at=NOW_REF.isoformat(),
        coverage_start="2026-08-01",
        coverage_end="2026-10-31",
        market="BIST100",
        status="success",
        provider_status="success",
        events=[
            BistEarningsEvent(
                symbol="CCOLA",
                company_name="COCA-COLA İÇECEK A.Ş.",
                earnings_date="2026-08-10",
                period_start="2026-01-01",
                period_end="2026-06-30",
                disclosure_index=1627747,
            )
        ],
    )

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=mock_calendar):
        response = client.get("/api/earnings/bist100/week?offset=0")
        assert response.status_code == 200
        data = response.json()
        assert data["market"] == "BIST100"
        assert len(data["events"]) == 1
        assert data["events"][0]["symbol"] == "CCOLA"
        assert data["events"][0]["earnings_date"] == "2026-08-10"


@patch("app.api.earnings._now_istanbul", return_value=NOW_REF)
def test_get_nasdaq100_week_earnings_endpoint(mock_now):
    mock_calendar = Nasdaq100EarningsCalendar(
        schema_version="2.0",
        generated_at=NOW_REF.isoformat(),
        coverage_start="2026-08-01",
        coverage_end="2026-10-31",
        market="NASDAQ100",
        status="success",
        provider_status="success",
        events=[
            NasdaqEarningsEvent(
                symbol="NVDA",
                company_name="NVIDIA Corporation",
                earnings_date="2026-08-18",
                session="after_hours",
                session_label="After Hours",
            )
        ],
    )

    with patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=mock_calendar):
        response = client.get("/api/earnings/nasdaq100/week?offset=1")
        assert response.status_code == 200
        data = response.json()
        assert data["market"] == "NASDAQ100"
        assert len(data["events"]) == 1
        assert data["events"][0]["symbol"] == "NVDA"
        assert data["events"][0]["session"] == "after_hours"
