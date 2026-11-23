from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.api.earnings as earnings_api_module
from app.main import app
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsCalendar,
    BistEarningsEvent,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsCalendar,
    NasdaqEarningsEvent,
)

client = TestClient(app)

TZ_TR = ZoneInfo("Europe/Istanbul")
FIXED_NOW = datetime(2026, 8, 18, 10, 0, 0, tzinfo=TZ_TR)  # Tuesday, 18 August 2026


def make_bist_cache(
    status="success",
    provider_status="success",
    cov_start="2026-08-01",
    cov_end="2026-10-31",
    generated_at=None,
    events=None,
    warnings=None,
):
    if generated_at is None:
        generated_at = FIXED_NOW.isoformat()
    if events is None:
        sample_events = [
            BistEarningsEvent(
                symbol="CCOLA",
                company_name="COCA-COLA İÇECEK A.Ş.",
                fiscal_year=2026,
                fiscal_period="Q2",
                earnings_date="2026-08-18",
                period_start="2026-01-01",
                period_end="2026-06-30",
                publish_date="2026-08-01",
                source="KAP",
                source_provider="KAP Financial Calendar",
                source_type="financial_calendar",
                source_event_id="KAP:CCOLA:2026-Q2",
                disclosure_index=1627747,
                source_url="https://www.kap.org.tr/tr/Bildirim/1627747",
            ),
            BistEarningsEvent(
                symbol="THYAO",
                company_name="TÜRK HAVA YOLLARI A.O.",
                fiscal_year=2026,
                fiscal_period="Q2",
                earnings_date="2026-08-20",
                period_start="2026-01-01",
                period_end="2026-06-30",
                publish_date="2026-08-05",
                source="Official IR",
                source_provider="THYAO Official IR",
                source_type="official_financial_calendar",
                source_event_id="THYAO:IR:2026-Q2",
                source_url="https://investor.turkishairlines.com",
            ),
            BistEarningsEvent(
                symbol="AKBNK",
                company_name="AKBANK T.A.Ş.",
                fiscal_year=2026,
                fiscal_period="Q3",
                earnings_date="2026-10-25",
                period_start="2026-01-01",
                period_end="2026-09-30",
                source="KAP",
                disclosure_index=1627800,
            ),
        ]
        events = [ev for ev in sample_events if cov_start <= ev.earnings_date <= cov_end]
    if warnings is None:
        warnings = ["BIST Cache Notice 1", "BIST Cache Notice 2"]

    return Bist100EarningsCalendar.model_construct(
        schema_version="2.0",
        generated_at=generated_at,
        coverage_start=cov_start,
        coverage_end=cov_end,
        market="BIST100",
        status=status,
        provider_status=provider_status,
        events=events,
        warnings=warnings,
    )


def make_nasdaq_cache(
    status="success",
    provider_status="success",
    cov_start="2026-08-01",
    cov_end="2026-10-31",
    generated_at=None,
    events=None,
    warnings=None,
):
    if generated_at is None:
        generated_at = FIXED_NOW.isoformat()
    if events is None:
        sample_events = [
            NasdaqEarningsEvent(
                symbol="NVDA",
                company_name="NVIDIA Corporation",
                earnings_date="2026-08-18",
                session="after_hours",
                session_label="After Hours",
                source="Yahoo Finance",
            ),
            NasdaqEarningsEvent(
                symbol="AAPL",
                company_name="Apple Inc.",
                earnings_date="2026-08-18",
                session="pre_market",
                session_label="Pre-Market",
                source="Yahoo Finance",
            ),
            NasdaqEarningsEvent(
                symbol="MSFT",
                company_name="Microsoft Corporation",
                earnings_date="2026-08-20",
                session="time_not_supplied",
                session_label="Time Not Supplied",
                source="Yahoo Finance",
            ),
            NasdaqEarningsEvent(
                symbol="AMZN",
                company_name="Amazon.com, Inc.",
                earnings_date="2026-10-28",
                session="after_hours",
                session_label="After Hours",
                source="Yahoo Finance",
            ),
        ]
        events = [ev for ev in sample_events if cov_start <= ev.earnings_date <= cov_end]
    if warnings is None:
        warnings = ["NASDAQ Cache Notice 1"]

    return Nasdaq100EarningsCalendar.model_construct(
        schema_version="2.0",
        generated_at=generated_at,
        coverage_start=cov_start,
        coverage_end=cov_end,
        market="NASDAQ100",
        status=status,
        provider_status=provider_status,
        events=events,
        warnings=warnings,
    )


