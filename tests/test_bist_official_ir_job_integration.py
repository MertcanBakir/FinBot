import os
import smtplib
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.jobs.earnings_refresh_job import EarningsRefreshJob
from app.jobs.official_ir_smoke_job import run_official_ir_smoke_test
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    KapEarningsFetchResult,
    Nasdaq100EarningsCalendar,
    OfficialIrProviderResult,
)
from app.providers.earnings.official_ir.base import BaseOfficialIrProvider
from app.providers.earnings.official_ir.bimas import BimasIrProvider
from app.providers.earnings.official_ir.registry import OfficialIrRegistry
from app.services.bist100_earnings import (
    Bist100EarningsService,
)
from app.services.calendar_state_store import merge_bist_partial_calendar
from app.services.earnings_refresh_report import format_earnings_refresh_email
from tests.conftest import create_mock_db_connection
from tests.state_store_fakes import MemoryCalendarStateStore


@pytest.fixture(autouse=True)
def mock_job_db(monkeypatch):
    mock_conn = create_mock_db_connection()
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_conn
    monkeypatch.setattr("app.jobs.earnings_refresh_job.get_db_connection", lambda: mock_ctx)


class DummyProvider(BaseOfficialIrProvider):
    def __init__(self, symbol: str, res: OfficialIrProviderResult):
        self.symbol = symbol
        self.company_name = f"{symbol} COMPANY"
        self.source_provider = f"{symbol} Investor Relations"
        self.url = f"https://{symbol.lower()}.com"
        self._res = res

    def parse_html(self, html: str) -> OfficialIrProviderResult:
        return self._res

    async def fetch_calendar(self, client=None) -> OfficialIrProviderResult:
        return self._res


class MockKapProvider:
    def __init__(self, fetch_res: KapEarningsFetchResult):
        self._res = fetch_res

    def _load_bist100_symbols(self):
        return ["BIMAS", "GARAN", "HALKB", "MAVI", "THYAO", "SOKM", "AKSA", "PGSUS"]

    async def get_earnings_result(self, start_dt, end_dt):
        return self._res

    async def fetch_calendar(self, coverage_start, coverage_end, published_from, published_to):
        return self._res


class MockNasdaqService:
    async def get_rolling_calendar(self, as_of=None):
        return Nasdaq100EarningsCalendar(
            schema_version="2.0",
            generated_at="2026-08-14T00:00:00Z",
            coverage_start="2026-08-14",
            coverage_end="2026-11-13",
            market="NASDAQ100",
            status="success",
            provider_status="success",
            events=[]
        )


