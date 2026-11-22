from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.jobs.morning_job import run_morning_job
from app.models.earnings import WeeklyEarningsEvent
from app.models.job_tracking import DuplicateJobRunError
from app.models.market import (
    Bist100Data,
    BitcoinData,
    CurrencyData,
    EconomicEvent,
    GoldData,
    MarketData,
    MarketSession,
    MarketSnapshot,
)
from app.repositories.job_tracking_repository import JobTrackingRepository
from tests.test_job_tracking_runner import StatefulFakeConnectionStore


def _build_test_snapshot(warnings=None) -> MarketSnapshot:
    return MarketSnapshot(
        success=True,
        generated_at="2026-08-29T10:25:00+03:00",
        timezone="Europe/Istanbul",
        session=MarketSession.INTRADAY,
        market=MarketData(
            bist100=Bist100Data(price=10500.0, change_percent=1.2),
            usdtry=CurrencyData(price=34.15),
            eurtry=CurrencyData(price=37.50),
            gold=GoldData(xauusd=2500.0, gram_try=2750.0),
            bitcoin=BitcoinData(usd=64000.0),
        ),
        text="Morning market summary text",
        warnings=warnings or [],
    )


@pytest.mark.asyncio
async def test_morning_job_success_lifecycle():
    """Verify morning job success runs full lifecycle, writes refresh_runs without provider outcomes, and sends email."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    today = datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()
    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = (
        [EconomicEvent(name="TCMB PPK", country="TR", scheduled_at=f"{today}T14:00:00+03:00", scheduled_at_istanbul=f"{today}T14:00:00+03:00", source="TCMB")],
        [WeeklyEarningsEvent(symbol="THYAO", company_name="THY", market="BIST100", earnings_date=today)],
        [],
        "database",
    )

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)), \
         patch.object(JobTrackingRepository, "upsert_provider_results") as mock_upsert_prov:

        result = await run_morning_job(
            market_service=mock_market,
            calendar_service=mock_calendar,
            email_service=mock_email,
            db_connection_factory=store.create_connection,
            dry_run=False,
            no_email=False,
        )

    # 1. Result verification
    assert result.status == "success"
    assert result.report_type == "morning"
    assert result.email_status == "sent"
    assert result.warnings == []
    assert result.metrics["report_type"] == "morning"
    assert result.metrics["calendar_source"] == "database"
    assert result.metrics["economic_event_count"] == 1
    assert result.metrics["earnings_event_count"] == 1
    assert result.metrics["warning_count"] == 0
    assert result.metrics["bist_price"] == 10500.0

    # 2. Central email verification (exactly 1 call by runner)
    assert mock_email.send_email.call_count == 1
    email_call_kwargs = mock_email.send_email.call_args.kwargs
    assert "Güne Başlarken" in email_call_kwargs["subject"]
    assert "TCMB PPK" in email_call_kwargs["body_text"]
    assert "THYAO" in email_call_kwargs["body_text"]

    # 3. Provider outcomes must NOT be written
    mock_upsert_prov.assert_not_called()

    # 4. DB state verification
    assert len(store.committed_runs) == 1
    run_row = next(iter(store.committed_runs.values()))
    assert run_row["job_name"] == "morning_job"
    assert run_row["status"] == "success"
    assert run_row["sync_action"] == "no_op"
    assert run_row["notification_status"] == "sent"
    assert run_row["notified_at"] is not None


@pytest.mark.asyncio
async def test_morning_job_partial_with_calendar_and_snapshot_warnings():
    """Verify non-fatal warnings set status to partial and preserve warnings in report email and DB."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot(warnings=["USDTRY spot delayed"]))

    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = (
        [],
        [],
        ["Could not fetch weekly earnings calendar"],
        "database",
    )

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        result = await run_morning_job(
            market_service=mock_market,
            calendar_service=mock_calendar,
            email_service=mock_email,
            db_connection_factory=store.create_connection,
            dry_run=False,
            no_email=False,
        )

    assert result.status == "partial"
    assert "USDTRY spot delayed" in result.warnings
    assert "Could not fetch weekly earnings calendar" in result.warnings
    assert result.metrics["warning_count"] == 2
    assert result.email_status == "sent"

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "partial"
    assert run_row["sync_action"] == "partial_update"
    assert run_row["notification_status"] == "sent"
    assert len(run_row["warnings"]) == 2


