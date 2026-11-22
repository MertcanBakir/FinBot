import logging
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.earnings_refresh_job import (
    EarningsRefreshJob,
    exit_code_for_result,
)
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    KapEarningsFetchResult,
    KapSymbolEarningsStatus,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsEvent,
)
from app.services.bist100_earnings import Bist100EarningsRefreshBundle
from app.services.bist_fiscal_period_archive import (
    BistFiscalPeriodArchive,
    BistPartitionUpsertResult,
)
from app.services.calendar_state_store import (
    BIST_ROLLING_CACHE_FILE,
    NASDAQ_ROLLING_CACHE_FILE,
    JsonCalendarStateStore,
)

PARTITION_DIR = Path(__file__).parent.parent / "data" / "generated" / "earnings" / "bist100"


from tests.conftest import create_mock_db_connection


@pytest.fixture(autouse=True)
def mock_job_db(monkeypatch):
    mock_conn = create_mock_db_connection()
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_conn
    monkeypatch.setattr("app.jobs.earnings_refresh_job.get_db_connection", lambda: mock_ctx)


@pytest.fixture
def mock_cache(tmp_path):
    bist_file = str(tmp_path / "bist100_earnings_calendar.json")
    nasdaq_file = str(tmp_path / "nasdaq100_earnings_calendar.json")
    return JsonCalendarStateStore(bist_file_path=bist_file, nasdaq_file_path=nasdaq_file)


@pytest.fixture
def mock_fiscal_archive(tmp_path):
    part_dir = str(tmp_path / "earnings" / "bist100")
    return BistFiscalPeriodArchive(data_dir=part_dir)


def make_bist_bundle(
    status="success",
    events=None,
    normalized_events=None,
    symbol_results=None,
    coverage_start="2026-08-13",
    coverage_end="2026-11-11",
    warnings=None
):
    events = events if events is not None else [
        BistEarningsEvent(
            symbol="CCOLA",
            company_name="CCOLA",
            earnings_date="2026-08-15",
            period_start="2026-01-01",
            period_end="2026-06-30",
            fiscal_year=2026,
            fiscal_period="Q2",
            publish_date="2026-08-10",
            disclosure_index=1627747,
            source_url="https://www.kap.org.tr/tr/Bildirim/1627747"
        )
    ]
    normalized_events = normalized_events if normalized_events is not None else events
    symbol_results = symbol_results if symbol_results is not None else [
        KapSymbolEarningsStatus(symbol="CCOLA", is_complete=True, calendar_status="scheduled")
    ]
    warn_list = warnings if warnings is not None else []

    cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        status=status,
        events=events if status != "failed" else [],
        symbol_results=symbol_results if status != "failed" else [],
        warnings=warn_list
    )
    fetch_res = KapEarningsFetchResult(
        status=status,
        events=events if status != "failed" else [],
        normalized_events=normalized_events if status != "failed" else [],
        symbol_results=symbol_results if status != "failed" else [],
        warnings=warn_list
    )
    return Bist100EarningsRefreshBundle(rolling_calendar=cal, fetch_result=fetch_res)


# --- A. Full Success & Email Body Assertions ---

@pytest.mark.asyncio
async def test_job_full_success(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bundle = make_bist_bundle(status="success")
    bist_service.get_refresh_bundle = AsyncMock(return_value=bundle)

    nasdaq_service = MagicMock()
    nasdaq_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="success",
        events=[NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-15", session="pre_market", session_label="Pre-Market")]
    )
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=nasdaq_cal)

    email_service = MagicMock()
    email_service.is_configured.return_value = True
    email_service.send_email.return_value = True

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.overall_status == "success"
    assert res.bist.outcome_status == "success"
    assert res.bist.sync_action == "replaced"
    assert res.bist.archive_outcome is not None
    assert res.bist.archive_outcome.archive_status == "success"
    assert res.bist.archive_outcome.files_created == 1
    assert res.nasdaq.outcome_status == "success"
    assert res.nasdaq.sync_action == "replaced"
    assert res.email_status == "sent"
    assert exit_code_for_result(res) == 0

    # Verify email delivery call assertions
    email_service.send_email.assert_called_once()
    call_kwargs = email_service.send_email.call_args[1]
    subject = call_kwargs["subject"]
    body_text = call_kwargs["body_text"]
    body_html = call_kwargs["body_html"]

    assert "FinBot Bilanço Takvimi" in subject
    assert "13 Ağustos 2026" in subject
    assert "[BIST100]" in body_text
    assert "[NASDAQ100]" in body_text
    assert "Durum güncellendi" in body_text
    assert body_html is not None
    assert "BIST100" in body_html
    assert "NASDAQ100" in body_html

    assert mock_cache.load_bist() is not None
    assert mock_cache.load_nasdaq() is not None