@pytest.mark.asyncio
async def test_job_integration_official_ir_comprehensive_counters_calculation():
    with tempfile.TemporaryDirectory():
        rolling_cache = MemoryCalendarStateStore()

        # 1. Prepare KAP Events (4 normalized events)
        # KAP1: Rolling coverage
        kap1 = BistEarningsEvent(
            symbol="PGSUS", company_name="PEGASUS", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30",
            publish_date="2026-01-10", date_type="company_announced", source="KAP",
            source_type="financial_calendar", disclosure_index=1, source_url="https://kap/1"
        )
        # KAP2: Out of rolling coverage
        kap2 = BistEarningsEvent(
            symbol="PGSUS", company_name="PEGASUS", fiscal_year=2026, fiscal_period="Q1",
            earnings_date="2026-05-11", period_start="2026-01-01", period_end="2026-03-31",
            publish_date="2026-01-10", date_type="company_announced", source="KAP",
            source_type="financial_calendar", disclosure_index=2, source_url="https://kap/2"
        )
        # KAP3: Rolling coverage, will overlap with GARAN IR (same date)
        kap3 = BistEarningsEvent(
            symbol="GARAN", company_name="GARANTI", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-18", period_start="2026-01-01", period_end="2026-06-30",
            publish_date="2026-01-10", date_type="company_announced", source="KAP",
            source_type="financial_calendar", disclosure_index=3, source_url="https://kap/3"
        )
        # KAP4: Rolling coverage, will overlap with HALKB IR (different date conflict)
        kap4 = BistEarningsEvent(
            symbol="HALKB", company_name="HALKBANK", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-25", period_start="2026-01-01", period_end="2026-06-30",
            publish_date="2026-01-10", date_type="company_announced", source="KAP",
            source_type="financial_calendar", disclosure_index=4, source_url="https://kap/4"
        )
        kap_res = KapEarningsFetchResult(
            status="success",
            events=[kap1, kap2, kap3, kap4],
            normalized_events=[kap1, kap2, kap3, kap4],
            warnings=[]
        )
        mock_kap = MockKapProvider(kap_res)

        # 2. Prepare Official IR Events (8 raw provider events across providers)
        # IR1: BIMAS Q2 in rolling coverage
        ir1 = BistEarningsEvent(
            symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-17", period_start="2026-01-01", period_end="2026-06-30",
            date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
            source_type="official_financial_calendar", source_event_id="bimas:2026:Q2", source_url="https://bim"
        )
        # IR2: BIMAS Q1 out of rolling coverage
        ir2 = BistEarningsEvent(
            symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q1",
            earnings_date="2026-05-07", period_start="2026-01-01", period_end="2026-03-31",
            date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
            source_type="official_financial_calendar", source_event_id="bimas:2026:Q1", source_url="https://bim"
        )
        bimas_res = OfficialIrProviderResult(
            symbol="BIMAS", source_provider="BIMAS IR", status="success", events=[ir1, ir2],
            rows_seen=2, accepted_row_count=2
        )

        # IR3: THYAO Q2 duplicate raw records with same date (2 raw events, 1 deduped) in rolling coverage
        ir3_a = BistEarningsEvent(
            symbol="THYAO", company_name="THY", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-22", period_start="2026-01-01", period_end="2026-06-30",
            date_type="company_announced", source="Official IR", source_provider="THYAO IR",
            source_type="official_financial_calendar", source_event_id="thyao:2026:Q2:1", source_url="https://thy"
        )
        ir3_b = BistEarningsEvent(
            symbol="THYAO", company_name="THY", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-22", period_start="2026-01-01", period_end="2026-06-30",
            date_type="company_announced", source="Official IR", source_provider="THYAO IR",
            source_type="official_financial_calendar", source_event_id="thyao:2026:Q2:2", source_url="https://thy"
        )
        thyao_res = OfficialIrProviderResult(
            symbol="THYAO", source_provider="THYAO IR", status="success", events=[ir3_a, ir3_b],
            rows_seen=2, accepted_row_count=2
        )

        # IR4: SOKM Q2 internal conflict with 2 different dates (2 raw events, excluded by reconciliation)
        ir4_a = BistEarningsEvent(
            symbol="SOKM", company_name="SOK", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-10", period_start="2026-01-01", period_end="2026-06-30",
            date_type="company_announced", source="Official IR", source_provider="SOKM IR",
            source_type="official_financial_calendar", source_event_id="sokm:2026:Q2:1", source_url="https://sok"
        )
        ir4_b = BistEarningsEvent(
            symbol="SOKM", company_name="SOK", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-12", period_start="2026-01-01", period_end="2026-06-30",
            date_type="company_announced", source="Official IR", source_provider="SOKM IR",
            source_type="official_financial_calendar", source_event_id="sokm:2026:Q2:2", source_url="https://sok"
        )
        sokm_res = OfficialIrProviderResult(
            symbol="SOKM", source_provider="SOKM IR", status="success", events=[ir4_a, ir4_b],
            rows_seen=2, accepted_row_count=2
        )

        # IR5: GARAN Q2 overlaps KAP3 with same date (1 raw event, overridden by KAP, 0 conflict)
        ir5 = BistEarningsEvent(
            symbol="GARAN", company_name="GARANTI", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-18", period_start="2026-01-01", period_end="2026-06-30",
            date_type="company_announced", source="Official IR", source_provider="GARAN IR",
            source_type="official_financial_calendar", source_event_id="garan:2026:Q2", source_url="https://garan"
        )
        garan_res = OfficialIrProviderResult(
            symbol="GARAN", source_provider="GARAN IR", status="success", events=[ir5],
            rows_seen=1, accepted_row_count=1
        )

        # IR6: HALKB Q2 overlaps KAP4 with different date (1 raw event, overridden by KAP, 1 conflict)
        ir6 = BistEarningsEvent(
            symbol="HALKB", company_name="HALKBANK", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-28", period_start="2026-01-01", period_end="2026-06-30",
            date_type="company_announced", source="Official IR", source_provider="HALKB IR",
            source_type="official_financial_calendar", source_event_id="halkb:2026:Q2", source_url="https://halk"
        )
        halkb_res = OfficialIrProviderResult(
            symbol="HALKB", source_provider="HALKB IR", status="success", events=[ir6],
            rows_seen=1, accepted_row_count=1
        )

        dummy_providers = [
            DummyProvider("BIMAS", bimas_res),
            DummyProvider("THYAO", thyao_res),
            DummyProvider("SOKM", sokm_res),
            DummyProvider("GARAN", garan_res),
            DummyProvider("HALKB", halkb_res),
        ]
        ir_reg = OfficialIrRegistry(providers=dummy_providers)

        bist_svc = Bist100EarningsService(provider=mock_kap, official_ir_reg=ir_reg)  # type: ignore
        nasdaq_svc = MockNasdaqService()  # type: ignore

        mock_email_svc = MagicMock()
        mock_email_svc.is_configured.return_value = False

        job = EarningsRefreshJob(
            bist_service=bist_svc,
            nasdaq_service=nasdaq_svc,  # type: ignore
            state_store=rolling_cache,
            email_service=mock_email_svc
        )

        as_of = datetime(2026, 8, 14, tzinfo=ZoneInfo("UTC"))
        job_result = await job.run(as_of=as_of)
        mock_email_svc.send_email.assert_not_called()

        ir_summary = job_result.bist.official_ir_summary
        assert ir_summary is not None

        # Verify all scoped and identity metrics exactly:
        assert ir_summary.kap_normalized_event_count == 4
        assert ir_summary.kap_rolling_event_count == 3
        assert ir_summary.ir_provider_raw_event_count == 5
        assert ir_summary.ir_normalized_event_count == 2
        assert ir_summary.ir_rolling_event_count == 2
        assert ir_summary.combined_rolling_event_count == 5
        assert ir_summary.events_added_from_ir_count == 2
        assert ir_summary.events_overridden_by_kap_count == 2
        assert ir_summary.kap_ir_conflicts_count == 1
        assert ir_summary.ir_carried_forward_count == 0