@pytest.mark.asyncio
async def test_morning_job_failed_execution_sanitizes_secret_and_sends_alert():
    """Verify unhandled business exception finalizes as failed and sends generic failure notification without leaking secrets."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    secret_key = "SecretApiKeyMorningJob999"
    secret_pass = "DbPasswordMorningSecret111"

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(
        side_effect=RuntimeError(f"Database crash postgresql://user:{secret_pass}@db.prod:5432 with token={secret_key}")
    )

    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = ([], [], [], "database")

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        with pytest.raises(RuntimeError):
            await run_morning_job(
                market_service=mock_market,
                calendar_service=mock_calendar,
                email_service=mock_email,
                db_connection_factory=store.create_connection,
                dry_run=False,
                no_email=False,
            )

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "failed"
    assert run_row["sync_action"] == "failed_no_write"
    assert run_row["notification_status"] == "sent"

    # Verify secret masking
    assert secret_key not in str(run_row)
    assert secret_pass not in str(run_row)

    assert mock_email.send_email.call_count == 1
    call_args = mock_email.send_email.call_args.kwargs
    assert secret_key not in call_args["subject"]
    assert secret_key not in call_args["body_text"]
    assert secret_pass not in call_args["subject"]
    assert secret_pass not in call_args["body_text"]


@pytest.mark.asyncio
async def test_morning_job_dry_run_executes_zero_db_and_zero_emails():
    """Verify --dry-run mode executes business logic without database writes or SMTP delivery."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = ([], [], [], "database")

    result = await run_morning_job(
        market_service=mock_market,
        calendar_service=mock_calendar,
        email_service=mock_email,
        db_connection_factory=store.create_connection,
        dry_run=True,
        no_email=False,
    )

    assert result.status == "success"
    assert result.email_status == "skipped_dry_run"
    assert result.dry_run is True
    assert len(store.committed_runs) == 0
    mock_email.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_morning_job_no_email_records_tracking_disabled():
    """Verify --no-email records tracking in database with notification_status=disabled and skips email."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = ([], [], [], "database")

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)):

        result = await run_morning_job(
            market_service=mock_market,
            calendar_service=mock_calendar,
            email_service=mock_email,
            db_connection_factory=store.create_connection,
            dry_run=False,
            no_email=True,
        )

    assert result.status == "success"
    assert result.email_status == "skipped_no_email"
    mock_email.send_email.assert_not_called()

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "success"
    assert run_row["notification_status"] == "disabled"


@pytest.mark.asyncio
async def test_morning_job_email_failure_sets_notification_status_failed():
    """Verify email sending failure records notification_status as failed in DB."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = False  # SMTP failed

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = ([], [], [], "database")

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        result = await run_morning_job(
            market_service=mock_market,
            calendar_service=mock_calendar,
            email_service=mock_email,
            db_connection_factory=store.create_connection,
            dry_run=False,
            no_email=False,
        )

    assert result.status == "success"
    assert result.email_status == "failed"
    assert result.email_error is not None

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["notification_status"] == "failed"
    assert run_row["notification_error"] is not None


@pytest.mark.asyncio
async def test_morning_job_cli_argument_parsing():
    """Verify CLI flags --dry-run and --no-email are correctly parsed and passed to MorningJob."""
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = ([], [], [], "database")

    result = await run_morning_job(
        argv=["--dry-run", "--no-email"],
        market_service=mock_market,
        calendar_service=mock_calendar,
        email_service=mock_email,
        db_connection_factory=MagicMock(),
    )

    assert result.dry_run is True
    assert result.no_email is True
    assert result.email_status == "skipped_dry_run"


