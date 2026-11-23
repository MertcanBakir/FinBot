from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import app
from app.models.market import EconomicEvent

client = TestClient(app)

def test_health_check_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

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