@pytest.mark.asyncio
async def test_job_integration_with_official_ir_carry_forward_and_email_rendering():
    with tempfile.TemporaryDirectory():
        rolling_cache = MemoryCalendarStateStore()

        # Pre-seed rolling state_store with an Official IR event for AKSA
        prev_aksa = BistEarningsEvent(
            symbol="AKSA", company_name="AKSA", fiscal_year=2026, fiscal_period="Q3",
            earnings_date="2026-11-04", period_start="2026-01-01", period_end="2026-09-30",
            date_type="company_announced", source="Official IR", source_provider="AKSA IR",
            source_type="official_financial_calendar", source_event_id="aksa:2026:Q3", source_url="https://aksa"
        )
        prev_calendar = Bist100EarningsCalendar(
            schema_version="2.0",
            generated_at="2026-08-13T12:00:00Z",
            coverage_start="2026-08-14",
            coverage_end="2026-11-13",
            status="success",
            events=[prev_aksa],
            symbol_results=[]
        )
        rolling_cache.stage_bist(prev_calendar)

        # KAP returns empty
        kap_res = KapEarningsFetchResult(status="success", events=[], normalized_events=[], warnings=[])
        mock_kap = MockKapProvider(kap_res)

        # AKSA IR provider fails with 500
        aksa_res = OfficialIrProviderResult(
            symbol="AKSA", source_provider="AKSA IR", status="failed", parser_failed=True,
            events=[], retry_count=2, warnings=["AKSA HTTP 500 connection down"]
        )
        dummy_aksa = DummyProvider("AKSA", aksa_res)
        ir_reg = OfficialIrRegistry(providers=[dummy_aksa])

        bist_svc = Bist100EarningsService(provider=mock_kap, official_ir_reg=ir_reg)  # type: ignore
        nasdaq_svc = MockNasdaqService()  # type: ignore

        mock_email_svc = MagicMock()
        mock_email_svc.is_configured.return_value = False

        job = EarningsRefreshJob(
            bist_service=bist_svc,
            nasdaq_service=nasdaq_svc,  # type: ignore
            state_store=rolling_cache,
            email_service=mock_email_svc
        )

        as_of = datetime(2026, 8, 14, tzinfo=ZoneInfo("UTC"))
        job_result = await job.run(as_of=as_of)
        mock_email_svc.send_email.assert_not_called()

        assert job_result.overall_status == "partial"
        ir_summary = job_result.bist.official_ir_summary
        assert ir_summary is not None
        assert ir_summary.ir_carried_forward_count == 1

        email_body = format_earnings_refresh_email(job_result)
        assert "AKSA" in email_body
        assert "Önceki Cache'ten Taşınanlar: AKSA" in email_body or "AKSA: Parser hatası" in email_body
        # Check that the provider warning appears exactly once in the email body
        assert email_body.count("AKSA HTTP 500 connection down") == 1


