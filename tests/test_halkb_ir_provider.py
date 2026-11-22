import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.models.earnings import BistEarningsEvent, OfficialIrProviderResult
from app.providers.earnings.official_ir.halkb import (
    PRIMARY_URL,
    SECONDARY_URL,
    HalkbIrProvider,
)
from app.providers.earnings.official_ir.registry import official_ir_registry

PRIMARY_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "halkb_2026.html")
SECONDARY_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "halkb_financial_presentations_2026.html")


def test_halkb_primary_parser_fy2025_and_q1_2026():
    with open(PRIMARY_FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = HalkbIrProvider()
    res = provider.parse_primary_html(html)

    assert res.symbol == "HALKB"
    assert res.status == "success"
    assert not res.parser_failed
    assert res.accepted_row_count == 2

    # FY 2025
    fy = next(e for e in res.events if e.fiscal_period == "FY")
    assert fy.fiscal_year == 2025
    assert fy.earnings_date == "2026-02-20"
    assert fy.period_start == "2025-01-01"
    assert fy.period_end == "2025-12-31"
    assert fy.source_url == PRIMARY_URL

    # Q1 2026
    q1 = next(e for e in res.events if e.fiscal_period == "Q1")
    assert q1.fiscal_year == 2026
    assert q1.earnings_date == "2026-05-08"
    assert q1.period_start == "2026-01-01"
    assert q1.period_end == "2026-03-31"
    assert q1.source_url == PRIMARY_URL


def test_halkb_secondary_parser_q1_and_q2():
    with open(SECONDARY_FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = HalkbIrProvider()
    res = provider.parse_secondary_html(html)

    assert res.symbol == "HALKB"
    assert res.status == "success"
    assert not res.parser_failed

    events_2026 = [e for e in res.events if e.fiscal_year == 2026]
    assert len(events_2026) == 2

    # Q1 2026
    q1 = next(e for e in events_2026 if e.fiscal_period == "Q1")
    assert q1.earnings_date == "2026-05-08"
    assert q1.source_url == SECONDARY_URL

    # Q2 2026
    q2 = next(e for e in events_2026 if e.fiscal_period == "Q2")
    assert q2.earnings_date == "2026-08-07"
    assert q2.period_start == "2026-01-01"
    assert q2.period_end == "2026-06-30"
    assert q2.date_type == "company_announced"
    assert q2.source == "Official IR"
    assert q2.source_provider == "HALKB Investor Relations"
    assert q2.source_type == "official_financial_calendar"
    assert q2.source_event_id == "halkb:2026:Q2"
    assert q2.source_url == SECONDARY_URL


def test_halkb_merge_primary_wins_and_fallback_fills_missing_q2():
    with open(PRIMARY_FIXTURE_PATH, encoding="utf-8") as f:
        primary_html = f.read()
    with open(SECONDARY_FIXTURE_PATH, encoding="utf-8") as f:
        secondary_html = f.read()

    provider = HalkbIrProvider()
    primary_res = provider.parse_primary_html(primary_html)
    secondary_res = provider.parse_secondary_html(secondary_html)

    merged = provider.merge_primary_and_fallback(primary_res, secondary_res)

    assert merged.status == "success"
    assert len(merged.events) == 6  # 2026: FY2025(P), Q1(2026)(P), Q2(2026)(S); 2025: Q1, Q2, Q3

    # Q1 2026 comes from primary (not overwritten by secondary)
    q1_2026 = next(e for e in merged.events if e.fiscal_year == 2026 and e.fiscal_period == "Q1")
    assert q1_2026.earnings_date == "2026-05-08"
    assert q1_2026.source_url == PRIMARY_URL

    # Q2 2026 comes from secondary fallback
    q2_2026 = next(e for e in merged.events if e.fiscal_year == 2026 and e.fiscal_period == "Q2")
    assert q2_2026.earnings_date == "2026-08-07"
    assert q2_2026.period_start == "2026-01-01"
    assert q2_2026.period_end == "2026-06-30"
    assert q2_2026.date_type == "company_announced"
    assert q2_2026.source == "Official IR"
    assert q2_2026.source_provider == "HALKB Investor Relations"
    assert q2_2026.source_type == "official_financial_calendar"
    assert q2_2026.source_event_id == "halkb:2026:Q2"
    assert q2_2026.source_url == SECONDARY_URL

    # FY 2025 comes from primary
    fy_2025 = next(e for e in merged.events if e.fiscal_year == 2025 and e.fiscal_period == "FY")
    assert fy_2025.earnings_date == "2026-02-20"
    assert fy_2025.source_url == PRIMARY_URL


def test_halkb_merge_conflict_different_date_prefers_primary_and_marks_partial():
    provider = HalkbIrProvider()

    p_ev = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2026, fiscal_period="Q1",
        earnings_date="2026-05-08", period_start="2026-01-01", period_end="2026-03-31",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2026:Q1", source_url=PRIMARY_URL
    )
    s_ev = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2026, fiscal_period="Q1",
        earnings_date="2026-05-15", period_start="2026-01-01", period_end="2026-03-31",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2026:Q1", source_url=SECONDARY_URL
    )

    primary_res = OfficialIrProviderResult(symbol="HALKB", source_provider="HALKB IR", status="success", events=[p_ev])
    secondary_res = OfficialIrProviderResult(symbol="HALKB", source_provider="HALKB IR", status="success", events=[s_ev])

    merged = provider.merge_primary_and_fallback(primary_res, secondary_res)

    assert merged.status == "partial"
    assert len(merged.events) == 1
    assert merged.events[0].earnings_date == "2026-05-08"  # Primary retained
    assert merged.events[0].source_url == PRIMARY_URL
    assert any("Conflict for HALKB Q1 2026" in w for w in merged.warnings)