# --- B & C. Independent Market Exception Handling ---

@pytest.mark.asyncio
async def test_bist_success_nasdaq_exception(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(side_effect=RuntimeError("Yahoo Crash"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.overall_status == "partial"
    assert res.bist.outcome_status == "success"
    assert res.bist.sync_action == "replaced"
    assert res.nasdaq.outcome_status == "failed"
    assert res.nasdaq.sync_action == "not_written"
    assert res.email_status == "skipped_not_configured"
    assert exit_code_for_result(res) == 0


@pytest.mark.asyncio
async def test_bist_exception_nasdaq_success(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(side_effect=RuntimeError("KAP Crash"))

    nasdaq_service = MagicMock()
    nasdaq_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="success",
        events=[NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-15", session="pre_market", session_label="Pre-Market")]
    )
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=nasdaq_cal)

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.overall_status == "partial"
    assert res.bist.outcome_status == "failed"
    assert res.bist.sync_action == "not_written"
    assert res.nasdaq.outcome_status == "success"
    assert res.nasdaq.sync_action == "replaced"
    assert res.nasdaq.state_updated is True
    assert mock_cache.load_nasdaq() is not None
    assert exit_code_for_result(res) == 0


@pytest.mark.asyncio
async def test_bist_exception_previous_cache_nasdaq_success(mock_cache, mock_fiscal_archive):
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        status="success",
        events=[
            BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30")
        ],
        symbol_results=[
            KapSymbolEarningsStatus(symbol="CCOLA", is_complete=True, calendar_status="scheduled"),
            KapSymbolEarningsStatus(symbol="AKBNK", is_complete=True, calendar_status="no_announced_date_in_coverage")
        ]
    )
    mock_cache.save_bist(prev_cal)

    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(side_effect=RuntimeError("KAP down"))

    nasdaq_service = MagicMock()
    nasdaq_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="success",
        events=[NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-15", session="pre_market", session_label="Pre-Market")]
    )
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=nasdaq_cal)

    email_service = MagicMock()
    email_service.is_configured.return_value = True
    email_service.send_email.return_value = True

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.overall_status == "partial"
    assert res.bist.fetch_status == "failed"
    assert res.bist.outcome_status == "failed"
    assert res.bist.sync_action == "preserved_previous"
    assert res.bist.state_preserved is True
    assert res.bist.state_updated is False

    assert res.bist.total_symbol_count is None
    assert res.bist.completed_symbol_count is None
    assert res.bist.unavailable_symbol_count is None
    assert res.bist.scheduled_symbol_count is None
    assert res.bist.healthy_negative_symbol_count is None
    assert res.bist.unavailable_symbols == []

    assert res.nasdaq.outcome_status == "success"
    assert res.nasdaq.state_updated is True
    assert mock_cache.load_bist().events[0].symbol == "CCOLA"

    email_service.send_email.assert_called_once()
    body_text = email_service.send_email.call_args[1]["body_text"]

    assert "Güncel sembol kontrol sayıları üretilemedi" in body_text
    assert "Önceki güvenilir durum korundu" in body_text
    assert "Güvenilir Kontrol Edilen Sembol: 2" not in body_text
    assert "RuntimeError" in body_text


@pytest.mark.asyncio
async def test_failed_bist_model_with_previous_cache(mock_cache, mock_fiscal_archive):
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        status="success",
        events=[BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-20")],
        symbol_results=[KapSymbolEarningsStatus(symbol="CCOLA", is_complete=True)]
    )
    mock_cache.save_bist(prev_cal)

    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="failed"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.bist.total_symbol_count is None
    assert res.bist.sync_action == "preserved_previous"
    assert res.bist.state_preserved is True
    assert mock_cache.load_bist().events[0].symbol == "CCOLA"


# --- D. Dual Service Exceptions ---

@pytest.mark.asyncio
async def test_both_services_exception(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(side_effect=RuntimeError("KAP Crash"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(side_effect=RuntimeError("Yahoo Crash"))

    email_service = MagicMock()
    email_service.is_configured.return_value = True
    email_service.send_email.return_value = True

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.overall_status == "failed"
    assert res.bist.outcome_status == "failed"
    assert res.nasdaq.outcome_status == "failed"
    assert res.email_status == "sent"
    assert exit_code_for_result(res) == 1


# --- E. BIST Partial Merge ---

@pytest.mark.asyncio
async def test_bist_partial_merge_job_flow(mock_cache, mock_fiscal_archive):
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        events=[
            BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30"),
            BistEarningsEvent(symbol="GARAN", company_name="GARAN", earnings_date="2026-09-10", period_start="2026-01-01", period_end="2026-06-30")
        ],
        symbol_results=[
            KapSymbolEarningsStatus(symbol="CCOLA", is_complete=True),
            KapSymbolEarningsStatus(symbol="GARAN", is_complete=True)
        ]
    )
    mock_cache.save_bist(prev_cal)

    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(
        status="partial",
        events=[BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-22", period_start="2026-01-01", period_end="2026-06-30", fiscal_year=2026, fiscal_period="Q2", publish_date="2026-08-10", disclosure_index=1627747, source_url="https://www.kap.org.tr/tr/Bildirim/1627747")],
        symbol_results=[
            KapSymbolEarningsStatus(symbol="CCOLA", is_complete=True, calendar_status="scheduled"),
            KapSymbolEarningsStatus(symbol="GARAN", is_complete=False, calendar_status="unavailable")
        ]
    ))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.bist.sync_action == "merged_and_updated"
    assert res.bist.diff.changed_count == 1
    assert res.bist.diff.carried_forward_count == 1
    assert res.bist.unavailable_symbols == ["GARAN"]


# --- F. Nasdaq Partial Preservation ---

@pytest.mark.asyncio
async def test_nasdaq_partial_preservation_job_flow(mock_cache, mock_fiscal_archive):
    prev_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        events=[NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-15", session="pre_market", session_label="Pre-Market")]
    )
    mock_cache.save_nasdaq(prev_cal)

    curr_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=[NasdaqEarningsEvent(symbol="MSFT", company_name="Microsoft", earnings_date="2026-08-20", session="after_hours", session_label="After Hours")]
    )

    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.nasdaq.sync_action == "preserved_previous"
    assert res.nasdaq.state_preserved is True
    assert mock_cache.load_nasdaq().events[0].symbol == "AAPL"


# --- Initial Partial Tests ---

@pytest.mark.asyncio
async def test_bist_initial_partial(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="partial"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.bist.sync_action == "initial_partial_written"
    assert res.bist.state_updated is True
    assert res.bist.state_preserved is False
    assert res.bist.outcome_status == "partial"


@pytest.mark.asyncio
async def test_nasdaq_initial_partial(mock_cache, mock_fiscal_archive):
    curr_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=[NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-15", session="pre_market", session_label="Pre-Market")]
    )

    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.nasdaq.sync_action == "initial_partial_written"
    assert res.nasdaq.state_updated is True
    assert res.nasdaq.state_preserved is False
    assert res.nasdaq.outcome_status == "partial"


# --- Requirement 10: Partition Archive Status & Error Scenarios ---

@pytest.mark.asyncio
async def test_partial_fetch_archive_outcome(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bundle = make_bist_bundle(status="partial", warnings=["Partial KAP OID timeout"])
    bist_service.get_refresh_bundle = AsyncMock(return_value=bundle)

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.bist.outcome_status == "partial"
    assert res.bist.archive_outcome is not None
    assert res.bist.archive_outcome.archive_status == "partial"
    assert "Partial KAP OID timeout" in res.bist.archive_outcome.warnings

    part = mock_fiscal_archive.load_partition(2026, "Q2")
    assert part is not None
    assert part.status == "partial"
    assert part.provider_status == "partial"


@pytest.mark.asyncio
async def test_archive_write_failure_behavior(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bundle = make_bist_bundle(status="success")
    bist_service.get_refresh_bundle = AsyncMock(return_value=bundle)

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = True
    email_service.send_email.return_value = True

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    with patch.object(mock_fiscal_archive, "upsert_events", return_value=BistPartitionUpsertResult(status="failed", error="Partition disk error")):
        res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

        assert res.bist.outcome_status == "partial"
        assert res.bist.archive_outcome.archive_status == "failed"
        assert res.bist.archive_outcome.error == "Partition disk error"
        assert res.nasdaq.outcome_status == "success"
        assert res.nasdaq.state_updated is True
        assert exit_code_for_result(res) == 0

        body_text = email_service.send_email.call_args[1]["body_text"]
        assert body_text.count("Partition disk error") == 1


@pytest.mark.asyncio
async def test_failed_fetch_archive_behavior(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bundle = make_bist_bundle(status="failed", warnings=["KAP Network Outage"])
    bist_service.get_refresh_bundle = AsyncMock(return_value=bundle)

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.bist.outcome_status == "failed"
    assert res.bist.archive_outcome.archive_status == "failed"
    assert not os.path.exists(mock_fiscal_archive.data_dir) or len(os.listdir(mock_fiscal_archive.data_dir)) == 0


@pytest.mark.asyncio
async def test_identical_daily_refresh(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bundle = make_bist_bundle(status="success")
    bist_service.get_refresh_bundle = AsyncMock(return_value=bundle)

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    # First run creates partition file
    res1 = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))
    assert res1.bist.archive_outcome.files_created == 1

    part_file = mock_fiscal_archive._get_partition_path("Q2", 2026)
    with open(part_file, "rb") as f:
        bytes1 = f.read()

    # Second run with identical events
    res2 = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))
    assert res2.bist.archive_outcome.events_unchanged > 0
    assert res2.bist.archive_outcome.files_created == 0
    assert res2.bist.archive_outcome.files_updated == 0
    assert res2.bist.archive_outcome.affected_files == []

    with open(part_file, "rb") as f:
        bytes2 = f.read()

    assert bytes1 == bytes2


# --- Save True but Reload Failed Test ---

@pytest.mark.asyncio
async def test_save_true_reload_failed(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    with patch.object(mock_cache, "save_bist", return_value=True), \
         patch.object(mock_cache, "load_bist", return_value=None):
        res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

        assert res.bist.sync_action == "write_failed"
        assert res.bist.outcome_status == "failed"
        assert res.bist.error == "State reload failed after save_bist returned True"


# --- Save Exception Isolation Test ---

@pytest.mark.asyncio
async def test_save_exception_isolation(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = True
    email_service.send_email.return_value = True

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    with patch.object(mock_cache, "save_bist", side_effect=OSError("disk failure")):
        res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

        assert res.bist.sync_action == "write_failed"
        assert res.bist.outcome_status == "failed"
        assert "OSError" in res.bist.error

        assert res.nasdaq.outcome_status == "success"

        call_kwargs = email_service.send_email.call_args[1]
        assert "OSError" in call_kwargs["body_text"]


# --- Write Failure Email Report Test ---

@pytest.mark.asyncio
async def test_write_failure_appears_in_email_body(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = True
    email_service.send_email.return_value = True

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    with patch.object(mock_cache, "save_bist", return_value=False):
        res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

        assert res.bist.sync_action == "write_failed"
        assert res.bist.error == "save_bist returned False for non-failed fetch"

        call_kwargs = email_service.send_email.call_args[1]
        body_text = call_kwargs["body_text"]
        body_html = call_kwargs["body_html"]
        assert "Durum yazılamadı" in body_text
        assert "save_bist returned False for non-failed fetch" in body_text
        assert "Durum yazılamadı" in body_html
        assert "save_bist returned False for non-failed fetch" in body_html


# --- Log Assertion Test ---

@pytest.mark.asyncio
async def test_exception_logging_no_nonetype_none(mock_cache, mock_fiscal_archive, caplog):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(side_effect=RuntimeError("KAP Failure"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    with caplog.at_level(logging.ERROR):
        await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert "NoneType: None" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


# --- `send_email=False` Test ---

@pytest.mark.asyncio
async def test_email_send_returns_false(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = True
    email_service.send_email.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.overall_status == "success"
    assert res.email_status == "failed"
    assert res.email_error == "E-posta gönderimi başarısız oldu (SMTP teslimat hatası)."
    assert exit_code_for_result(res) == 0


@pytest.mark.asyncio
async def test_services_called_with_same_as_of_datetime(mock_cache, mock_fiscal_archive):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    ref_as_of = datetime(2026, 8, 13, 10, 0, 0)

    await job.run(as_of=ref_as_of)

    bist_arg = bist_service.get_refresh_bundle.call_args[1]["as_of"]
    nasdaq_arg = nasdaq_service.get_rolling_calendar.call_args[1]["as_of"]

    assert bist_arg == nasdaq_arg


# --- Production Isolation Test ---

@pytest.mark.asyncio
async def test_production_state_sources_not_modified(mock_cache, mock_fiscal_archive):
    def snapshot_file(path_str: str):
        p = Path(path_str)
        return p.read_bytes() if p.exists() else None

    def snapshot_dir(dir_path: Path):
        if not dir_path.exists():
            return {}
        return {f.name: f.read_bytes() for f in dir_path.glob("*.json")}

    bist_prod_before = snapshot_file(BIST_ROLLING_CACHE_FILE)
    nasdaq_prod_before = snapshot_file(NASDAQ_ROLLING_CACHE_FILE)
    part_prod_before = snapshot_dir(PARTITION_DIR)

    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11"))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )
    await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    bist_prod_after = snapshot_file(BIST_ROLLING_CACHE_FILE)
    nasdaq_prod_after = snapshot_file(NASDAQ_ROLLING_CACHE_FILE)
    part_prod_after = snapshot_dir(PARTITION_DIR)

    assert bist_prod_before == bist_prod_after
    assert nasdaq_prod_before == nasdaq_prod_after
    assert part_prod_before == part_prod_after


# --- BIST Daily Archive Cutoff Tests ---

def test_filter_daily_archive_events_cutoff_rules():
    from app.jobs.earnings_refresh_job import (
        BIST_DAILY_ARCHIVE_MIN_EARNINGS_DATE,
        filter_daily_archive_events,
    )

    ev_hist_2025_q3 = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2025, fiscal_period="Q3",
        earnings_date="2025-11-07", period_start="2025-01-01", period_end="2025-09-30",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2025:Q3", source_url="https://halkb"
    )
    ev_fy25_announced_2026 = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2025, fiscal_period="FY",
        earnings_date="2026-02-20", period_start="2025-01-01", period_end="2025-12-31",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2025:FY", source_url="https://halkb"
    )
    ev_2026_q2 = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2026, fiscal_period="Q2",
        earnings_date="2026-08-07", period_start="2026-01-01", period_end="2026-06-30",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2026:Q2", source_url="https://halkb"
    )
    ev_fy26_announced_2027 = BistEarningsEvent(
        symbol="AKSA", company_name="AKSA", fiscal_year=2026, fiscal_period="FY",
        earnings_date="2027-02-17", period_start="2026-01-01", period_end="2026-12-31",
        date_type="company_announced", source="Official IR", source_provider="AKSA IR",
        source_type="official_financial_calendar", source_event_id="aksa:2026:FY", source_url="https://aksa"
    )

    all_events = [ev_hist_2025_q3, ev_fy25_announced_2026, ev_2026_q2, ev_fy26_announced_2027]
    filtered = filter_daily_archive_events(all_events, min_earnings_date=BIST_DAILY_ARCHIVE_MIN_EARNINGS_DATE)

    # 1. 2025 Q3 (earnings_date=2025-11-07) is excluded
    assert ev_hist_2025_q3 not in filtered

    # 2. FY 2025 (earnings_date=2026-02-20) is KEPT
    assert ev_fy25_announced_2026 in filtered

    # 3. 2026 Q2 (earnings_date=2026-08-07) is KEPT
    assert ev_2026_q2 in filtered

    # 4. FY 2026 (earnings_date=2027-02-17) is KEPT
    assert ev_fy26_announced_2027 in filtered

    assert len(filtered) == 3


@pytest.mark.asyncio
async def test_job_filters_historical_events_before_partition_upsert_while_preserving_rolling(mock_cache, mock_fiscal_archive):
    ev_hist1 = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2025, fiscal_period="Q3",
        earnings_date="2025-11-07", period_start="2025-01-01", period_end="2025-09-30",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2025:Q3", source_url="https://halkb"
    )
    ev_hist2 = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2017, fiscal_period="FY",
        earnings_date="2018-02-14", period_start="2017-01-01", period_end="2017-12-31",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2017:FY", source_url="https://halkb"
    )
    ev_fy25 = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2025, fiscal_period="FY",
        earnings_date="2026-02-20", period_start="2025-01-01", period_end="2025-12-31",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2025:FY", source_url="https://halkb"
    )
    ev_q2_26 = BistEarningsEvent(
        symbol="CCOLA", company_name="CCOLA", fiscal_year=2026, fiscal_period="Q2",
        earnings_date="2026-08-15", period_start="2026-01-01", period_end="2026-06-30",
        publish_date="2026-08-10", disclosure_index=1627747, date_type="company_announced",
        source="KAP", source_type="financial_calendar", source_url="https://kap/1627747"
    )

    # Normalized result contains all 4 events
    all_normalized = [ev_hist1, ev_hist2, ev_fy25, ev_q2_26]
    # Rolling calendar only has future 2026 event
    rolling_events = [ev_q2_26]

    bundle = make_bist_bundle(
        status="success",
        events=rolling_events,
        normalized_events=all_normalized,
        coverage_start="2026-08-13",
        coverage_end="2026-11-11"
    )

    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=bundle)

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11", status="success"
    ))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))
    assert res.overall_status == "success"

    # Historical partitions (earnings_date < 2026-01-01) MUST NOT be created
    assert mock_fiscal_archive.load_partition(2025, "Q3") is None
    assert mock_fiscal_archive.load_partition(2017, "FY") is None

    # Partitions with earnings_date >= 2026-01-01 MUST be created
    p_fy25 = mock_fiscal_archive.load_partition(2025, "FY")
    assert p_fy25 is not None
    assert len(p_fy25.events) == 1
    assert p_fy25.events[0].symbol == "HALKB"
    assert p_fy25.events[0].earnings_date == "2026-02-20"

    p_q2_26 = mock_fiscal_archive.load_partition(2026, "Q2")
    assert p_q2_26 is not None
    assert len(p_q2_26.events) == 1
    assert p_q2_26.events[0].symbol == "CCOLA"


