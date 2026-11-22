import re
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.models.earnings import (
    BistEarningsCalendar,
    BistEarningsEvent,
    KapEarningsFetchMetrics,
    KapEarningsFetchResult,
    KapSymbolEarningsStatus,
    NasdaqEarningsCalendar,
    NasdaqEarningsEvent,
)
from app.services.bist100_earnings import (
    Bist100EarningsService,
    build_bist100_rolling_calendar,
)
from app.services.nasdaq100_earnings import (
    Nasdaq100EarningsService,
    build_nasdaq100_rolling_calendar,
)

# --- A. Coverage Calculation Test ---

def test_rolling_coverage_91_calendar_days():
    as_of = date(2026, 8, 13)
    coverage_start = as_of
    coverage_end = as_of + timedelta(days=90)

    assert coverage_start.isoformat() == "2026-08-13"
    assert coverage_end.isoformat() == "2026-11-11"
    # Difference is 90 days, boundary inclusive count is 91 days
    assert (coverage_end - coverage_start) == timedelta(days=90)


# --- B. BIST Boundary & Whitelist Filtering Test ---

def test_bist_rolling_boundary_and_whitelist_filtering():
    as_of = date(2026, 8, 13)
    cov_start = as_of
    cov_end = as_of + timedelta(days=90)
    whitelist = {"CCOLA", "GARAN", "THYAO"}

    events = [
        BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-12"),  # Excluded (before)
        BistEarningsEvent(symbol="THYAO", company_name="THYAO", earnings_date="2026-08-13"),  # Included (start)
        BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-13"),  # Included (start, sort test)
        BistEarningsEvent(symbol="GARAN", company_name="GARAN", earnings_date="2026-09-01"),  # Included
        BistEarningsEvent(symbol="NONBIST", company_name="NONBIST", earnings_date="2026-09-01"),  # Excluded (non-whitelist)
        BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-11-11"),  # Included (end)
        BistEarningsEvent(symbol="GARAN", company_name="GARAN", earnings_date="2026-11-12"),  # Excluded (after)
    ]

    fetch_result = KapEarningsFetchResult(status="success", events=events)
    gen_dt = datetime(2026, 8, 13, 12, 0, 0, tzinfo=ZoneInfo("UTC"))

    cal = build_bist100_rolling_calendar(
        fetch_result=fetch_result,
        coverage_start=cov_start,
        coverage_end=cov_end,
        generated_at=gen_dt,
        whitelist=whitelist
    )

    assert cal.schema_version == "2.0"
    assert cal.coverage_start == "2026-08-13"
    assert cal.coverage_end == "2026-11-11"
    assert len(cal.events) == 4

    # Verify deterministic sorting (earnings_date asc, symbol asc)
    assert [(e.symbol, e.earnings_date) for e in cal.events] == [
        ("CCOLA", "2026-08-13"),
        ("THYAO", "2026-08-13"),
        ("GARAN", "2026-09-01"),
        ("CCOLA", "2026-11-11"),
    ]


# --- C. BIST Metadata Preservation Test ---