@pytest.mark.asyncio
async def test_morning_job_idempotency_duplicate_run_fails_closed():
    """Verify duplicate job run is rejected with DuplicateJobRunError and halts execution."""
    mock_email = MagicMock()
    mock_market = MagicMock()
    mock_calendar = MagicMock()

    with patch.object(JobTrackingRepository, "start_run", side_effect=DuplicateJobRunError("Duplicate execution detected")):
        with pytest.raises(DuplicateJobRunError):
            await run_morning_job(
                market_service=mock_market,
                calendar_service=mock_calendar,
                email_service=mock_email,
                db_connection_factory=MagicMock(),
                dry_run=False,
                no_email=False,
            )

    mock_market.generate.assert_not_called()
    mock_email.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_morning_job_transaction_commit_verification():
    """Verify Tx A, Tx C, and Tx D are executed inside proper transaction blocks."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = ([], [], [], "database")

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        result = await run_morning_job(
            market_service=mock_market,
            calendar_service=mock_calendar,
            email_service=mock_email,
            db_connection_factory=store.create_connection,
            dry_run=False,
            no_email=False,
        )

    assert result.status == "success"
    # Ensure connections were used with active transactions that committed
    assert store.connections_opened >= 2
    assert store.transactions_committed >= 2
    assert store.transactions_rolled_back == 0
    assert len(store.committed_runs) == 1


def test_market_summary_adapter_morning_outcome_and_metric_hygiene():
    """Verify MarketSummaryResultAdapter cleanses email html/body from metrics and enforces empty provider outcomes."""
    from app.models.market_summary_job import MarketSummaryJobResult
    from app.services.job_tracking.adapters.market_summary_adapter import (
        MarketSummaryResultAdapter,
    )

    adapter = MarketSummaryResultAdapter()
    result = MarketSummaryJobResult(
        status="success",
        report_type="morning",
        started_at="2026-08-29T10:00:00+00:00",
        finished_at="2026-08-29T10:00:05+00:00",
        duration_seconds=5.0,
        warnings=["Non-critical warning"],
        metrics={
            "report_type": "morning",
            "calendar_source": "database",
            "subject": "Leaked Subject",
            "body_text": "Leaked text",
            "body_html": "<div>Leaked HTML</div>",
            "secret_token": "SecretTok12345",
        },
        subject="Email Subject",
        body_text="Email text",
        body_html="<p>Email html</p>",
    )

    outcome = adapter.to_outcome(result)
    assert outcome.status == "success"
    assert outcome.sync_action == "no_op"
    assert outcome.provider_outcomes == []
    assert outcome.warnings == ["Non-critical warning"]
    assert "subject" not in outcome.metrics
    assert "body_text" not in outcome.metrics
    assert "body_html" not in outcome.metrics
    assert outcome.metrics["secret_token"] == "***MASKED***"
    assert outcome.metrics["report_type"] == "morning"
    assert outcome.metrics["warning_count"] == 1


def test_market_summary_email_renderer_morning():
    """Verify MarketSummaryEmailRenderer returns correct subject, text, and html."""
    from app.models.market_summary_job import MarketSummaryJobResult
    from app.services.job_tracking.notification.renderer import (
        MarketSummaryEmailRenderer,
    )

    renderer = MarketSummaryEmailRenderer()
    result = MarketSummaryJobResult(
        status="success",
        report_type="morning",
        started_at="2026-08-29T10:00:00+00:00",
        subject="Custom Morning Subject",
        body_text="Custom Morning Body",
        body_html="<p>Custom HTML</p>",
    )

    subj, text, html_body = renderer.render(result)
    assert subj == "Custom Morning Subject"
    assert text == "Custom Morning Body"
    assert html_body == "<p>Custom HTML</p>"

    # Test fallback rendering when subject and body are None
    fallback_result = MarketSummaryJobResult(
        status="failed",
        report_type="morning",
        started_at="2026-08-29T10:00:00+00:00",
        error="Critical Failure",
    )
    f_subj, f_text, f_html = renderer.render(fallback_result)
    assert "[FinBot] Sabah Piyasa Özeti — FAILED" in f_subj
    assert "Critical Failure" in f_text
    assert f_html is None


@pytest.mark.parametrize(
    "status, email_status, dry_run, no_email, expected_code",
    [
        ("success", "sent", False, False, 0),
        ("partial", "sent", False, False, 0),
        ("success", "failed", False, False, 1),
        ("success", "skipped_not_configured", False, False, 1),
        ("success", None, False, False, 1),
        ("failed", "sent", False, False, 1),
        ("success", "skipped_dry_run", True, False, 0),
        ("partial", "skipped_dry_run", True, False, 0),
        ("success", "skipped_no_email", False, True, 0),
        ("partial", "skipped_no_email", False, True, 0),
        ("failed", "skipped_dry_run", True, False, 1),
        ("failed", "skipped_no_email", False, True, 1),
    ],
)
def test_morning_job_cli_exit_code_matrix(status, email_status, dry_run, no_email, expected_code):
    """Verify deterministic CLI exit code rules across all execution matrix cases."""
    from app.models.market_summary_job import (
        MarketSummaryJobResult,
        determine_cli_exit_code,
    )

    result = MarketSummaryJobResult(
        status=status,
        report_type="morning",
        started_at="2026-08-29T10:00:00+00:00",
        email_status=email_status,
        dry_run=dry_run,
        no_email=no_email,
    )
    assert determine_cli_exit_code(result) == expected_code


@pytest.mark.asyncio
async def test_morning_job_email_not_configured_records_tracking_and_exits_nonzero():
    """Verify when email is not configured: no SMTP call, notification_status=not_configured, email_status=skipped_not_configured, CLI exit=1."""
    from app.models.market_summary_job import determine_cli_exit_code

    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = False  # Email not configured

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_calendar = MagicMock()
    mock_calendar.get_weekly_calendar.return_value = ([], [], [], "database")

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        result = await run_morning_job(
            market_service=mock_market,
            calendar_service=mock_calendar,
            email_service=mock_email,
            db_connection_factory=store.create_connection,
            dry_run=False,
            no_email=False,
        )

    assert result.status == "success"
    assert result.email_status == "skipped_not_configured"
    mock_email.send_email.assert_not_called()

    # DB notification status must be not_configured
    run_row = next(iter(store.committed_runs.values()))
    assert run_row["notification_status"] == "not_configured"

    # Production CLI exit decision must be 1 (non-zero fail-safe)
    assert determine_cli_exit_code(result) == 1

