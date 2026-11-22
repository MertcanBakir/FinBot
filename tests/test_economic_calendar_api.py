from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import app
from app.models.economic_calendar import EconomicProviderResult, RollingEconomicCalendar
from app.models.market import EconomicEvent

client = TestClient(app)

def _build_test_calendar(coverage_start="2026-08-17", warnings=None) -> RollingEconomicCalendar:
    d_start = date.fromisoformat(coverage_start)
    d_end = d_start + timedelta(days=90)
    return RollingEconomicCalendar(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start=d_start.isoformat(),
        coverage_end=d_end.isoformat(),
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", event_count=1),
            EconomicProviderResult(provider_id="BLS", provider_name="BLS", status="success", event_count=1)
        ],
        events=[
            EconomicEvent(name="Türkiye TÜFE", country="TR", scheduled_at="", scheduled_at_istanbul="2026-08-17T10:00:00+03:00", source="TÜİK", provider_id="TUIK", event_code="TR_CPI"),
            EconomicEvent(name="ABD CPI", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-20T15:30:00+03:00", source="BLS", provider_id="BLS", event_code="US_CPI")
        ],
        warnings=warnings or []
    )

def test_api_missing_cache_returns_503():
    with patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load", return_value=None):
        for path in [
            "/api/economic-calendar/today",
            "/api/economic-calendar/week",
            "/api/economic-calendar/month",
            "/api/economic-calendar?start_date=2026-08-17&end_date=2026-08-20"
        ]:
            res = client.get(path)
            assert res.status_code == 503
            assert "Supabase economic calendar is unavailable" in res.json()["detail"]

def test_api_today_economic_events():
    cal = _build_test_calendar()
    with patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load", return_value=cal), \
         patch("app.services.economic_calendar_state_store.economic_calendar_state_store.is_stale", return_value=False):
        
        with patch("app.api.market.datetime") as mock_dt:
            tz = ZoneInfo("Europe/Istanbul")
            mock_dt.now.return_value = datetime(2026, 8, 17, 10, 0, tzinfo=tz)
            mock_dt.fromisoformat = datetime.fromisoformat

            res = client.get("/api/economic-calendar/today")
            assert res.status_code == 200
            data = res.json()
            assert data["date"] == "2026-08-17"
            assert len(data["events"]) == 1
            assert data["events"][0]["name"] == "Türkiye TÜFE"

def test_api_today_outside_coverage_returns_503():
    # Cache coverage is in 2025, but today is in 2026
    cal = RollingEconomicCalendar(
        schema_version="2.0",
        generated_at="2025-01-01T07:30:00+03:00",
        coverage_start="2025-01-01",
        coverage_end="2025-04-01",
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[],
        events=[],
        warnings=[]
    )
    with patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load", return_value=cal):
        with patch("app.api.market.datetime") as mock_dt:
            tz = ZoneInfo("Europe/Istanbul")
            mock_dt.now.return_value = datetime(2026, 8, 17, 10, 0, tzinfo=tz)
            mock_dt.fromisoformat = datetime.fromisoformat

            res = client.get("/api/economic-calendar/today")
            assert res.status_code == 503

def test_api_week_partial_overlap_and_no_overlap():
    cal = _build_test_calendar(coverage_start="2026-08-17")
    with patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load", return_value=cal), \
         patch("app.services.economic_calendar_state_store.economic_calendar_state_store.is_stale", return_value=False):
        
        with patch("app.api.market.datetime") as mock_dt:
            tz = ZoneInfo("Europe/Istanbul")
            mock_dt.now.return_value = datetime(2026, 8, 17, 10, 0, tzinfo=tz)
            mock_dt.fromisoformat = datetime.fromisoformat

            # Current week (Aug 17 - Aug 23): fully in coverage
            res = client.get("/api/economic-calendar/week?offset=0")
            assert res.status_code == 200
            data = res.json()
            assert data["is_complete"] is True

            # Distant week (offset 30 -> 30 weeks later): completely outside coverage -> 422
            res2 = client.get("/api/economic-calendar/week?offset=30")
            assert res2.status_code == 422

def test_api_month_partial_overlap():
    # Calendar starts on Aug 17, so Aug 1 - Aug 31 is partially covered
    cal = _build_test_calendar(coverage_start="2026-08-17")
    with patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load", return_value=cal), \
         patch("app.services.economic_calendar_state_store.economic_calendar_state_store.is_stale", return_value=False):
        
        with patch("app.api.market.datetime") as mock_dt:
            tz = ZoneInfo("Europe/Istanbul")
            mock_dt.now.return_value = datetime(2026, 8, 17, 10, 0, tzinfo=tz)
            mock_dt.fromisoformat = datetime.fromisoformat

            res = client.get("/api/economic-calendar/month?offset=0")
            assert res.status_code == 200
            data = res.json()
            assert data["is_complete"] is False
            assert data["effective_start"] == "2026-08-17"

def test_api_range_boundary_validations():
    cal = _build_test_calendar(coverage_start="2026-08-17")
    with patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load", return_value=cal), \
         patch("app.services.economic_calendar_state_store.economic_calendar_state_store.is_stale", return_value=False):
        
        # Exact 91 days inclusive (Aug 17 + 90 days = Nov 15) -> 200 OK
        res1 = client.get("/api/economic-calendar?start_date=2026-08-17&end_date=2026-11-15")
        assert res1.status_code == 200

        # 92 days inclusive (Aug 17 + 91 days = Nov 16) -> 400 Bad Request
        res2 = client.get("/api/economic-calendar?start_date=2026-08-17&end_date=2026-11-16")
        assert res2.status_code == 400
        assert "cannot exceed 91 calendar days" in res2.json()["detail"]

        # Outside coverage -> 422 Unprocessable Entity
        res3 = client.get("/api/economic-calendar?start_date=2026-08-01&end_date=2026-08-31")
        assert res3.status_code == 422

def test_api_warnings_are_deduplicated_across_endpoints():
    warn_text = "Minor provider telemetry latency"
    cal = _build_test_calendar(coverage_start="2026-08-17", warnings=[warn_text, warn_text])
    with patch("app.services.economic_calendar_state_store.economic_calendar_state_store.load", return_value=cal), \
         patch("app.services.economic_calendar_state_store.economic_calendar_state_store.is_stale", return_value=False):
        
        with patch("app.api.market.datetime") as mock_dt:
            tz = ZoneInfo("Europe/Istanbul")
            mock_dt.now.return_value = datetime(2026, 8, 17, 10, 0, tzinfo=tz)
            mock_dt.fromisoformat = datetime.fromisoformat

            for path in [
                "/api/economic-calendar/today",
                "/api/economic-calendar/week?offset=0",
                "/api/economic-calendar/month?offset=0",
                "/api/economic-calendar?start_date=2026-08-17&end_date=2026-08-20"
            ]:
                res = client.get(path)
                assert res.status_code == 200
                data = res.json()
                assert data["warnings"].count(warn_text) == 1