# =============================================================================
# PART 1: Market-Specific BIST100 & NASDAQ100 Tests
# =============================================================================

@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_bist_missing_cache_returns_503(mock_now):
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=None):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 503
        assert "supabase earnings data is unavailable" in resp.json()["detail"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_nasdaq_missing_cache_returns_503(mock_now):
    with patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=None):
        resp = client.get("/api/earnings/nasdaq100/today")
        assert resp.status_code == 503
        assert "supabase earnings data is unavailable" in resp.json()["detail"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_bist_failed_status_cache_returns_503(mock_now):
    failed_cal = make_bist_cache(status="failed")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=failed_cal):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 503


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_today_with_matching_events(mock_now):
    bist_cal = make_bist_cache()
    nasdaq_cal = make_nasdaq_cache()
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp_b = client.get("/api/earnings/bist100/today")
        assert resp_b.status_code == 200
        data_b = resp_b.json()
        assert data_b["market"] == "BIST100"
        assert data_b["schema_version"] == "2.0"
        assert data_b["date"] == "2026-08-18"
        assert len(data_b["events"]) == 1
        assert data_b["events"][0]["symbol"] == "CCOLA"

    with patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp_n = client.get("/api/earnings/nasdaq100/today")
        assert resp_n.status_code == 200
        data_n = resp_n.json()
        assert data_n["market"] == "NASDAQ100"
        assert data_n["date"] == "2026-08-18"
        assert len(data_n["events"]) == 2
        # Session order: pre_market first (AAPL), after_hours second (NVDA)
        assert data_n["events"][0]["symbol"] == "AAPL"
        assert data_n["events"][1]["symbol"] == "NVDA"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_today_healthy_empty_returns_200(mock_now):
    bist_cal = make_bist_cache(events=[])
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []
        assert data["status"] == "success"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_today_outside_coverage_returns_503(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-09-01", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 503
        assert "outside" in resp.json()["detail"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_week_partial_overlap_returns_200_and_incomplete(mock_now):
    # Today is Tue 18 Aug 2026. Requested week: 17 Aug - 23 Aug.
    # Cache coverage starts on 18 Aug.
    bist_cal = make_bist_cache(cov_start="2026-08-18", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/week?offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["schema_version"] == "1.0"
        assert data["week_start"] == "2026-08-17"
        assert data["week_end"] == "2026-08-23"
        assert data["requested_start"] == "2026-08-17"
        assert data["requested_end"] == "2026-08-23"
        assert data["effective_start"] == "2026-08-18"
        assert data["effective_end"] == "2026-08-23"
        assert data["is_complete"] is False
        assert any("kısmen" in w for w in data["warnings"])


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_week_full_overlap_returns_200_and_complete(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-08-01", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/week?offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_complete"] is True
        assert data["effective_start"] == "2026-08-17"
        assert data["effective_end"] == "2026-08-23"
        assert len(data["events"]) == 2  # CCOLA (18 Aug) and THYAO (20 Aug)


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_week_no_overlap_returns_422(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-09-01", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/week?offset=0")
        assert resp.status_code == 422
        assert "no overlap" in resp.json()["detail"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_month_partial_overlap_returns_200_incomplete(mock_now):
    # August 2026 (2026-08-01 to 2026-08-31), cache starts 2026-08-10
    bist_cal = make_bist_cache(cov_start="2026-08-10", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/month?offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["schema_version"] == "2.0"
        assert data["month_start"] == "2026-08-01"
        assert data["month_end"] == "2026-08-31"
        assert data["effective_start"] == "2026-08-10"
        assert data["effective_end"] == "2026-08-31"
        assert data["is_complete"] is False


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_month_no_overlap_returns_422(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-10-01", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/month?offset=0")  # August 2026
        assert resp.status_code == 422


@patch("app.api.earnings._now_istanbul", return_value=datetime(2026, 12, 15, 10, 0, tzinfo=TZ_TR))
def test_month_december_offset_transitions_to_january(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-12-01", cov_end="2027-02-28")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/month?offset=1")  # January 2027
        assert resp.status_code == 200
        data = resp.json()
        assert data["month_start"] == "2027-01-01"
        assert data["month_end"] == "2027-01-31"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_range_91_days_accepted(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-08-01", cov_end="2026-11-30")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        d_start = "2026-08-01"
        d_end = (date(2026, 8, 1) + timedelta(days=90)).strftime("%Y-%m-%d")
        resp = client.get(f"/api/earnings/bist100?start_date={d_start}&end_date={d_end}")
        assert resp.status_code == 200
        assert resp.json()["is_complete"] is True


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_range_92_days_returns_400(mock_now):
    d_start = "2026-08-01"
    d_end = (date(2026, 8, 1) + timedelta(days=91)).strftime("%Y-%m-%d")
    resp = client.get(f"/api/earnings/bist100?start_date={d_start}&end_date={d_end}")
    assert resp.status_code == 400
    assert "91" in resp.json()["detail"]


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_range_invalid_date_format_returns_400(mock_now):
    resp = client.get("/api/earnings/bist100?start_date=invalid&end_date=2026-08-20")
    assert resp.status_code == 400


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_range_start_after_end_returns_400(mock_now):
    resp = client.get("/api/earnings/bist100?start_date=2026-08-20&end_date=2026-08-10")
    assert resp.status_code == 400


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_range_outside_coverage_returns_422(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-08-10", cov_end="2026-08-30")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100?start_date=2026-08-01&end_date=2026-08-20")
        assert resp.status_code == 422


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_stale_cache_detection(mock_now):
    old_time = (FIXED_NOW - timedelta(hours=37)).isoformat()
    bist_cal = make_bist_cache(generated_at=old_time)
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stale"] is True
        assert data["status"] == "success"  # Stale alone does not degrade status


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_reversed_coverage_market_specific_returns_503(mock_now):
    """Reversed coverage (start > end) must be treated as invalid_cache (503), not outside coverage (422)."""
    bist_cal = make_bist_cache(cov_start="2026-11-16", cov_end="2026-08-18")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/week?offset=0")
        assert resp.status_code == 503
        assert "reversed" in resp.json()["detail"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_event_date_outside_coverage_returns_503(mock_now):
    """Event date outside cache coverage window is an integrity failure (invalid cache -> 503)."""
    bad_events = [
        BistEarningsEvent(symbol="ASELS", company_name="Aselsan", earnings_date="2026-11-15")
    ]
    bist_cal = make_bist_cache(cov_start="2026-08-01", cov_end="2026-10-31", events=bad_events)
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 503
        assert "outside cache coverage" in resp.json()["detail"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_huge_week_and_month_offset_returns_400_not_500(mock_now):
    """Excessive offsets must return 400 Bad Request instead of unhandled 500 OverflowError."""
    resp_w = client.get("/api/earnings/bist100/week?offset=999999999")
    assert resp_w.status_code == 400
    assert "offset" in resp_w.json()["detail"].lower()

    resp_m = client.get("/api/earnings/bist100/month?offset=100000")
    assert resp_m.status_code == 400
    assert "offset" in resp_m.json()["detail"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_negative_offset_returns_422(mock_now):
    """Negative offset is rejected by query param validation (ge=0)."""
    resp = client.get("/api/earnings/bist100/week?offset=-1")
    assert resp.status_code == 422


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_bist_partial_cache_returns_200_partial(mock_now):
    bist_cal = make_bist_cache(status="partial", provider_status="partial")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 200
        assert resp.json()["status"] == "partial"
        assert resp.json()["provider_status"] == "partial"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_bist_deterministic_sorting(mock_now):
    events = [
        BistEarningsEvent(symbol="THYAO", company_name="THY", fiscal_year=2026, fiscal_period="Q2", earnings_date="2026-08-18"),
        BistEarningsEvent(symbol="ASELS", company_name="Aselsan", fiscal_year=2026, fiscal_period="Q1", earnings_date="2026-08-18"),
        BistEarningsEvent(symbol="ASELS", company_name="Aselsan", fiscal_year=2026, fiscal_period="Q2", earnings_date="2026-08-18"),
        BistEarningsEvent(symbol="AKBNK", company_name="Akbank", fiscal_year=2026, fiscal_period="Q2", earnings_date="2026-08-18"),
    ]
    bist_cal = make_bist_cache(events=events)
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 200
        symbols = [e["symbol"] for e in resp.json()["events"]]
        assert symbols == ["AKBNK", "ASELS", "ASELS", "THYAO"]
        assert resp.json()["events"][1]["fiscal_period"] == "Q1"
        assert resp.json()["events"][2]["fiscal_period"] == "Q2"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_nasdaq_session_sorting(mock_now):
    events = [
        NasdaqEarningsEvent(symbol="B", company_name="B", earnings_date="2026-08-18", session="after_hours", session_label="After Hours"),
        NasdaqEarningsEvent(symbol="A", company_name="A", earnings_date="2026-08-18", session="after_hours", session_label="After Hours"),
        NasdaqEarningsEvent(symbol="C", company_name="C", earnings_date="2026-08-18", session="time_not_supplied", session_label="Time Not Supplied"),
        NasdaqEarningsEvent(symbol="D", company_name="D", earnings_date="2026-08-18", session="pre_market", session_label="Pre-Market"),
    ]
    nasdaq_cal = make_nasdaq_cache(events=events)
    with patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/nasdaq100/today")
        assert resp.status_code == 200
        symbols = [e["symbol"] for e in resp.json()["events"]]
        assert symbols == ["D", "C", "A", "B"]


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_kap_and_official_ir_provenance_preservation(mock_now):
    bist_cal = make_bist_cache()
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/week?offset=0")
        assert resp.status_code == 200
        events = resp.json()["events"]
        ccola = next(e for e in events if e["symbol"] == "CCOLA")
        assert ccola["source"] == "KAP"
        assert ccola["source_provider"] == "KAP Financial Calendar"
        assert ccola["source_type"] == "financial_calendar"
        assert ccola["disclosure_index"] == 1627747
        assert ccola["source_url"] == "https://www.kap.org.tr/tr/Bildirim/1627747"

        thyao = next(e for e in events if e["symbol"] == "THYAO")
        assert thyao["source"] == "Official IR"
        assert thyao["source_provider"] == "THYAO Official IR"
        assert thyao["source_type"] == "official_financial_calendar"
        assert thyao["source_event_id"] == "THYAO:IR:2026-Q2"
        assert thyao["source_url"] == "https://investor.turkishairlines.com"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_legacy_week_fields_and_schema_version_1_0(mock_now):
    bist_cal = make_bist_cache()
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/week?offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["schema_version"] == "1.0"
        assert "generated_at" in data
        assert "week_start" in data
        assert "week_end" in data
        assert "market" in data
        assert "status" in data
        assert "provider_status" in data
        assert "events" in data
        assert "warnings" in data


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_generated_at_from_cache_not_request_time(mock_now):
    custom_gen_at = "2026-08-17T08:30:00+03:00"
    bist_cal = make_bist_cache(generated_at=custom_gen_at)
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal):
        resp = client.get("/api/earnings/bist100/today")
        assert resp.status_code == 200
        assert resp.json()["generated_at"] == custom_gen_at


# =============================================================================
# PART 2: Combined BIST100 + NASDAQ100 & Fault Isolation Tests
# =============================================================================

@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_both_success_returns_200_top_success(mock_now):
    bist_cal = make_bist_cache(status="success")
    nasdaq_cal = make_nasdaq_cache(status="success")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["schema_version"] == "2.0"
        assert data["view"] == "today"
        assert data["status"] == "success"
        assert data["is_complete"] is True
        assert data["stale"] is False
        assert data["bist100"] is not None
        assert data["nasdaq100"] is not None
        assert len(data["market_errors"]) == 0
        assert len(data["bist100"]["events"]) == 1
        assert len(data["nasdaq100"]["events"]) == 2


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_bist_partial_nasdaq_success_makes_top_level_partial(mock_now):
    bist_cal = make_bist_cache(status="partial")
    nasdaq_cal = make_nasdaq_cache(status="success")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["bist100"]["status"] == "partial"
        assert data["nasdaq100"]["status"] == "success"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_bist_success_nasdaq_partial_makes_top_level_partial(mock_now):
    bist_cal = make_bist_cache(status="success")
    nasdaq_cal = make_nasdaq_cache(status="partial")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["bist100"]["status"] == "success"
        assert data["nasdaq100"]["status"] == "partial"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_bist_missing_nasdaq_success_returns_200_partial(mock_now):
    nasdaq_cal = make_nasdaq_cache(status="success")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=None), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["is_complete"] is False
        assert data["bist100"] is None
        assert data["nasdaq100"] is not None
        assert "BIST100" in data["market_errors"]


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_bist_success_nasdaq_missing_returns_200_partial(mock_now):
    bist_cal = make_bist_cache(status="success")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=None):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["is_complete"] is False
        assert data["bist100"] is not None
        assert data["nasdaq100"] is None
        assert "NASDAQ100" in data["market_errors"]


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_both_missing_returns_503(mock_now):
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=None), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=None):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower() or "both" in resp.json()["detail"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_one_market_coverage_outside_returns_200_partial(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-09-01", cov_end="2026-10-31")
    nasdaq_cal = make_nasdaq_cache(cov_start="2026-08-01", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["bist100"] is None
        assert "BIST100" in data["market_errors"]
        assert data["nasdaq100"] is not None
        assert len(data["nasdaq100"]["events"]) == 2


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_both_markets_out_of_coverage_week_returns_422(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-09-01", cov_end="2026-10-31")
    nasdaq_cal = make_nasdaq_cache(cov_start="2026-09-01", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/week?offset=0")
        assert resp.status_code == 422


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_both_markets_out_of_coverage_today_returns_503(mock_now):
    bist_cal = make_bist_cache(cov_start="2026-09-01", cov_end="2026-10-31")
    nasdaq_cal = make_nasdaq_cache(cov_start="2026-09-01", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 503


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_separate_generated_at_and_coverages_preserved(mock_now):
    bist_gen = "2026-08-18T06:00:00+03:00"
    nasdaq_gen = "2026-08-18T07:15:00+03:00"
    bist_cal = make_bist_cache(generated_at=bist_gen, cov_start="2026-08-10", cov_end="2026-11-10")
    nasdaq_cal = make_nasdaq_cache(generated_at=nasdaq_gen, cov_start="2026-08-01", cov_end="2026-10-31")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/week?offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bist100"]["generated_at"] == bist_gen
        assert data["bist100"]["coverage_start"] == "2026-08-10"
        assert data["nasdaq100"]["generated_at"] == nasdaq_gen
        assert data["nasdaq100"]["coverage_start"] == "2026-08-01"


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_one_stale_makes_top_level_stale_true_without_degrading_status(mock_now):
    old_gen = (FIXED_NOW - timedelta(hours=40)).isoformat()
    bist_cal = make_bist_cache(generated_at=old_gen, status="success")
    nasdaq_cal = make_nasdaq_cache(generated_at=FIXED_NOW.isoformat(), status="success")
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["stale"] is True
        assert data["bist100"]["stale"] is True
        assert data["nasdaq100"]["stale"] is False


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_warnings_deduplicated_at_top_level(mock_now):
    bist_cal = make_bist_cache(warnings=["Shared Warning", "BIST unique"])
    nasdaq_cal = make_nasdaq_cache(warnings=["Shared Warning", "NASDAQ unique"])
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["warnings"] == ["Shared Warning", "BIST unique", "NASDAQ unique"]


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_range_view_works(mock_now):
    bist_cal = make_bist_cache()
    nasdaq_cal = make_nasdaq_cache()
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings?start_date=2026-08-15&end_date=2026-08-25")
        assert resp.status_code == 200
        data = resp.json()
        assert data["view"] == "range"
        assert data["requested_start"] == "2026-08-15"
        assert data["requested_end"] == "2026-08-25"
        assert len(data["bist100"]["events"]) == 2
        assert len(data["nasdaq100"]["events"]) == 3


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_bist_malformed_coverage_and_nasdaq_healthy_isolates_error(mock_now):
    bad_bist = make_bist_cache(cov_start="NOT-A-DATE")
    nasdaq_cal = make_nasdaq_cache(status="success")

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bad_bist), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["is_complete"] is False
        assert data["bist100"] is None
        assert data["nasdaq100"] is not None
        assert len(data["nasdaq100"]["events"]) == 2
        assert "BIST100" in data["market_errors"]
        assert "coverage_start" in data["market_errors"]["BIST100"].lower()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_nasdaq_malformed_event_and_bist_healthy_isolates_error(mock_now):
    bist_cal = make_bist_cache(status="success")
    bad_nasdaq_events = [
        NasdaqEarningsEvent.model_construct(symbol="NVDA", company_name="Nvidia", earnings_date="NOT-A-DATE", session="after_hours", session_label="After Hours")
    ]
    bad_nasdaq = make_nasdaq_cache(events=bad_nasdaq_events)

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=bad_nasdaq):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["is_complete"] is False
        assert data["bist100"] is not None
        assert data["nasdaq100"] is None
        assert len(data["bist100"]["events"]) == 1
        assert "NASDAQ100" in data["market_errors"]


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_bist_malformed_generated_at_and_nasdaq_healthy_isolates_error(mock_now):
    bad_bist = make_bist_cache(generated_at="2026-08-18T10:00:00")  # Naive without timezone
    nasdaq_cal = make_nasdaq_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bad_bist), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["bist100"] is None
        assert "timezone" in data["market_errors"]["BIST100"].lower()
        assert data["nasdaq100"] is not None


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_two_malformed_caches_returns_503(mock_now):
    bad_bist = make_bist_cache(cov_start="NOT-A-DATE")
    bad_nasdaq = make_nasdaq_cache(cov_start="NOT-A-DATE")

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bad_bist), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=bad_nasdaq):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 503


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_missing_and_other_outside_coverage_returns_503(mock_now):
    nasdaq_cal = make_nasdaq_cache(cov_start="2026-09-01", cov_end="2026-10-31")

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=None), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/week?offset=0")  # August 2026
        assert resp.status_code == 503


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_reversed_coverage_combined_isolation(mock_now):
    bad_bist = make_bist_cache(cov_start="2026-11-16", cov_end="2026-08-18")
    nasdaq_cal = make_nasdaq_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bad_bist), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bist100"] is None
        assert "reversed" in data["market_errors"]["BIST100"].lower()
        assert data["nasdaq100"] is not None


# =============================================================================
# PART 3: Exact Single Read Guarantee Tests (All Branches)
# =============================================================================

@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_single_read_both_full(mock_now):
    """Branch A: Both caches full -> load_bist and load_nasdaq each called exactly once."""
    bist_cal = make_bist_cache()
    nasdaq_cal = make_nasdaq_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal) as mock_lb, \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal) as mock_ln:
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        mock_lb.assert_called_once()
        mock_ln.assert_called_once()


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_single_read_bist_missing(mock_now):
    """Branch B: BIST missing, Nasdaq healthy -> load_bist called once (no re-read), load_nasdaq called once."""
    nasdaq_cal = make_nasdaq_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=None) as mock_lb, \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal) as mock_ln:
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        assert resp.json()["status"] == "partial"
        assert resp.json()["bist100"] is None
        assert resp.json()["nasdaq100"] is not None
        assert mock_lb.call_count == 1
        assert mock_ln.call_count == 1


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_single_read_nasdaq_missing(mock_now):
    """Branch C: Nasdaq missing, BIST healthy -> load_bist called once, load_nasdaq called once (no re-read)."""
    bist_cal = make_bist_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal) as mock_lb, \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=None) as mock_ln:
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        assert resp.json()["status"] == "partial"
        assert resp.json()["bist100"] is not None
        assert resp.json()["nasdaq100"] is None
        assert mock_lb.call_count == 1
        assert mock_ln.call_count == 1


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_single_read_both_missing(mock_now):
    """Branch D: Both caches missing -> load_bist called once, load_nasdaq called once, returns 503."""
    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=None) as mock_lb, \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=None) as mock_ln:
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 503
        assert mock_lb.call_count == 1
        assert mock_ln.call_count == 1


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_single_read_bist_load_exception(mock_now):
    """Branch E: BIST load raises exception -> called once, original error recorded, Nasdaq intact."""
    nasdaq_cal = make_nasdaq_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", side_effect=RuntimeError("BIST disk read failed")) as mock_lb, \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal) as mock_ln:
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["bist100"] is None
        assert data["nasdaq100"] is not None
        assert "BIST disk read failed" in data["market_errors"]["BIST100"]
        assert mock_lb.call_count == 1
        assert mock_ln.call_count == 1


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_single_read_both_load_exceptions(mock_now):
    """Branch F: Both loads raise exceptions -> each called once, 503 response with diagnostic details."""
    with patch("app.api.earnings.calendar_state_store.load_bist", side_effect=RuntimeError("BIST disk read failed")) as mock_lb, \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", side_effect=RuntimeError("NASDAQ disk read failed")) as mock_ln:
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "BIST disk read failed" in detail
        assert "NASDAQ disk read failed" in detail
        assert mock_lb.call_count == 1
        assert mock_ln.call_count == 1


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_combined_volatile_disk_side_effect_guarantee(mock_now):
    """Branch G: side_effect=[None, healthy_bist] -> only 1 call made, 2nd value remains unconsumed."""
    healthy_bist = make_bist_cache()
    healthy_nasdaq = make_nasdaq_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", side_effect=[None, healthy_bist]) as mock_lb, \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=healthy_nasdaq) as mock_ln:
        resp = client.get("/api/earnings/today")
        assert resp.status_code == 200
        assert resp.json()["bist100"] is None  # Remains missing from the 1st read
        assert mock_lb.call_count == 1
        assert mock_ln.call_count == 1