@pytest.mark.asyncio
async def test_job_preserves_preexisting_historical_partition_byte_identical(mock_cache, mock_fiscal_archive, tmp_path):
    # Pre-seed a historical 2025 Q3 partition (created via explicit backfill)
    hist_ev = BistEarningsEvent(
        symbol="HALKB", company_name="HALKB", fiscal_year=2025, fiscal_period="Q3",
        earnings_date="2025-11-07", period_start="2025-01-01", period_end="2025-09-30",
        date_type="company_announced", source="Official IR", source_provider="HALKB IR",
        source_type="official_financial_calendar", source_event_id="halkb:2025:Q3", source_url="https://halkb"
    )
    from app.models.earnings import BistFiscalPeriodEarningsCalendar
    hist_cal = BistFiscalPeriodEarningsCalendar(
        schema_version="2.1",
        dataset_type="fiscal_period_partition",
        generated_at="2025-12-01T10:00:00Z",
        market="BIST100",
        fiscal_year=2025,
        fiscal_period="Q3",
        status="success",
        provider_status="success",
        events=[hist_ev],
        warnings=["Explicit backfill warning"]
    )
    mock_fiscal_archive._atomic_save(hist_cal)
    hist_file_path = Path(mock_fiscal_archive._get_partition_path("Q3", 2025))
    assert hist_file_path.exists()
    hist_bytes_before = hist_file_path.read_bytes()

    # Run Daily Refresh Job with only 2026 events
    q2_ev = BistEarningsEvent(
        symbol="CCOLA", company_name="CCOLA", fiscal_year=2026, fiscal_period="Q2",
        earnings_date="2026-08-15", period_start="2026-01-01", period_end="2026-06-30",
        publish_date="2026-08-10", disclosure_index=1627747, date_type="company_announced",
        source="KAP", source_type="financial_calendar", source_url="https://kap/1627747"
    )
    bundle = make_bist_bundle(
        status="success",
        events=[q2_ev],
        normalized_events=[q2_ev],
        coverage_start="2026-08-13",
        coverage_end="2026-11-11"
    )

    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=bundle)

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(return_value=Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11", status="success"
    ))

    email_service = MagicMock()
    email_service.is_configured.return_value = False

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        fiscal_archive=mock_fiscal_archive,
        email_service=email_service
    )

    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))
    assert res.overall_status == "success"

    # Verify that preexisting 2025 Q3 partition was NOT modified, deleted, or mutated
    assert hist_file_path.exists()
    hist_bytes_after = hist_file_path.read_bytes()
    assert hist_bytes_before == hist_bytes_after

    reloaded_hist = mock_fiscal_archive.load_partition(2025, "Q3")
    assert reloaded_hist is not None
    assert reloaded_hist.generated_at == "2025-12-01T10:00:00Z"
    assert reloaded_hist.warnings == ["Explicit backfill warning"]
    assert "bist100_Q3_2025_earnings_calendar.json" not in res.bist.archive_outcome.affected_files
