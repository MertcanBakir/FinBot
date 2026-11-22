import os

import httpx
import pytest

from app.providers.earnings.official_ir.mavi import MaviIrProvider

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "mavi_2026.html")


def test_mavi_ir_provider_golden_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        html = f.read()

    provider = MaviIrProvider()
    res = provider.parse_html(html)

    assert res.symbol == "MAVI"
    assert res.source_provider == "MAVI Investor Relations"
    assert res.status == "success"
    assert not res.parser_failed
    assert res.accepted_row_count == 3
    assert res.rejected_row_count == 3  # 3 webcasts rejected

    events = res.events
    assert len(events) == 3

    # H1 2026 (Q2)
    h1 = next(e for e in events if e.fiscal_period == "Q2")
    assert h1.fiscal_year == 2026
    assert h1.earnings_date == "2026-09-16"
    assert h1.period_start == "2026-02-01"
    assert h1.period_end == "2026-07-31"
    assert h1.source == "Official IR"
    assert h1.source_provider == "MAVI Investor Relations"
    assert h1.source_type == "official_financial_calendar"
    assert h1.date_type == "company_announced"
    assert h1.source_event_id == "eid-520"

    # 9M 2026 (Q3)
    q3 = next(e for e in events if e.fiscal_period == "Q3")
    assert q3.fiscal_year == 2026
    assert q3.earnings_date == "2026-12-09"
    assert q3.period_start == "2026-02-01"
    assert q3.period_end == "2026-10-31"

    # FY 2025 (FY)
    fy = next(e for e in events if e.fiscal_period == "FY")
    assert fy.fiscal_year == 2025
    assert fy.earnings_date == "2026-03-17"
    assert fy.period_start == "2025-02-01"
    assert fy.period_end == "2026-01-31"


def test_mavi_ir_provider_strict_event_list_required():
    provider = MaviIrProvider()
    html = """
    <div id="other-events">
        <table>
            <tr id="eid-520">
                <td>16 September 2026</td>
                <td class="event-title">H1 2026 Financial Results</td>
            </tr>
        </table>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "failed"
    assert res.parser_failed is True


def test_mavi_ir_provider_empty_html():
    provider = MaviIrProvider()
    res = provider.parse_html("")
    assert res.status == "failed"
    assert res.parser_failed


def test_mavi_ir_provider_date_before_period_end_rejected_alone_fails():
    provider = MaviIrProvider()
    html = """
    <div id="event-list">
        <table>
            <tr id="eid-test">
                <td>15 May 2026</td>
                <td class="event-title">H1 2026 Financial Results</td>
            </tr>
        </table>
    </div>
    """
    res = provider.parse_html(html)
    assert len(res.events) == 0
    assert res.rejected_row_count == 1
    assert res.status == "failed"
    assert res.parser_failed is True


def test_mavi_ir_provider_mixed_valid_and_malformed_returns_partial():
    provider = MaviIrProvider()
    html = """
    <div id="event-list">
        <table>
            <tr id="eid-520">
                <td>16 September 2026</td>
                <td class="event-title">H1 2026 Financial Results</td>
            </tr>
            <tr id="eid-test">
                <td>15 May 2026</td>
                <td class="event-title">H1 2026 Financial Results</td>
            </tr>
        </table>
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
async def test_mavi_ir_provider_mock_fetch():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        content = f.read()

    requested_urls = []

    def handler(request: httpx.Request):
        requested_urls.append(str(request.url))
        return httpx.Response(200, text=content)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = MaviIrProvider()
        assert provider.url == "https://www.mavicompany.com/en/investor-relations/financial-calendar"
        res = await provider.fetch_calendar(client=client)
        assert res.status == "success"
        assert len(res.events) == 3
        assert requested_urls == ["https://www.mavicompany.com/en/investor-relations/financial-calendar"]
        assert all(e.source_url == "https://www.mavicompany.com/en/investor-relations/financial-calendar" for e in res.events)


def test_mavi_ir_provider_fy2024_visible_year_typo_metadata_fallback():
    provider = MaviIrProvider()
    html = """
    <div id="event-list">
        <table>
            <tr id="eid-498" data-event-start-date="2025-03-11 09:24:00" data-event-end-date="2025-03-18 23:59:00">
                <td>18 March 2024</td>
                <td class="event-title">FY 2024 Financial Results</td>
                <td class="add-calendar"></td>
            </tr>
        </table>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "success"
    assert res.parser_failed is False
    assert len(res.events) == 1
    assert res.accepted_row_count == 1
    assert res.rejected_row_count == 0

    ev = res.events[0]
    assert ev.fiscal_year == 2024
    assert ev.fiscal_period == "FY"
    assert ev.period_start == "2024-02-01"
    assert ev.period_end == "2025-01-31"
    assert ev.earnings_date == "2025-03-18"
    assert any("Corrected MAVI FY 2024 visible date" in w for w in res.warnings)


def test_mavi_ir_provider_q1_2024_visible_date_preserved():
    provider = MaviIrProvider()
    html = """
    <div id="event-list">
        <table>
            <tr id="eid-480" data-event-start-date="2024-06-07 09:24:00" data-event-end-date="2024-06-14 23:59:00">
                <td>13 June 2024</td>
                <td class="event-title">Q1 2024 Financial Results</td>
            </tr>
        </table>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "success"
    assert len(res.events) == 1
    assert res.events[0].earnings_date == "2024-06-13"


def test_mavi_ir_provider_q1_2023_corrupt_metadata_ignored():
    provider = MaviIrProvider()
    html = """
    <div id="event-list">
        <table>
            <tr id="eid-387" data-event-start-date="2023-01-01 09:24:00" data-event-end-date="2023-01-08 23:59:00">
                <td>8 June 2023</td>
                <td class="event-title">Q1 2023 Financial Results</td>
            </tr>
        </table>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "success"
    assert len(res.events) == 1
    assert res.events[0].earnings_date == "2023-06-08"


def test_mavi_ir_provider_unsafe_fy_fallback_rejected():
    provider = MaviIrProvider()
    # Metadata has different month/day (April 20 vs March 18) -> unsafe fallback must be rejected
    html = """
    <div id="event-list">
        <table>
            <tr id="eid-bad" data-event-end-date="2025-04-20 23:59:00">
                <td>18 March 2024</td>
                <td class="event-title">FY 2024 Financial Results</td>
            </tr>
        </table>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "failed"
    assert res.parser_failed is True
    assert len(res.events) == 0
    assert res.rejected_row_count == 1
    assert any("before period_end" in w for w in res.warnings)
    assert any(iss.kind == "row_validation" for iss in res.issues)


def test_mavi_ir_provider_populates_structured_issues():
    provider = MaviIrProvider()
    html = """
    <div id="event-list">
        <table>
            <tr id="eid-498" data-event-start-date="2025-03-07 09:24:00" data-event-end-date="2025-03-18 23:59:00">
                <td>18 March 2024</td>
                <td class="event-title">FY 2024 Financial Results</td>
            </tr>
            <tr id="eid-520">
                <td>16 September 2026</td>
                <td class="event-title">H1 2026 Financial Results</td>
            </tr>
        </table>
    </div>
    """
    res = provider.parse_html(html)
    assert res.status == "success"
    assert len(res.events) == 2
    assert len(res.issues) == 1
    iss = res.issues[0]
    assert iss.kind == "informational"
    assert iss.fiscal_year == 2024
    assert iss.fiscal_period == "FY"
    assert iss.earnings_date == "2025-03-18"
    assert iss.is_global is False