@pytest.mark.asyncio
async def test_official_ir_failure_carry_forward_in_rolling_cache():
    # Previous state_store has BIMAS Official IR event
    bimas_ev = BistEarningsEvent(
        symbol="BIMAS",
        company_name="BIMAS",
        fiscal_year=2026,
        fiscal_period="Q3",
        earnings_date="2026-11-09",
        period_start="2026-01-01",
        period_end="2026-09-30",
        date_type="company_announced",
        source="Official IR",
        source_provider="BIMAS Investor Relations",
        source_type="official_financial_calendar",
        source_event_id="bimas:2026:Q3",
        source_url="https://bim"
    )
    previous_bist = Bist100EarningsCalendar(
        schema_version="2.0",
        generated_at="2026-08-13T12:00:00Z",
        coverage_start="2026-08-14",
        coverage_end="2026-11-13",
        status="success",
        events=[bimas_ev],
        symbol_results=[]
    )

    # Current refresh: KAP is complete for BIMAS but found 0 events; BIMAS IR failed (HTTP 500)
    current_bist = Bist100EarningsCalendar(
        schema_version="2.0",
        generated_at="2026-08-14T12:00:00Z",
        coverage_start="2026-08-14",
        coverage_end="2026-11-13",
        status="partial",
        events=[],
        ir_incomplete_symbols=["BIMAS"],
        warnings=["HTTP 500 from BIMAS IR"]
    )

    merged = merge_bist_partial_calendar(previous=previous_bist, current=current_bist)
    assert len(merged.events) == 1
    assert merged.events[0].symbol == "BIMAS"
    assert merged.events[0].source == "Official IR"
    assert merged.status == "partial"
    assert any("carried forward" in w.lower() for w in merged.warnings)