def test_halkb_secondary_malformed_date_rejected_and_empty_cells_ignored():
    html = """
    <div class="cmp-accordion__panel">
        <table class="cmp-table">
            <thead>
                <tr><th>Yıl</th><th>1. ÇEYREK</th><th>2. ÇEYREK</th><th>3. ÇEYREK</th><th>4. ÇEYREK</th></tr>
            </thead>
            <tbody>
                <tr>
                    <td>2026</td>
                    <td>08.05.2026</td>
                    <td>gecersiz-tarih</td>
                    <td>-</td>
                    <td>Yok</td>
                </tr>
            </tbody>
        </table>
    </div>
    """
    provider = HalkbIrProvider()
    res = provider.parse_secondary_html(html)

    assert res.status == "partial"
    assert len(res.events) == 1
    assert res.events[0].fiscal_period == "Q1"
    assert res.accepted_row_count == 1
    assert res.rejected_row_count >= 1
    assert any("Could not parse valid date" in w for w in res.warnings)


def test_halkb_secondary_date_before_period_end_rejected():
    html = """
    <div class="cmp-accordion__panel">
        <table class="cmp-table">
            <thead>
                <tr><th>Yıl</th><th>1. ÇEYREK</th></tr>
            </thead>
            <tbody>
                <tr>
                    <td>2026</td>
                    <td>15 Mart 2026</td>
                </tr>
            </tbody>
        </table>
    </div>
    """
    provider = HalkbIrProvider()
    res = provider.parse_secondary_html(html)
    assert res.status == "failed"
    assert res.accepted_row_count == 0
    assert any("before period_end" in w for w in res.warnings)


def test_halkb_merge_primary_success_and_secondary_failure():
    with open(PRIMARY_FIXTURE_PATH, encoding="utf-8") as f:
        primary_html = f.read()

    provider = HalkbIrProvider()
    primary_res = provider.parse_primary_html(primary_html)
    secondary_res = OfficialIrProviderResult(
        symbol="HALKB", source_provider="HALKB IR", status="failed", parser_failed=True,
        events=[], warnings=["Secondary page HTTP 500"]
    )

    merged = provider.merge_primary_and_fallback(primary_res, secondary_res)

    assert merged.status == "partial"
    assert len(merged.events) == 2  # Primary events preserved
    assert any("Secondary page HTTP 500" in w for w in merged.warnings)


def test_halkb_merge_primary_failure_and_secondary_success():
    with open(SECONDARY_FIXTURE_PATH, encoding="utf-8") as f:
        secondary_html = f.read()

    provider = HalkbIrProvider()
    primary_res = OfficialIrProviderResult(
        symbol="HALKB", source_provider="HALKB IR", status="failed", parser_failed=True,
        events=[], warnings=["Primary page HTTP 404"]
    )
    secondary_res = provider.parse_secondary_html(secondary_html)

    merged = provider.merge_primary_and_fallback(primary_res, secondary_res)

    assert merged.status == "partial"
    assert len(merged.events) > 0  # Secondary events preserved
    assert any("Primary page HTTP 404" in w for w in merged.warnings)


def test_halkb_merge_both_failure():
    provider = HalkbIrProvider()
    primary_res = OfficialIrProviderResult(
        symbol="HALKB", source_provider="HALKB IR", status="failed", parser_failed=True,
        events=[], warnings=["Primary failed"]
    )
    secondary_res = OfficialIrProviderResult(
        symbol="HALKB", source_provider="HALKB IR", status="failed", parser_failed=True,
        events=[], warnings=["Secondary failed"]
    )

    merged = provider.merge_primary_and_fallback(primary_res, secondary_res)

    assert merged.status == "failed"
    assert len(merged.events) == 0
    assert merged.parser_failed is True


@pytest.mark.asyncio
async def test_halkb_fetch_calendar_retry_and_backoff():
    with open(PRIMARY_FIXTURE_PATH, encoding="utf-8") as f:
        primary_content = f.read()
    with open(SECONDARY_FIXTURE_PATH, encoding="utf-8") as f:
        secondary_content = f.read()

    primary_calls = 0
    secondary_calls = 0

    def dual_handler(request: httpx.Request):
        nonlocal primary_calls, secondary_calls
        url_str = str(request.url)
        if url_str == PRIMARY_URL:
            primary_calls += 1
            if primary_calls == 1:
                return httpx.Response(429, text="Too Many Requests")
            return httpx.Response(200, text=primary_content)
        elif url_str == SECONDARY_URL:
            secondary_calls += 1
            if secondary_calls == 1:
                return httpx.Response(500, text="Server Error")
            return httpx.Response(200, text=secondary_content)
        return httpx.Response(404, text="Not Found")

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        transport = httpx.MockTransport(dual_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = HalkbIrProvider()
            res = await provider.fetch_calendar(client=client)

            assert res.status == "success"
            assert res.retry_count == 2
            assert primary_calls == 2
            assert secondary_calls == 2
            assert mock_sleep.call_count == 2


def test_halkb_parse_html_backward_compatibility():
    with open(PRIMARY_FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = HalkbIrProvider()
    res = provider.parse_html(html)
    assert res.status == "success"
    assert len(res.events) == 2


def test_other_official_ir_providers_unaffected_in_registry():
    providers = official_ir_registry.get_all_providers()
    symbols = [p.symbol for p in providers]
    assert sorted(symbols) == ["AKSA", "BIMAS", "HALKB", "MAVI", "SOKM", "THYAO"]