# =============================================================================
# PART 4: Real Loader Unknown Session & Model Validation Tests
# =============================================================================

def test_unknown_nasdaq_session_causes_model_validation_failure():
    """Unknown session is rejected directly by NasdaqEarningsEvent model."""
    with pytest.raises(ValidationError):
        NasdaqEarningsEvent(
            symbol="NVDA",
            company_name="Nvidia",
            earnings_date="2026-08-18",
            session="mystery_session",  # Invalid
            session_label="Mystery",
        )


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_unknown_nasdaq_session_endpoint_isolation(mock_now):
    """If Nasdaq cache file has unknown session, load_nasdaq returns None, market-specific returns 503, combined returns 200 partial."""
    bist_cal = make_bist_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=None):
        resp_market = client.get("/api/earnings/nasdaq100/today")
        assert resp_market.status_code == 503

        resp_comb = client.get("/api/earnings/today")
        assert resp_comb.status_code == 200
        data = resp_comb.json()
        assert data["status"] == "partial"
        assert data["bist100"] is not None
        assert data["nasdaq100"] is None
        assert "NASDAQ100" in data["market_errors"]


# =============================================================================
# PART 5: Legacy Schema & No-Live-Provider Tests
# =============================================================================

@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_legacy_models_can_parse_week_responses(mock_now):
    """Ensure response from /week endpoints can be parsed by legacy BistEarningsCalendar and NasdaqEarningsCalendar models."""
    bist_cal = make_bist_cache()
    nasdaq_cal = make_nasdaq_cache()

    with patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):
        resp_b = client.get("/api/earnings/bist100/week?offset=0")
        assert resp_b.status_code == 200
        parsed_b = BistEarningsCalendar.model_validate(resp_b.json())
        assert parsed_b.market == "BIST100"
        assert parsed_b.schema_version == "1.0"
        assert len(parsed_b.events) == 2

        resp_n = client.get("/api/earnings/nasdaq100/week?offset=0")
        assert resp_n.status_code == 200
        parsed_n = NasdaqEarningsCalendar.model_validate(resp_n.json())
        assert parsed_n.market == "NASDAQ100"
        assert parsed_n.schema_version == "1.0"
        assert len(parsed_n.events) == 3