@pytest.mark.asyncio
async def test_official_ir_http_retry_comprehensive_matrix():
    provider = BimasIrProvider()

    # 1. 500 -> 500 -> 200: success, retry_count 2
    call_count = 0

    def retry_500_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(500, text="Internal Server Error")
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "bimas_2026.html")
        with open(fixture_path, encoding="utf-8") as f:
            return httpx.Response(200, text=f.read())

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        transport = httpx.MockTransport(retry_500_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await provider.fetch_calendar(client=client)
            assert res.status == "success"
            assert len(res.events) == 2
            assert res.retry_count == 2
            assert mock_sleep.call_count == 2

    # 2. 429 -> 200: success, retry_count 1
    call_count = 0

    def retry_429_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, text="Too Many Requests")
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "bimas_2026.html")
        with open(fixture_path, encoding="utf-8") as f:
            return httpx.Response(200, text=f.read())

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        transport = httpx.MockTransport(retry_429_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await provider.fetch_calendar(client=client)
            assert res.status == "success"
            assert res.retry_count == 1
            assert mock_sleep.call_count == 1

    # 3. 501 -> 200: success, retry_count 1 (testing arbitrary 5xx)
    call_count = 0

    def retry_501_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(501, text="Not Implemented")
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "bimas_2026.html")
        with open(fixture_path, encoding="utf-8") as f:
            return httpx.Response(200, text=f.read())

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        transport = httpx.MockTransport(retry_501_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await provider.fetch_calendar(client=client)
            assert res.status == "success"
            assert res.retry_count == 1
            assert mock_sleep.call_count == 1

    # 4. 404: failed, non-retriable, retry_count 0
    def not_found_handler(request: httpx.Request):
        return httpx.Response(404, text="Not Found")

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        transport = httpx.MockTransport(not_found_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await provider.fetch_calendar(client=client)
            assert res.status == "failed"
            assert res.retry_count == 0
            assert mock_sleep.call_count == 0

    # 5. 500 -> 500 -> 500: failed, retry exhaustion
    def exhaust_handler(request: httpx.Request):
        return httpx.Response(500, text="Server Error")

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        transport = httpx.MockTransport(exhaust_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await provider.fetch_calendar(client=client)
            assert res.status == "failed"
            assert res.retry_count == 2
            assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_smoke_job_quality_gate_exit_codes_and_counter_matching():
    ok_ev = BistEarningsEvent(
        symbol="BIMAS", company_name="BIMAS", fiscal_year=2026, fiscal_period="Q2",
        earnings_date="2026-08-17", period_start="2026-01-01", period_end="2026-06-30",
        date_type="company_announced", source="Official IR", source_provider="BIMAS IR",
        source_type="official_financial_calendar", source_event_id="bimas:2026:Q2", source_url="https://bim"
    )
    ok_res = OfficialIrProviderResult(
        symbol="BIMAS", source_provider="BIMAS IR", status="success", events=[ok_ev], rows_seen=1, accepted_row_count=1
    )

    # 1. Full Success -> Exit Code 0
    reg_ok = OfficialIrRegistry(providers=[DummyProvider("BIMAS", ok_res)])
    exit_code_ok = await run_official_ir_smoke_test(registry=reg_ok)
    assert exit_code_ok == 0

    # 2. Empty registry -> Exit Code 1
    reg_empty = OfficialIrRegistry(providers=[])
    exit_code_empty = await run_official_ir_smoke_test(registry=reg_empty)
    assert exit_code_empty == 1

    # 3. Provider with 0 events -> Exit Code 1
    zero_res = OfficialIrProviderResult(
        symbol="MAVI", source_provider="MAVI IR", status="success", events=[], rows_seen=0, accepted_row_count=0
    )
    reg_zero = OfficialIrRegistry(providers=[DummyProvider("MAVI", zero_res)])
    exit_code_zero = await run_official_ir_smoke_test(registry=reg_zero)
    assert exit_code_zero == 1

    # 4. Provider with failure -> Exit Code 1
    fail_res = OfficialIrProviderResult(
        symbol="GARAN", source_provider="GARAN IR", status="failed", parser_failed=True, events=[]
    )
    reg_fail = OfficialIrRegistry(providers=[DummyProvider("GARAN", fail_res)])
    exit_code_fail = await run_official_ir_smoke_test(registry=reg_fail)
    assert exit_code_fail == 1

    # 5. Equal length but duplicate results (BIMAS, BIMAS instead of BIMAS, GARAN) -> Exit Code 1
    p1 = DummyProvider("BIMAS", ok_res)
    p2 = DummyProvider("GARAN", ok_res)
    reg_dup = OfficialIrRegistry(providers=[p1, p2])
    with patch.object(reg_dup, "fetch_all", new_callable=AsyncMock) as mock_fetch_all:
        mock_fetch_all.return_value = [ok_res, ok_res]  # 2 results, but both are BIMAS!
        exit_code_dup = await run_official_ir_smoke_test(registry=reg_dup)
        assert exit_code_dup == 1

    # 6. Unexpected extra symbol -> Exit Code 1
    reg_extra = OfficialIrRegistry(providers=[DummyProvider("BIMAS", ok_res)])
    extra_res = OfficialIrProviderResult(symbol="EXTRA", source_provider="EXTRA", status="success", events=[ok_ev])
    with patch.object(reg_extra, "fetch_all", new_callable=AsyncMock) as mock_fetch_all:
        mock_fetch_all.return_value = [ok_res, extra_res]
        exit_code_extra = await run_official_ir_smoke_test(registry=reg_extra)
        assert exit_code_extra == 1


def test_smoke_cli_main_entrypoint():
    from app.jobs.official_ir_smoke_job import main

    with patch("app.jobs.official_ir_smoke_job.run_official_ir_smoke_test", new_callable=AsyncMock) as mock_smoke, \
         patch("sys.exit") as mock_exit:
        mock_smoke.return_value = 0
        main()
        mock_smoke.assert_called_once()
        mock_exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_job_integration_historical_mavi_conflict_does_not_make_rolling_job_partial():
    with tempfile.TemporaryDirectory():
        rolling_cache = MemoryCalendarStateStore()

        # KAP returns 1 event in 2026 rolling coverage
        kap_ev = BistEarningsEvent(
            symbol="PGSUS", company_name="PEGASUS", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30",
            publish_date="2026-01-10", date_type="company_announced", source="KAP",
            source_type="financial_calendar", disclosure_index=1, source_url="https://kap/1"
        )
        kap_res = KapEarningsFetchResult(
            status="success", events=[kap_ev], normalized_events=[kap_ev], warnings=[]
        )
        mock_kap = MockKapProvider(kap_res)

        # MAVI IR returns valid 2026 event + historical 2023 conflict (2 different dates for Q1 2023)
        mavi_2026 = BistEarningsEvent(
            symbol="MAVI", company_name="MAVI", fiscal_year=2026, fiscal_period="Q2",
            earnings_date="2026-09-16", period_start="2026-02-01", period_end="2026-07-31",
            date_type="company_announced", source="Official IR", source_provider="MAVI IR",
            source_type="official_financial_calendar", source_event_id="eid-520", source_url="https://mavi"
        )
        mavi_2023_a = BistEarningsEvent(
            symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
            earnings_date="2023-06-08", period_start="2023-02-01", period_end="2023-04-30",
            date_type="company_announced", source="Official IR", source_provider="MAVI IR",
            source_type="official_financial_calendar", source_event_id="eid-387", source_url="https://mavi"
        )
        mavi_2023_b = BistEarningsEvent(
            symbol="MAVI", company_name="MAVI", fiscal_year=2023, fiscal_period="Q1",
            earnings_date="2023-06-09", period_start="2023-02-01", period_end="2023-04-30",
            date_type="company_announced", source="Official IR", source_provider="MAVI IR",
            source_type="official_financial_calendar", source_event_id="eid-388", source_url="https://mavi"
        )
        mavi_res = OfficialIrProviderResult(
            symbol="MAVI", source_provider="MAVI IR", status="success",
            events=[mavi_2026, mavi_2023_a, mavi_2023_b], rows_seen=3, accepted_row_count=3
        )
        ir_reg = OfficialIrRegistry(providers=[DummyProvider("MAVI", mavi_res)])

        bist_svc = Bist100EarningsService(provider=mock_kap, official_ir_reg=ir_reg)  # type: ignore
        nasdaq_svc = MockNasdaqService()  # type: ignore

        mock_email_svc = MagicMock()
        mock_email_svc.is_configured.return_value = False

        job = EarningsRefreshJob(
            bist_service=bist_svc,
            nasdaq_service=nasdaq_svc,  # type: ignore
            state_store=rolling_cache,
            email_service=mock_email_svc
        )

        as_of = datetime(2026, 8, 14, tzinfo=ZoneInfo("UTC"))
        job_result = await job.run(as_of=as_of)
        mock_email_svc.send_email.assert_not_called()

        # Overall status must remain SUCCESS!
        assert job_result.overall_status == "success"
        assert job_result.bist.outcome_status == "success"
        assert job_result.bist.fetch_status == "success"


def test_central_smtp_security_gate_blocks_real_smtp():
    """Verify that any attempt to establish a real SMTP connection fails immediately with AssertionError."""
    with pytest.raises(AssertionError, match="Real SMTP access is forbidden during tests"):
        smtplib.SMTP("smtp.gmail.com", 587)


@pytest.mark.asyncio
async def test_job_uses_mock_email_service_and_never_touches_real_smtp():
    """Verify that EarningsRefreshJob with configured mock EmailService invokes mock without touching real SMTP."""
    with tempfile.TemporaryDirectory():
        rolling_cache = MemoryCalendarStateStore()

        kap_res = KapEarningsFetchResult(status="success", events=[], normalized_events=[], warnings=[])
        mock_kap = MockKapProvider(kap_res)
        bist_svc = Bist100EarningsService(provider=mock_kap, official_ir_reg=OfficialIrRegistry(providers=[]))  # type: ignore
        nasdaq_svc = MockNasdaqService()  # type: ignore

        mock_email_svc = MagicMock()
        mock_email_svc.is_configured.return_value = True
        mock_email_svc.send_email.return_value = True

        job = EarningsRefreshJob(
            bist_service=bist_svc,
            nasdaq_service=nasdaq_svc,  # type: ignore
            state_store=rolling_cache,
            email_service=mock_email_svc
        )

        as_of = datetime(2026, 8, 14, tzinfo=ZoneInfo("UTC"))
        job_result = await job.run(as_of=as_of)

        assert job_result.overall_status == "success"
        assert job_result.email_status == "sent"
        mock_email_svc.send_email.assert_called_once()
        # Verify smtplib.SMTP was never touched and monkeypatched guard didn't fail


@pytest.mark.asyncio
async def test_job_integration_mavi_full_html_daily_refresh():
    """Verify end-to-end that MAVI provider parsing works through the daily refresh."""
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "official_ir", "mavi_2026.html")
    with open(fixture_path, encoding="utf-8") as f:
        mavi_html = f.read()

    from app.providers.earnings.official_ir.mavi import MaviIrProvider

    class RealHtmlMaviProvider(MaviIrProvider):
        async def fetch_calendar(self, client=None):
            return self.parse_html(mavi_html)

    with tempfile.TemporaryDirectory():
        rolling_cache = MemoryCalendarStateStore()

        kap_res = KapEarningsFetchResult(status="success", events=[], normalized_events=[], warnings=[])
        mock_kap = MockKapProvider(kap_res)
        ir_reg = OfficialIrRegistry(providers=[RealHtmlMaviProvider()])

        bist_svc = Bist100EarningsService(provider=mock_kap, official_ir_reg=ir_reg)  # type: ignore
        nasdaq_svc = MockNasdaqService()  # type: ignore

        mock_email_svc = MagicMock()
        mock_email_svc.is_configured.return_value = True
        mock_email_svc.send_email.return_value = True

        job = EarningsRefreshJob(
            bist_service=bist_svc,
            nasdaq_service=nasdaq_svc,  # type: ignore
            state_store=rolling_cache,
            email_service=mock_email_svc
        )

        as_of = datetime(2026, 8, 14, tzinfo=ZoneInfo("UTC"))
        job_result = await job.run(as_of=as_of)

        # 1. Daily refresh checks
        assert job_result.overall_status == "success"
        assert job_result.bist.outcome_status == "success"
        assert job_result.bist.official_ir_summary is not None
        assert len(job_result.bist.official_ir_summary.providers) == 1
        mavi_prov = job_result.bist.official_ir_summary.providers[0]
        assert mavi_prov.symbol == "MAVI"
        assert mavi_prov.status == "success"
        assert mavi_prov.warnings == []
        assert job_result.bist.official_ir_summary.ir_normalized_event_count == 1
        assert "MAVI" not in job_result.bist.unavailable_symbols
        assert not any("MAVI" in w for w in job_result.bist.warnings)

