import os

import httpx
import pytest

from app.providers.earnings.official_ir.garan import GaranIrProvider

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "garan_2026.html")


def test_garan_ir_provider_golden_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = GaranIrProvider()
    res = provider.parse_html(html)

    assert res.symbol == "GARAN"
    assert res.source_provider == "GARAN Investor Relations"
    assert res.status == "success"
    assert not res.parser_failed
    assert res.accepted_row_count == 2
    assert res.rejected_row_count == 1  # Webcast is rejected

    events = res.events
    assert len(events) == 2

    # Q2 (1YY26)
    q2 = next(e for e in events if e.fiscal_period == "Q2")
    assert q2.fiscal_year == 2026
    assert q2.earnings_date == "2026-07-30"
    assert q2.period_start == "2026-01-01"
    assert q2.period_end == "2026-06-30"
    assert q2.source == "Official IR"
    assert q2.source_provider == "GARAN Investor Relations"
    assert q2.source_type == "official_financial_calendar"
    assert q2.date_type == "company_announced"

    # Q3 (9A26)
    q3 = next(e for e in events if e.fiscal_period == "Q3")
    assert q3.fiscal_year == 2026
    assert q3.earnings_date == "2026-10-28"
    assert q3.period_start == "2026-01-01"
    assert q3.period_end == "2026-09-30"
    assert q3.source == "Official IR"
    assert q3.source_provider == "GARAN Investor Relations"


def test_garan_ir_provider_strict_container_required():
    provider = GaranIrProvider()
    # <a> without class="containerCalendar"
    html = """
    <div class="otherContainer">
        <a><h3>28.10.2026</h3><p>Garanti BBVA 9A26 Finansal Sonuçları Açıklaması</p></a>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "failed"
    assert res.parser_failed is True


def test_garan_ir_provider_empty_html():
    provider = GaranIrProvider()
    res = provider.parse_html("")
    assert res.status == "failed"
    assert res.parser_failed


def test_garan_ir_provider_malformed_date_alone_fails():
    provider = GaranIrProvider()
    html = """
    <div class="containerCalendar">
        <a><h3>gecersiz-tarih</h3><p>Garanti BBVA 1YY26 Finansal Sonuçları Açıklaması</p></a>
    </div>
    """
    res = provider.parse_html(html)
    assert len(res.events) == 0
    assert res.rejected_row_count == 1
    assert res.status == "failed"
    assert res.parser_failed is True


def test_garan_ir_provider_mixed_valid_and_malformed_returns_partial():
    provider = GaranIrProvider()
    html = """
    <div class="containerCalendar">
        <a><h3>28.10.2026</h3><p>Garanti BBVA 9A26 Finansal Sonuçları Açıklaması</p></a>
        <a><h3>gecersiz-tarih</h3><p>Garanti BBVA 1YY26 Finansal Sonuçları Açıklaması</p></a>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "partial"
    assert res.parser_failed is False
    assert len(res.events) == 1
    assert res.accepted_row_count == 1
    assert res.rejected_row_count == 1
    assert len(res.warnings) > 0


@pytest.mark.asyncio
async def test_garan_ir_provider_mock_fetch():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        content = f.read()

    def handler(request: httpx.Request):
        return httpx.Response(200, text=content)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GaranIrProvider()
        res = await provider.fetch_calendar(client=client)
        assert res.status == "success"
        assert len(res.events) == 2
