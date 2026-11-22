import os

import httpx
import pytest

from app.providers.earnings.official_ir.bimas import BimasIrProvider

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "bimas_2026.html")


def test_bimas_ir_provider_golden_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = BimasIrProvider()
    res = provider.parse_html(html)

    assert res.symbol == "BIMAS"
    assert res.source_provider == "BIMAS Investor Relations"
    assert res.status == "success"
    assert not res.parser_failed
    assert res.accepted_row_count == 2
    assert res.rejected_row_count == 2

    events = res.events
    assert len(events) == 2

    # Q2
    q2 = next(e for e in events if e.fiscal_period == "Q2")
    assert q2.fiscal_year == 2026
    assert q2.earnings_date == "2026-08-17"
    assert q2.period_start == "2026-01-01"
    assert q2.period_end == "2026-06-30"
    assert q2.source == "Official IR"
    assert q2.source_provider == "BIMAS Investor Relations"
    assert q2.source_type == "official_financial_calendar"
    assert q2.date_type == "company_announced"
    assert q2.source_event_id == "bimas:2026:Q2"

    # Q3
    q3 = next(e for e in events if e.fiscal_period == "Q3")
    assert q3.fiscal_year == 2026
    assert q3.earnings_date == "2026-11-09"
    assert q3.period_start == "2026-01-01"
    assert q3.period_end == "2026-09-30"


def test_bimas_ir_provider_strict_table_required():
    provider = BimasIrProvider()
    html = """
    <table id="other_table">
        <tr><th>2026 2. Çeyrek Finansal Sonuçlarının Açıklanması</th><td>17.08.2026</td></tr>
    </table>
    """
    res = provider.parse_html(html)
    assert res.status == "failed"
    assert res.parser_failed is True


def test_bimas_ir_provider_empty_html():
    provider = BimasIrProvider()
    res = provider.parse_html("")
    assert res.status == "failed"
    assert res.parser_failed


def test_bimas_ir_provider_malformed_date_alone_fails():
    provider = BimasIrProvider()
    html = """
    <table id="table1">
        <tr><th>2026 2. Çeyrek Finansal Sonuçlarının Açıklanması</th><td>invalid-date</td></tr>
    </table>
    """
    res = provider.parse_html(html)
    assert len(res.events) == 0
    assert res.rejected_row_count == 1
    assert res.status == "failed"
    assert res.parser_failed is True


def test_bimas_ir_provider_mixed_valid_and_malformed_returns_partial():
    provider = BimasIrProvider()
    html = """
    <table id="table1">
        <tr><th>2026 2. Çeyrek Finansal Sonuçlarının Açıklanması</th><td>17.08.2026</td></tr>
        <tr><th>2026 3. Çeyrek Finansal Sonuçlarının Açıklanması</th><td>invalid-date</td></tr>
    </table>
    """
    res = provider.parse_html(html)
    assert res.status == "partial"
    assert res.parser_failed is False
    assert len(res.events) == 1
    assert res.accepted_row_count == 1
    assert res.rejected_row_count == 1
    assert len(res.warnings) > 0


@pytest.mark.asyncio
async def test_bimas_ir_provider_mock_fetch():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        content = f.read()

    def handler(request: httpx.Request):
        return httpx.Response(200, text=content)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BimasIrProvider()
        res = await provider.fetch_calendar(client=client)
        assert res.status == "success"
        assert len(res.events) == 2
