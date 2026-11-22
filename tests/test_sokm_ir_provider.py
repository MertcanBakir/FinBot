import os

import httpx
import pytest

from app.providers.earnings.official_ir.sokm import SokmIrProvider

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "sokm_2026.html")


def test_sokm_ir_provider_golden_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = SokmIrProvider()
    res = provider.parse_html(html)

    assert res.symbol == "SOKM"
    assert res.source_provider == "SOKM Investor Relations"
    assert res.status == "success"
    assert not res.parser_failed
    assert res.accepted_row_count == 2
    # Q3 and FY contain "Son Tarih" and MUST be rejected
    assert res.rejected_row_count == 2

    events = res.events
    assert len(events) == 2

    # Q1
    q1 = next(e for e in events if e.fiscal_period == "Q1")
    assert q1.fiscal_year == 2026
    assert q1.earnings_date == "2026-05-07"
    assert q1.period_start == "2026-01-01"
    assert q1.period_end == "2026-03-31"
    assert q1.source == "Official IR"
    assert q1.source_provider == "SOKM Investor Relations"
    assert q1.source_type == "official_financial_calendar"
    assert q1.date_type == "company_announced"
    assert q1.source_event_id == "sokm:2026:Q1"

    # Q2
    q2 = next(e for e in events if e.fiscal_period == "Q2")
    assert q2.fiscal_year == 2026
    assert q2.earnings_date == "2026-08-17"
    assert q2.period_start == "2026-01-01"
    assert q2.period_end == "2026-06-30"


def test_sokm_ir_provider_strict_itemstable_required():
    provider = SokmIrProvider()
    # item-line elements outside of #itemstable
    html = """
    <div id="other_table">
        <div class="item-line">
            <div class="item-title">2026 1. Çeyrek Finansal Sonuçları</div>
            <div class="item-date">07.05.2026</div>
        </div>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "failed"
    assert res.parser_failed is True


def test_sokm_ir_provider_deadline_rows_are_explicitly_rejected():
    provider = SokmIrProvider()
    html = """
    <div id="itemstable">
        <div class="item-line">
            <div class="item-title">2026 3. Çeyrek Finansal Sonuçları Son Tarih</div>
            <div class="item-date">09.11.2026</div>
        </div>
        <div class="item-line">
            <div class="item-title">2026 Q3 Financial Results Deadline</div>
            <div class="item-date">09.11.2026</div>
        </div>
    </div>
    """
    res = provider.parse_html(html)
    assert len(res.events) == 0
    assert res.rejected_row_count == 2
    assert res.status == "failed"
    assert res.parser_failed is True


def test_sokm_ir_provider_mixed_valid_and_malformed_returns_partial():
    provider = SokmIrProvider()
    html = """
    <div id="itemstable">
        <div class="item-line">
            <div class="item-title">2026 1. Çeyrek Finansal Sonuçları</div>
            <div class="item-date">07.05.2026</div>
        </div>
        <div class="item-line">
            <div class="item-title">2026 2. Çeyrek Finansal Sonuçları</div>
            <div class="item-date">gecersiz-tarih</div>
        </div>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "partial"
    assert res.parser_failed is False
    assert len(res.events) == 1
    assert res.accepted_row_count == 1
    assert res.rejected_row_count == 1
    assert len(res.warnings) > 0


def test_sokm_ir_provider_empty_html():
    provider = SokmIrProvider()
    res = provider.parse_html("")
    assert res.status == "failed"
    assert res.parser_failed


@pytest.mark.asyncio
async def test_sokm_ir_provider_mock_fetch():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        content = f.read()

    def handler(request: httpx.Request):
        return httpx.Response(200, text=content)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = SokmIrProvider()
        res = await provider.fetch_calendar(client=client)
        assert res.status == "success"
        assert len(res.events) == 2
