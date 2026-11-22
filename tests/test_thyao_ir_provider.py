import os

import httpx
import pytest

from app.providers.earnings.official_ir.thyao import ThyaoIrProvider

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "thyao_2026.html")


def test_thyao_ir_provider_golden_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = ThyaoIrProvider()
    res = provider.parse_html(html)

    assert res.symbol == "THYAO"
    assert res.source_provider == "THYAO Investor Relations"
    assert res.status == "success"
    assert not res.parser_failed
    assert res.accepted_row_count == 2
    assert res.rejected_row_count >= 1  # Earnings call rejected

    events = res.events
    assert len(events) == 2

    # 1Q'26
    q1 = next(e for e in events if e.fiscal_period == "Q1")
    assert q1.fiscal_year == 2026
    assert q1.earnings_date == "2026-04-29"
    assert q1.period_start == "2026-01-01"
    assert q1.period_end == "2026-03-31"
    assert q1.source == "Official IR"
    assert q1.source_provider == "THYAO Investor Relations"
    assert q1.source_type == "official_financial_calendar"
    assert q1.date_type == "company_announced"
    assert q1.source_event_id == "thyao:2026:Q1"
    assert "1q26-results" in (q1.source_url or "")

    # 2Q'26
    q2 = next(e for e in events if e.fiscal_period == "Q2")
    assert q2.fiscal_year == 2026
    assert q2.earnings_date == "2026-08-05"
    assert q2.period_start == "2026-01-01"
    assert q2.period_end == "2026-06-30"


def test_thyao_ir_provider_strict_report_link_required():
    provider = ThyaoIrProvider()
    # Links without class="report-link"
    html = """
    <a href="/test" class="other-link">
        <span class="date">29.04.2026</span>
        <p class="report-aciklama">1Q'26 Financial Results</p>
    </a>
    """
    res = provider.parse_html(html)
    assert res.status == "failed"
    assert res.parser_failed is True


def test_thyao_ir_provider_empty_html():
    provider = ThyaoIrProvider()
    res = provider.parse_html("")
    assert res.status == "failed"
    assert res.parser_failed


def test_thyao_ir_provider_malformed_date_alone_fails():
    provider = ThyaoIrProvider()
    html = """
    <a href="/test" class="report-link">
        <span class="date">gecersiz-tarih</span>
        <p class="report-aciklama">1Q'26 Financial Results</p>
    </a>
    """
    res = provider.parse_html(html)
    assert len(res.events) == 0
    assert res.rejected_row_count == 1
    assert res.status == "failed"
    assert res.parser_failed is True


def test_thyao_ir_provider_mixed_valid_and_malformed_returns_partial():
    provider = ThyaoIrProvider()
    html = """
    <a href="/test1" class="report-link">
        <span class="date">29.04.2026</span>
        <p class="report-aciklama">1Q'26 Financial Results</p>
    </a>
    <a href="/test2" class="report-link">
        <span class="date">gecersiz-tarih</span>
        <p class="report-aciklama">2Q'26 Financial Results</p>
    </a>
    """
    res = provider.parse_html(html)
    assert res.status == "partial"
    assert res.parser_failed is False
    assert len(res.events) == 1
    assert res.accepted_row_count == 1
    assert res.rejected_row_count == 1
    assert len(res.warnings) > 0


@pytest.mark.asyncio
async def test_thyao_ir_provider_mock_fetch():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        content = f.read()

    def handler(request: httpx.Request):
        return httpx.Response(200, text=content)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ThyaoIrProvider()
        res = await provider.fetch_calendar(client=client)
        assert res.status == "success"
        assert len(res.events) == 2