def test_api_module_has_no_live_service_imports():
    """Verify that app/api/earnings.py does not import live service or provider singletons."""
    assert not hasattr(earnings_api_module, "bist100_earnings_service")
    assert not hasattr(earnings_api_module, "nasdaq100_earnings_service")
    assert not hasattr(earnings_api_module, "KapEarningsProvider")
    assert not hasattr(earnings_api_module, "OfficialIrRegistry")
    assert not hasattr(earnings_api_module, "YahooEarningsProvider")


@patch("app.api.earnings._now_istanbul", return_value=FIXED_NOW)
def test_endpoints_never_call_live_providers(mock_now):
    """Ensure all endpoints execute without calling live services/providers."""
    bist_cal = make_bist_cache()
    nasdaq_cal = make_nasdaq_cache()

    with patch("app.services.bist100_earnings.Bist100EarningsService.get_week_events", side_effect=RuntimeError("Live call!")) as mock_bist_svc, \
         patch("app.services.nasdaq100_earnings.Nasdaq100EarningsService.get_week_events", side_effect=RuntimeError("Live call!")) as mock_nasdaq_svc, \
         patch("app.providers.earnings.kap_provider.KapEarningsProvider.get_earnings_result", side_effect=RuntimeError("Live call!")) as mock_kap, \
         patch("app.providers.earnings.official_ir.registry.OfficialIrRegistry.fetch_all", side_effect=RuntimeError("Live call!")) as mock_ir, \
         patch("app.providers.earnings.yahoo_provider.YahooEarningsProvider.get_earnings_events", side_effect=RuntimeError("Live call!")) as mock_yahoo, \
         patch("app.api.earnings.calendar_state_store.load_bist", return_value=bist_cal), \
         patch("app.api.earnings.calendar_state_store.load_nasdaq", return_value=nasdaq_cal):

        endpoints = [
            "/api/earnings/bist100/today",
            "/api/earnings/bist100/week?offset=0",
            "/api/earnings/bist100/month?offset=0",
            "/api/earnings/bist100?start_date=2026-08-18&end_date=2026-08-25",
            "/api/earnings/nasdaq100/today",
            "/api/earnings/nasdaq100/week?offset=0",
            "/api/earnings/nasdaq100/month?offset=0",
            "/api/earnings/nasdaq100?start_date=2026-08-18&end_date=2026-08-25",
            "/api/earnings/today",
            "/api/earnings/week?offset=0",
            "/api/earnings/month?offset=0",
            "/api/earnings?start_date=2026-08-18&end_date=2026-08-25",
        ]

        for ep in endpoints:
            res = client.get(ep)
            assert res.status_code == 200, f"Endpoint {ep} failed with {res.status_code}: {res.text}"

        mock_bist_svc.assert_not_called()
        mock_nasdaq_svc.assert_not_called()
        mock_kap.assert_not_called()
        mock_ir.assert_not_called()
        mock_yahoo.assert_not_called()

