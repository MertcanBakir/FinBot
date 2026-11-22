import os

import httpx
import pytest

from app.providers.earnings.official_ir.aksa import AksaIrProvider

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "aksa_2026.html")


def test_aksa_ir_provider_golden_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = AksaIrProvider()
    res = provider.parse_html(html)

    assert res.symbol == "AKSA"
    assert res.source_provider == "AKSA Investor Relations"
    assert res.status == "success"
    assert not res.parser_failed
    assert res.accepted_row_count == 4
    assert res.rejected_row_count == 0

    events = res.events
    assert len(events) == 4

    # All 4 events must be company_announced
    for ev in events:
        assert ev.date_type == "company_announced"
        assert ev.source == "Official IR"
        assert ev.source_provider == "AKSA Investor Relations"
        assert ev.source_type == "official_financial_calendar"
        assert ev.publish_date is None

    # Q1
    q1 = next(e for e in events if e.fiscal_period == "Q1")
    assert q1.fiscal_year == 2026
    assert q1.earnings_date == "2026-05-07"
    assert q1.period_start == "2026-01-01"
    assert q1.period_end == "2026-03-31"
    assert q1.source_event_id == "aksa:2026:Q1"

    # Q2
    q2 = next(e for e in events if e.fiscal_period == "Q2")
    assert q2.fiscal_year == 2026
    assert q2.earnings_date == "2026-08-13"
    assert q2.period_start == "2026-01-01"
    assert q2.period_end == "2026-06-30"
    assert q2.source_event_id == "aksa:2026:Q2"

    # Q3
    q3 = next(e for e in events if e.fiscal_period == "Q3")
    assert q3.fiscal_year == 2026
    assert q3.earnings_date == "2026-11-04"
    assert q3.period_start == "2026-01-01"
    assert q3.period_end == "2026-09-30"
    assert q3.source_event_id == "aksa:2026:Q3"

    # FY
    fy = next(e for e in events if e.fiscal_period == "FY")
    assert fy.fiscal_year == 2026
    assert fy.earnings_date == "2027-02-17"
    assert fy.period_start == "2026-01-01"
    assert fy.period_end == "2026-12-31"
    assert fy.source_event_id == "aksa:2026:FY"


def test_aksa_ir_provider_strict_table_required():
    provider = AksaIrProvider()
    html = """
    <table>
        <caption>Alakasız Tablo</caption>
        <tr><th>2026 Yılı</th></tr>
    </table>
    """
    res = provider.parse_html(html)
    assert res.status == "failed"
    assert res.parser_failed is True


def test_aksa_ir_provider_deterministic_year_missing():
    provider = AksaIrProvider()
    html = """
    <table>
        <caption>Finansal Tabloların Yayınlanma Takvimi</caption>
        <tr>
            <th>1. Çeyrek</th>
        </tr>
        <tr>
            <td>07.05.2026</td>
        </tr>
    </table>
    """
    res = provider.parse_html(html)
    # Header lacks explicit year -> parser fails deterministically
    assert res.status == "failed"
    assert res.parser_failed is True


def test_aksa_ir_provider_mixed_valid_and_malformed_returns_partial():
    provider = AksaIrProvider()
    html = """
    <table>
        <caption>Finansal Tabloların Yayınlanma Takvimi</caption>
        <tr>
            <th>2026 Yılı Finansal Tabloları</th>
            <th>1Ç</th>
            <th>2Ç</th>
            <th>3Ç</th>
            <th>4Ç</th>
        </tr>
        <tr>
            <td>Yayınlanma Tarihi</td>
            <td>07.05.2026</td>
            <td>invalid-date</td>
            <td>04.11.2026</td>
            <td>17.02.2027</td>
        </tr>
    </table>
    """
    res = provider.parse_html(html)
    assert res.status == "partial"
    assert res.parser_failed is False
    assert len(res.events) == 3
    assert res.accepted_row_count == 3
    assert res.rejected_row_count == 1
    assert len(res.warnings) > 0


@pytest.mark.asyncio
async def test_aksa_ir_provider_mock_fetch():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        content = f.read()

    def handler(request: httpx.Request):
        return httpx.Response(200, text=content)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = AksaIrProvider()
        res = await provider.fetch_calendar(client=client)
        assert res.status == "success"
        assert len(res.events) == 4