def test_bist_metadata_preservation():
    sym_results = [
        KapSymbolEarningsStatus(symbol="CCOLA", refresh_status="success", calendar_status="scheduled", is_complete=True),
        KapSymbolEarningsStatus(symbol="AKBNK", refresh_status="success", calendar_status="no_announced_date_in_coverage", is_complete=True),
        KapSymbolEarningsStatus(symbol="GARAN", refresh_status="failed", calendar_status="unavailable", is_complete=False)
    ]
    metrics = KapEarningsFetchMetrics(input_symbol_count=100, resolved_symbol_count=99, unmappable_disclosure_count=1)
    fetch_result = KapEarningsFetchResult(
        status="partial",
        events=[BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-15")],
        symbol_results=sym_results,
        warnings=["Unavailable KAP symbols: GARAN"],
        metrics=metrics
    )

    cal = build_bist100_rolling_calendar(
        fetch_result=fetch_result,
        coverage_start=date(2026, 8, 13),
        coverage_end=date(2026, 11, 11),
        generated_at=datetime.now(),
        whitelist={"CCOLA", "AKBNK", "GARAN"}
    )

    assert cal.status == "partial"
    assert cal.provider_status == "partial"
    assert len(cal.symbol_results) == 3

    sym_map = {s.symbol: s for s in cal.symbol_results}
    assert sym_map["GARAN"].calendar_status == "unavailable"
    assert not sym_map["GARAN"].is_complete
    assert cal.warnings == ["Unavailable KAP symbols: GARAN"]
    assert cal.metrics.unmappable_disclosure_count == 1


# --- D. BIST Date-Only Format Test ---

def test_bist_date_only_format():
    event = BistEarningsEvent(
        symbol="CCOLA",
        company_name="COCA COLA",
        earnings_date="2026-08-10",
        period_start="2026-04-01",
        period_end="2026-06-30",
        publish_date="2026-08-01"
    )

    pattern = r"^\d{4}-\d{2}-\d{2}$"
    assert re.match(pattern, event.earnings_date)
    assert re.match(pattern, event.period_start)
    assert re.match(pattern, event.period_end)
    assert re.match(pattern, event.publish_date)


# --- E. Nasdaq Coverage & Session Ordering Test ---

def test_nasdaq_rolling_coverage_and_session_ordering():
    as_of = date(2026, 8, 13)
    cov_start = as_of
    cov_end = as_of + timedelta(days=90)
    whitelist = {"AAPL", "MSFT", "NVDA", "AMZN"}

    events = [
        NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-12", session="pre_market", session_label="Pre-Market"),  # Excluded
        NasdaqEarningsEvent(symbol="AMZN", company_name="Amazon", earnings_date="2026-08-13", session="after_hours", session_label="After Hours"),  # 2026-08-13 pos 2
        NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-13", session="pre_market", session_label="Pre-Market"),    # 2026-08-13 pos 0
        NasdaqEarningsEvent(symbol="MSFT", company_name="Microsoft", earnings_date="2026-08-13", session="time_not_supplied", session_label="Time Not Supplied"), # 2026-08-13 pos 1
        NasdaqEarningsEvent(symbol="NVDA", company_name="Nvidia", earnings_date="2026-08-13", session="pre_market", session_label="Pre-Market"),   # 2026-08-13 pos 0 (symbol sort)
        NasdaqEarningsEvent(symbol="NONNASDAQ", company_name="NonNasdaq", earnings_date="2026-09-01", session="pre_market", session_label="Pre-Market"), # Excluded
    ]

    cal = build_nasdaq100_rolling_calendar(
        events=events,
        warnings=[],
        coverage_start=cov_start,
        coverage_end=cov_end,
        generated_at=datetime(2026, 8, 13, 12, 0, 0),
        whitelist=whitelist
    )

    assert cal.schema_version == "2.0"
    assert cal.status == "success"
    assert cal.provider_status == "success"
    assert len(cal.events) == 4

    # Order check on 2026-08-13: AAPL (pre), NVDA (pre), MSFT (time_not_supplied), AMZN (after_hours)
    ordered_syms = [e.symbol for e in cal.events]
    assert ordered_syms == ["AAPL", "NVDA", "MSFT", "AMZN"]


# --- F. Healthy Empty Nasdaq Result Test ---

def test_nasdaq_healthy_empty_result():
    cal = build_nasdaq100_rolling_calendar(
        events=[],
        warnings=[],
        coverage_start=date(2026, 8, 13),
        coverage_end=date(2026, 11, 11),
        generated_at=datetime.now(),
        whitelist={"AAPL"}
    )
    assert cal.status == "success"
    assert cal.provider_status == "success"
    assert len(cal.events) == 0


# --- G. Nasdaq Partial Status Test ---

def test_nasdaq_partial_status():
    event = NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-15", session="pre_market", session_label="Pre-Market")
    cal = build_nasdaq100_rolling_calendar(
        events=[event],
        warnings=["Yahoo pagination timeout for ticker group"],
        coverage_start=date(2026, 8, 13),
        coverage_end=date(2026, 11, 11),
        generated_at=datetime.now(),
        whitelist={"AAPL"}
    )
    assert cal.status == "partial"
    assert cal.provider_status == "partial"
    assert len(cal.events) == 1


# --- H. Exception Handling Tests ---

@pytest.mark.asyncio
async def test_bist_service_exception_returns_failed_schema_2(caplog):
    from app.providers.earnings.official_ir.registry import OfficialIrRegistry
    service = Bist100EarningsService(official_ir_reg=OfficialIrRegistry(providers=[]))
    with patch.object(service.provider, "get_earnings_result", side_effect=RuntimeError("KAP Connection Crash")):
        cal = await service.get_rolling_calendar(as_of=datetime(2026, 8, 13, 10, 0, 0))

        assert cal.schema_version == "2.0"
        assert cal.status == "failed"
        assert cal.provider_status == "failed"
        assert len(cal.events) == 0
        assert any("KAP fetch exception" in w or "Failed" in w for w in cal.warnings)
        assert "NoneType: None" not in caplog.text
        assert "KAP Connection Crash" in caplog.text


@pytest.mark.asyncio
async def test_nasdaq_service_exception_returns_failed_schema_2():
    service = Nasdaq100EarningsService()
    with patch.object(service.provider, "get_earnings_events", side_effect=RuntimeError("Yahoo HTTP Error 500")):
        cal = await service.get_rolling_calendar(as_of=datetime(2026, 8, 13, 10, 0, 0))

        assert cal.schema_version == "2.0"
        assert cal.status == "failed"
        assert cal.provider_status == "failed"
        assert len(cal.events) == 0
        assert any("Failed to fetch NASDAQ100 earnings" in w for w in cal.warnings)


# --- I. Schema 1.0 Backward Compatibility Test ---

@pytest.mark.asyncio
async def test_schema_1_0_backward_compatibility():
    bist_v1 = BistEarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        week_start="2026-08-10",
        week_end="2026-08-16"
    )
    assert bist_v1.schema_version == "1.0"

    nasdaq_v1 = NasdaqEarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        week_start="2026-08-10",
        week_end="2026-08-16"
    )
    assert nasdaq_v1.schema_version == "1.0"
