import logging
from datetime import datetime
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
from tests.conftest import create_mock_db_connection
from tests.state_store_fakes import MemoryCalendarStateStore


@pytest.fixture(autouse=True)
def mock_job_db(monkeypatch):
    mock_conn = create_mock_db_connection()
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_conn
    monkeypatch.setattr("app.jobs.earnings_refresh_job.get_db_connection", lambda: mock_ctx)


@pytest.fixture
def mock_cache():
    return MemoryCalendarStateStore()


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
async def test_job_full_success(mock_cache):
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
        email_service=email_service
    )

    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.overall_status == "success"
    assert res.bist.outcome_status == "success"
    assert res.bist.sync_action == "replaced"
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
async def test_bist_success_nasdaq_exception(mock_cache):
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
async def test_bist_exception_nasdaq_success(mock_cache):
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
async def test_bist_exception_previous_cache_nasdaq_success(mock_cache):
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
    mock_cache.stage_bist(prev_cal)

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
async def test_failed_bist_model_with_previous_cache(mock_cache):
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        status="success",
        events=[BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-20")],
        symbol_results=[KapSymbolEarningsStatus(symbol="CCOLA", is_complete=True)]
    )
    mock_cache.stage_bist(prev_cal)

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
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.bist.total_symbol_count is None
    assert res.bist.sync_action == "preserved_previous"
    assert res.bist.state_preserved is True
    assert mock_cache.load_bist().events[0].symbol == "CCOLA"


# --- D. Dual Service Exceptions ---

@pytest.mark.asyncio
async def test_both_services_exception(mock_cache):
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
async def test_bist_partial_merge_job_flow(mock_cache):
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
    mock_cache.stage_bist(prev_cal)

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
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.bist.sync_action == "merged_and_updated"
    assert res.bist.diff.changed_count == 1
    assert res.bist.diff.carried_forward_count == 1
    assert res.bist.unavailable_symbols == ["GARAN"]


# --- F. Nasdaq Partial Preservation ---

@pytest.mark.asyncio
async def test_nasdaq_partial_preservation_job_flow(mock_cache):
    prev_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        events=[NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-15", session="pre_market", session_label="Pre-Market")]
    )
    mock_cache.stage_nasdaq(prev_cal)

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
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.nasdaq.sync_action == "preserved_previous"
    assert res.nasdaq.state_preserved is True
    assert mock_cache.load_nasdaq().events[0].symbol == "AAPL"


# --- Initial Partial Tests ---

@pytest.mark.asyncio
async def test_bist_initial_partial(mock_cache):
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
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.bist.sync_action == "initial_partial_written"
    assert res.bist.state_updated is True
    assert res.bist.state_preserved is False
    assert res.bist.outcome_status == "partial"


@pytest.mark.asyncio
async def test_nasdaq_initial_partial(mock_cache):
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
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.nasdaq.sync_action == "initial_partial_written"
    assert res.nasdaq.state_updated is True
    assert res.nasdaq.state_preserved is False
    assert res.nasdaq.outcome_status == "partial"


# --- Save True but Reload Failed Test ---

@pytest.mark.asyncio
async def test_state_staging_exception_isolated_to_bist_and_nasdaq_continues(mock_cache):
    bist_service = MagicMock()
    bist_service.get_refresh_bundle = AsyncMock(return_value=make_bist_bundle(status="success"))

    nasdaq_service = MagicMock()
    nasdaq_service.get_rolling_calendar = AsyncMock(
        return_value=Nasdaq100EarningsCalendar(
            generated_at="2026-08-13T12:00:00",
            coverage_start="2026-08-13",
            coverage_end="2026-11-11",
        )
    )

    job = EarningsRefreshJob(
        bist_service=bist_service,
        nasdaq_service=nasdaq_service,
        state_store=mock_cache,
        email_service=MagicMock(is_configured=MagicMock(return_value=False)),
    )

    with patch.object(mock_cache, "stage_bist", side_effect=OSError("staging failure")):
        result = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert result.bist.outcome_status == "failed"
    assert result.bist.sync_action == "not_written"
    assert "State staging failed: OSError" == result.bist.error
    assert result.nasdaq.outcome_status == "success"
    assert mock_cache.load_nasdaq() is not None


# --- Log Assertion Test ---

@pytest.mark.asyncio
async def test_exception_logging_no_nonetype_none(mock_cache, caplog):
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
        email_service=email_service
    )

    with caplog.at_level(logging.ERROR):
        await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert "NoneType: None" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


# --- `send_email=False` Test ---

@pytest.mark.asyncio
async def test_email_send_returns_false(mock_cache):
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
        email_service=email_service
    )
    res = await job.run(as_of=datetime(2026, 8, 13, 10, 0, 0))

    assert res.overall_status == "success"
    assert res.email_status == "failed"
    assert res.email_error == "E-posta gönderimi başarısız oldu (SMTP teslimat hatası)."
    assert exit_code_for_result(res) == 0


@pytest.mark.asyncio
async def test_services_called_with_same_as_of_datetime(mock_cache):
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
        email_service=email_service
    )
    ref_as_of = datetime(2026, 8, 13, 10, 0, 0)

    await job.run(as_of=ref_as_of)

    bist_arg = bist_service.get_refresh_bundle.call_args[1]["as_of"]
    nasdaq_arg = nasdaq_service.get_rolling_calendar.call_args[1]["as_of"]

    assert bist_arg == nasdaq_arg


# --- Production Isolation Test ---
