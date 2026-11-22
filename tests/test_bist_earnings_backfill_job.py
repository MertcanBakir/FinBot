import os
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.jobs.bist_earnings_backfill_job import run_bist_earnings_backfill
from app.models.earnings import (
    BistEarningsEvent,
    KapEarningsFetchMetrics,
    KapEarningsFetchResult,
    OfficialIrProviderResult,
)
from app.providers.earnings.kap_provider import KapEarningsProvider
from app.providers.earnings.official_ir.registry import OfficialIrRegistry
from app.services.bist_fiscal_period_archive import BistFiscalPeriodArchive
from app.services.calendar_state_store import (
    BIST_ROLLING_CACHE_FILE,
    NASDAQ_ROLLING_CACHE_FILE,
)

PROD_PARTITION_DIR = Path(__file__).parent.parent / "data" / "generated" / "earnings" / "bist100"


@pytest.fixture
def temp_cache_dir():
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)


@pytest.fixture
def empty_ir_registry():
    return OfficialIrRegistry(providers=[])


@pytest.mark.asyncio
async def test_backfill_default_ir_registry_bound_and_called(temp_cache_dir):
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    # KAP returns empty success
    kap_res = KapEarningsFetchResult(status="success", events=[], normalized_events=[])

    # IR event from BIMAS
    bimas_ev = BistEarningsEvent(
        symbol="BIMAS",
        company_name="BIMAS",
        fiscal_year=2026,
        fiscal_period="Q2",
        earnings_date="2026-08-17",
        period_start="2026-01-01",
        period_end="2026-06-30",
        date_type="company_announced",
        source="Official IR",
        source_provider="BIMAS Investor Relations",
        source_type="official_financial_calendar",
        source_event_id="bimas:2026:Q2",
        source_url="https://bim.com.tr"
    )
    ir_res = OfficialIrProviderResult(
        symbol="BIMAS",
        source_provider="BIMAS Investor Relations",
        status="success",
        events=[bimas_ev]
    )

    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_kap_fetch, \
         patch("app.jobs.bist_earnings_backfill_job.official_ir_registry.fetch_all", new_callable=AsyncMock) as mock_ir_fetch_all:

        mock_kap_fetch.return_value = kap_res
        mock_ir_fetch_all.return_value = [ir_res]

        # Call WITHOUT ir_registry argument -> must use default official_ir_registry
        exit_code = await run_bist_earnings_backfill(
            year=2026,
            provider=mock_provider,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 0
        mock_kap_fetch.assert_called_once()
        mock_ir_fetch_all.assert_called_once()

        # Check that BIMAS IR event was written to partition
        p_q2 = fiscal_archive.load_partition(2026, "Q2")
        assert p_q2 is not None
        assert len(p_q2.events) == 1
        assert p_q2.events[0].symbol == "BIMAS"
        assert p_q2.events[0].source == "Official IR"


@pytest.mark.asyncio
async def test_backfill_current_year_date_ranges(temp_cache_dir, empty_ir_registry):
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    tz = ZoneInfo(settings.TIMEZONE)
    today = datetime.now(tz).date()
    current_year = today.year

    ev_q1 = BistEarningsEvent(
        symbol="PGSUS",
        company_name="PEGASUS",
        fiscal_year=current_year,
        fiscal_period="Q1",
        period_start=f"{current_year}-01-01",
        period_end=f"{current_year}-03-31",
        earnings_date=f"{current_year}-05-11",
        publish_date=f"{current_year}-04-16",
        date_type="company_announced",
        source="KAP",
        source_type="financial_calendar",
        disclosure_index=1594449,
        source_url="https://www.kap.org.tr/tr/Bildirim/1594449"
    )

    fetch_res = KapEarningsFetchResult(
        status="success",
        events=[ev_q1],
        normalized_events=[ev_q1],
        metrics=KapEarningsFetchMetrics(financial_calendar_disclosure_count=1)
    )

    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = fetch_res

        exit_code = await run_bist_earnings_backfill(
            year=current_year,
            provider=mock_provider,
            ir_registry=empty_ir_registry,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 0
        mock_fetch.assert_called_once()
        call_kwargs = mock_fetch.call_args.kwargs

        # Current year: 1 Jan to 31 Dec coverage, published_from 1 Jan, published_to today
        assert call_kwargs["coverage_start"] == date(current_year, 1, 1)
        assert call_kwargs["coverage_end"] == date(current_year, 12, 31)
        assert call_kwargs["published_from"] == date(current_year, 1, 1)
        assert call_kwargs["published_to"] == today


@pytest.mark.asyncio
async def test_backfill_past_year_date_ranges(temp_cache_dir, empty_ir_registry):
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    past_year = 2024

    ev_fy = BistEarningsEvent(
        symbol="PGSUS",
        company_name="PEGASUS",
        fiscal_year=past_year,
        fiscal_period="FY",
        period_start=f"{past_year}-01-01",
        period_end=f"{past_year}-12-31",
        earnings_date=f"{past_year}-03-04",
        publish_date=f"{past_year}-02-15",
        date_type="company_announced",
        source="KAP",
        source_type="financial_calendar",
        disclosure_index=1000000,
        source_url="https://www.kap.org.tr/tr/Bildirim/1000000"
    )

    fetch_res = KapEarningsFetchResult(
        status="success",
        events=[ev_fy],
        normalized_events=[ev_fy],
        metrics=KapEarningsFetchMetrics(financial_calendar_disclosure_count=1)
    )

    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = fetch_res

        exit_code = await run_bist_earnings_backfill(
            year=past_year,
            provider=mock_provider,
            ir_registry=empty_ir_registry,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 0
        mock_fetch.assert_called_once()
        call_kwargs = mock_fetch.call_args.kwargs

        # Past year: entire year coverage and filing search
        assert call_kwargs["coverage_start"] == date(past_year, 1, 1)
        assert call_kwargs["coverage_end"] == date(past_year, 12, 31)
        assert call_kwargs["published_from"] == date(past_year, 1, 1)
        assert call_kwargs["published_to"] == date(past_year, 12, 31)


@pytest.mark.asyncio
async def test_backfill_future_year_rejects_without_calling_provider(temp_cache_dir, empty_ir_registry):
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    tz = ZoneInfo(settings.TIMEZONE)
    today = datetime.now(tz).date()
    future_year = today.year + 1

    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_fetch:
        exit_code = await run_bist_earnings_backfill(
            year=future_year,
            provider=mock_provider,
            ir_registry=empty_ir_registry,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 1
        mock_fetch.assert_not_called()
        assert len(os.listdir(temp_cache_dir)) == 0


@pytest.mark.asyncio
async def test_backfill_partial_fetch_writes_reliable_events_and_preserves_status(temp_cache_dir, empty_ir_registry):
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    ev_q1 = BistEarningsEvent(
        symbol="PGSUS",
        company_name="PEGASUS",
        fiscal_year=2026,
        fiscal_period="Q1",
        period_start="2026-01-01",
        period_end="2026-03-31",
        earnings_date="2026-05-11",
        publish_date="2026-04-16",
        date_type="company_announced",
        source="KAP",
        source_type="financial_calendar",
        disclosure_index=1594449,
        source_url="https://www.kap.org.tr/tr/Bildirim/1594449"
    )

    fetch_res = KapEarningsFetchResult(
        status="partial",
        events=[ev_q1],
        normalized_events=[ev_q1],
        warnings=["Partial OID timeout"],
        metrics=KapEarningsFetchMetrics(financial_calendar_disclosure_count=1)
    )

    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = fetch_res

        exit_code = await run_bist_earnings_backfill(
            year=2026,
            provider=mock_provider,
            ir_registry=empty_ir_registry,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 0
        part_q1 = fiscal_archive.load_partition(2026, "Q1")
        assert part_q1 is not None
        assert part_q1.status == "partial"
        assert part_q1.provider_status == "partial"
        assert "Partial OID timeout" in part_q1.warnings


@pytest.mark.asyncio
async def test_backfill_failed_fetch_exits_with_code_1_and_writes_zero_files(temp_cache_dir, empty_ir_registry):
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    fetch_res = KapEarningsFetchResult(
        status="failed",
        events=[],
        warnings=["Fetch failure"]
    )

    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = fetch_res

        exit_code = await run_bist_earnings_backfill(
            year=2026,
            provider=mock_provider,
            ir_registry=empty_ir_registry,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 1
        assert len(os.listdir(temp_cache_dir)) == 0


@pytest.mark.asyncio
async def test_backfill_strict_target_year_earnings_date_filter(temp_cache_dir, empty_ir_registry):
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    # 1. FY 2025 result announced in 2026 (earnings_date = 2026-03-17) -> MUST BE INCLUDED in 2026 backfill
    ev_fy25 = BistEarningsEvent(
        symbol="MAVI", company_name="MAVI", fiscal_year=2025, fiscal_period="FY",
        period_start="2025-02-01", period_end="2026-01-31", earnings_date="2026-03-17",
        publish_date="2026-01-10", date_type="company_announced", source="KAP",
        source_type="financial_calendar", disclosure_index=1, source_url="https://kap/1"
    )

    # 2. FY 2026 result announced in 2027 (earnings_date = 2027-02-17) -> MUST BE EXCLUDED from 2026 backfill
    ev_fy26 = BistEarningsEvent(
        symbol="AKSA", company_name="AKSA", fiscal_year=2026, fiscal_period="FY",
        period_start="2026-01-01", period_end="2026-12-31", earnings_date="2027-02-17",
        publish_date="2026-01-10", date_type="company_announced", source="KAP",
        source_type="financial_calendar", disclosure_index=2, source_url="https://kap/2"
    )

    fetch_res = KapEarningsFetchResult(
        status="success",
        events=[ev_fy25, ev_fy26],
        normalized_events=[ev_fy25, ev_fy26]
    )

    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = fetch_res

        exit_code = await run_bist_earnings_backfill(
            year=2026,
            provider=mock_provider,
            ir_registry=empty_ir_registry,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 0
        # FY 2025 partition created with MAVI
        p_fy25 = fiscal_archive.load_partition(2025, "FY")
        assert p_fy25 is not None
        assert len(p_fy25.events) == 1
        assert p_fy25.events[0].symbol == "MAVI"

        # FY 2026 partition NOT created (because AKSA announced in 2027)
        p_fy26 = fiscal_archive.load_partition(2026, "FY")
        assert p_fy26 is None


@pytest.mark.asyncio
async def test_backfill_does_not_modify_production_cache_or_partition_files(temp_cache_dir, empty_ir_registry):
    def snapshot_file(path_str: str):
        p = Path(path_str)
        return p.read_bytes() if p.exists() else None

    def snapshot_dir(dir_path: Path):
        if not dir_path.exists():
            return {}
        return {f.name: f.read_bytes() for f in dir_path.glob("*.json")}

    bist_before = snapshot_file(BIST_ROLLING_CACHE_FILE)
    nasdaq_before = snapshot_file(NASDAQ_ROLLING_CACHE_FILE)
    part_before = snapshot_dir(PROD_PARTITION_DIR)

    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    fetch_res = KapEarningsFetchResult(status="success", events=[], normalized_events=[])
    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = fetch_res
        await run_bist_earnings_backfill(
            year=2026, provider=mock_provider, ir_registry=empty_ir_registry, fiscal_archive=fiscal_archive
        )

    bist_after = snapshot_file(BIST_ROLLING_CACHE_FILE)
    nasdaq_after = snapshot_file(NASDAQ_ROLLING_CACHE_FILE)
    part_after = snapshot_dir(PROD_PARTITION_DIR)

    assert bist_before == bist_after
    assert nasdaq_before == nasdaq_after
    assert part_before == part_after


@pytest.mark.asyncio
async def test_backfill_2026_includes_halkb_q2_from_secondary_presentation_fallback(temp_cache_dir):
    import httpx

    from app.providers.earnings.official_ir.halkb import (
        PRIMARY_URL,
        SECONDARY_URL,
        HalkbIrProvider,
    )

    primary_fixture = Path(__file__).parent / "fixtures" / "official_ir" / "halkb_2026.html"
    secondary_fixture = Path(__file__).parent / "fixtures" / "official_ir" / "halkb_financial_presentations_2026.html"

    primary_text = primary_fixture.read_text(encoding="utf-8")
    secondary_text = secondary_fixture.read_text(encoding="utf-8")

    def halkb_mock_handler(request: httpx.Request):
        url_str = str(request.url)
        if url_str == PRIMARY_URL:
            return httpx.Response(200, text=primary_text)
        elif url_str == SECONDARY_URL:
            return httpx.Response(200, text=secondary_text)
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(halkb_mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        halkb_provider = HalkbIrProvider()
        halkb_result = await halkb_provider.fetch_calendar(client=client)

    ir_registry = OfficialIrRegistry(providers=[])
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_kap = KapEarningsProvider()
    kap_res = KapEarningsFetchResult(status="success", events=[], normalized_events=[])

    with patch.object(mock_kap, "fetch_calendar", new_callable=AsyncMock) as mock_kap_fetch, \
         patch.object(ir_registry, "fetch_all", new_callable=AsyncMock) as mock_ir_fetch:
        mock_kap_fetch.return_value = kap_res
        mock_ir_fetch.return_value = [halkb_result]

        exit_code = await run_bist_earnings_backfill(
            year=2026,
            provider=mock_kap,
            ir_registry=ir_registry,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 0

        # Verify Q2 2026 partition contains HALKB Q2 from secondary fallback
        p_q2 = fiscal_archive.load_partition(2026, "Q2")
        assert p_q2 is not None
        halkb_q2 = next((e for e in p_q2.events if e.symbol == "HALKB"), None)
        assert halkb_q2 is not None
        assert halkb_q2.fiscal_year == 2026
        assert halkb_q2.fiscal_period == "Q2"
        assert halkb_q2.earnings_date == "2026-08-07"
        assert halkb_q2.date_type == "company_announced"
        assert halkb_q2.source == "Official IR"
        assert halkb_q2.source_provider == "HALKB Investor Relations"
        assert halkb_q2.source_type == "official_financial_calendar"
        assert halkb_q2.source_url == SECONDARY_URL
        assert halkb_q2.source_event_id == "halkb:2026:Q2"

        # Verify Q1 2026 partition contains HALKB Q1 from primary
        p_q1 = fiscal_archive.load_partition(2026, "Q1")
        assert p_q1 is not None
        halkb_q1 = next((e for e in p_q1.events if e.symbol == "HALKB"), None)
        assert halkb_q1 is not None
        assert halkb_q1.earnings_date == "2026-05-08"
        assert halkb_q1.source_url == PRIMARY_URL


@pytest.mark.asyncio
async def test_backfill_year_2025_writes_2025_partition_unaffected_by_daily_cutoff(temp_cache_dir, empty_ir_registry):
    fiscal_archive = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    mock_provider = KapEarningsProvider()

    # 2025 Q3 event with earnings_date in 2025 (e.g. 2025-11-07)
    ev_2025_q3 = BistEarningsEvent(
        symbol="HALKB",
        company_name="TÜRKİYE HALK BANKASI A.Ş.",
        fiscal_year=2025,
        fiscal_period="Q3",
        earnings_date="2025-11-07",
        period_start="2025-01-01",
        period_end="2025-09-30",
        publish_date="2025-10-15",
        disclosure_index=1200000,
        date_type="company_announced",
        source="KAP",
        source_type="financial_calendar",
        source_url="https://kap/1200000"
    )
    kap_res = KapEarningsFetchResult(
        status="success",
        events=[ev_2025_q3],
        normalized_events=[ev_2025_q3]
    )

    with patch.object(mock_provider, "fetch_calendar", new_callable=AsyncMock) as mock_kap_fetch:
        mock_kap_fetch.return_value = kap_res

        # Explicit backfill for year 2025
        exit_code = await run_bist_earnings_backfill(
            year=2025,
            provider=mock_provider,
            ir_registry=empty_ir_registry,
            fiscal_archive=fiscal_archive
        )

        assert exit_code == 0

        # Verify that 2025 Q3 partition was successfully written by explicit backfill
        p_q3_2025 = fiscal_archive.load_partition(2025, "Q3")
        assert p_q3_2025 is not None
        assert len(p_q3_2025.events) == 1
        assert p_q3_2025.events[0].symbol == "HALKB"
        assert p_q3_2025.events[0].earnings_date == "2025-11-07"
