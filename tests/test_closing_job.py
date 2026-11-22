from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.closing_job import run_closing_job
from app.models.job_tracking import DuplicateJobRunError
from app.models.market import (
    Bist100Data,
    BistMovers,
    BitcoinData,
    CurrencyData,
    GoldData,
    MarketData,
    MarketMover,
    MarketSession,
    MarketSnapshot,
)
from app.repositories.job_tracking_repository import JobTrackingRepository
from tests.test_job_tracking_runner import StatefulFakeConnectionStore


def _build_test_snapshot(warnings=None) -> MarketSnapshot:
    return MarketSnapshot(
        success=True,
        generated_at="2026-08-29T18:30:00+03:00",
        timezone="Europe/Istanbul",
        session=MarketSession.CLOSED,
        market=MarketData(
            bist100=Bist100Data(price=10550.0, change_percent=0.85),
            usdtry=CurrencyData(price=34.18),
            eurtry=CurrencyData(price=37.55),
            gold=GoldData(xauusd=2510.0, gram_try=2760.0),
            bitcoin=BitcoinData(usd=64200.0),
        ),
        text="Closing market summary text",
        warnings=warnings or [],
    )


def _build_test_movers(warnings=None):
    movers = BistMovers(
        gainers=[MarketMover(symbol="THYAO", price=310.0, change_percent=4.5)],
        losers=[MarketMover(symbol="ASELS", price=62.0, change_percent=-2.1)],
    )
    return movers, warnings or []


@pytest.mark.asyncio
async def test_closing_job_success_lifecycle():
    """Verify closing job success runs full lifecycle, writes refresh_runs without provider outcomes, and sends email."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers())

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)), \
         patch.object(JobTrackingRepository, "upsert_provider_results") as mock_upsert_prov:

        result = await run_closing_job(
            market_service=mock_market,
            movers_service=mock_movers,
            email_service=mock_email,
            db_connection_factory=store.create_connection,
            dry_run=False,
            no_email=False,
        )

    # 1. Result verification
    assert result.status == "success"
    assert result.report_type == "closing"
    assert result.email_status == "sent"
    assert result.warnings == []
    assert result.metrics["report_type"] == "closing"
    assert result.metrics["mover_count"] == 2
    assert result.metrics["gainers_count"] == 1
    assert result.metrics["losers_count"] == 1
    assert result.metrics["warning_count"] == 0
    assert result.metrics["bist_price"] == 10550.0

    # 2. Central email verification (exactly 1 call by runner)
    assert mock_email.send_email.call_count == 1
    email_call_kwargs = mock_email.send_email.call_args.kwargs
    assert "Günün Kapanışı" in email_call_kwargs["subject"]
    assert "THYAO" in email_call_kwargs["body_text"]
    assert "ASELS" in email_call_kwargs["body_text"]

    # 3. Provider outcomes must NOT be written
    mock_upsert_prov.assert_not_called()

    # 4. DB state verification
    assert len(store.committed_runs) == 1
    run_row = next(iter(store.committed_runs.values()))
    assert run_row["job_name"] == "closing_job"
    assert run_row["status"] == "success"
    assert run_row["sync_action"] == "no_op"
    assert run_row["notification_status"] == "sent"
    assert run_row["notified_at"] is not None


@pytest.mark.asyncio
async def test_closing_job_partial_with_movers_warnings():
    """Verify non-fatal warnings set status to partial and preserve warnings in report email and DB."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot(warnings=["Gold spot delayed"]))

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers(warnings=["3 tickers unavailable on Yahoo"]))

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        result = await run_closing_job(
            market_service=mock_market,
            movers_service=mock_movers,
            email_service=mock_email,
            db_connection_factory=store.create_connection,
            dry_run=False,
            no_email=False,
        )

    assert result.status == "partial"
    assert "Gold spot delayed" in result.warnings
    assert "3 tickers unavailable on Yahoo" in result.warnings
    assert result.metrics["warning_count"] == 2
    assert result.email_status == "sent"

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "partial"
    assert run_row["sync_action"] == "partial_update"
    assert run_row["notification_status"] == "sent"
    assert len(run_row["warnings"]) == 2


@pytest.mark.asyncio
async def test_closing_job_failed_execution_sanitizes_secret_and_sends_alert():
    """Verify unhandled business exception finalizes as failed and sends generic failure notification without leaking secrets."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    secret_key = "SecretApiKeyClosingJob888"
    secret_pass = "DbPasswordClosingSecret222"

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(
        side_effect=RuntimeError(f"Database crash postgresql://user:{secret_pass}@db.prod:5432 with token={secret_key}")
    )

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers())

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        with pytest.raises(RuntimeError):
            await run_closing_job(
                market_service=mock_market,
                movers_service=mock_movers,
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
async def test_closing_job_dry_run_executes_zero_db_and_zero_emails():
    """Verify --dry-run mode executes business logic without database writes or SMTP delivery."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers())

    result = await run_closing_job(
        market_service=mock_market,
        movers_service=mock_movers,
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
async def test_closing_job_no_email_records_tracking_disabled():
    """Verify --no-email records tracking in database with notification_status=disabled and skips email."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers())

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)):

        result = await run_closing_job(
            market_service=mock_market,
            movers_service=mock_movers,
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
async def test_closing_job_email_failure_sets_notification_status_failed():
    """Verify email sending failure records notification_status as failed in DB."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = False  # SMTP failed

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers())

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        result = await run_closing_job(
            market_service=mock_market,
            movers_service=mock_movers,
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
async def test_closing_job_cli_argument_parsing():
    """Verify CLI flags --dry-run and --no-email are correctly parsed and passed to ClosingJob."""
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers())

    result = await run_closing_job(
        argv=["--dry-run", "--no-email"],
        market_service=mock_market,
        movers_service=mock_movers,
        email_service=mock_email,
        db_connection_factory=MagicMock(),
    )

    assert result.dry_run is True
    assert result.no_email is True
    assert result.email_status == "skipped_dry_run"


@pytest.mark.asyncio
async def test_closing_job_idempotency_duplicate_run_fails_closed():
    """Verify duplicate job run is rejected with DuplicateJobRunError and halts execution."""
    mock_email = MagicMock()
    mock_market = MagicMock()
    mock_movers = MagicMock()

    with patch.object(JobTrackingRepository, "start_run", side_effect=DuplicateJobRunError("Duplicate execution detected")):
        with pytest.raises(DuplicateJobRunError):
            await run_closing_job(
                market_service=mock_market,
                movers_service=mock_movers,
                email_service=mock_email,
                db_connection_factory=MagicMock(),
                dry_run=False,
                no_email=False,
            )

    mock_market.generate.assert_not_called()
    mock_email.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_closing_job_transaction_commit_verification():
    """Verify Tx A, Tx C, and Tx D are executed inside proper transaction blocks."""
    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers())

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        result = await run_closing_job(
            market_service=mock_market,
            movers_service=mock_movers,
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


def test_market_summary_adapter_closing_outcome_and_metric_hygiene():
    """Verify MarketSummaryResultAdapter cleanses email html/body from metrics and enforces empty provider outcomes for closing job."""
    from app.models.market_summary_job import MarketSummaryJobResult
    from app.services.job_tracking.adapters.market_summary_adapter import (
        MarketSummaryResultAdapter,
    )

    adapter = MarketSummaryResultAdapter()
    result = MarketSummaryJobResult(
        status="success",
        report_type="closing",
        started_at="2026-08-29T18:30:00+00:00",
        finished_at="2026-08-29T18:30:04+00:00",
        duration_seconds=4.0,
        warnings=[],
        metrics={
            "report_type": "closing",
            "mover_count": 4,
            "gainers_count": 2,
            "losers_count": 2,
            "subject": "Leaked Subject",
            "body_text": "Leaked text",
            "body_html": "<div>Leaked HTML</div>",
        },
        subject="Closing Subject",
        body_text="Closing text",
        body_html="<p>Closing html</p>",
    )

    outcome = adapter.to_outcome(result)
    assert outcome.status == "success"
    assert outcome.sync_action == "no_op"
    assert outcome.provider_outcomes == []
    assert outcome.warnings == []
    assert "subject" not in outcome.metrics
    assert "body_text" not in outcome.metrics
    assert "body_html" not in outcome.metrics
    assert outcome.metrics["report_type"] == "closing"
    assert outcome.metrics["mover_count"] == 4


def test_market_summary_email_renderer_closing():
    """Verify MarketSummaryEmailRenderer returns correct subject, text, and html for closing job."""
    from app.models.market_summary_job import MarketSummaryJobResult
    from app.services.job_tracking.notification.renderer import (
        MarketSummaryEmailRenderer,
    )

    renderer = MarketSummaryEmailRenderer()
    result = MarketSummaryJobResult(
        status="success",
        report_type="closing",
        started_at="2026-08-29T18:30:00+00:00",
        subject="Custom Closing Subject",
        body_text="Custom Closing Body",
        body_html="<p>Custom Closing HTML</p>",
    )

    subj, text, html_body = renderer.render(result)
    assert subj == "Custom Closing Subject"
    assert text == "Custom Closing Body"
    assert html_body == "<p>Custom Closing HTML</p>"

    # Test fallback rendering when subject and body are None
    fallback_result = MarketSummaryJobResult(
        status="failed",
        report_type="closing",
        started_at="2026-08-29T18:30:00+00:00",
        error="Closing Pipeline Failed",
    )
    f_subj, f_text, f_html = renderer.render(fallback_result)
    assert "[FinBot] Kapanış Piyasa Özeti — FAILED" in f_subj
    assert "Closing Pipeline Failed" in f_text
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
def test_closing_job_cli_exit_code_matrix(status, email_status, dry_run, no_email, expected_code):
    """Verify deterministic CLI exit code rules across all execution matrix cases for closing job."""
    from app.models.market_summary_job import (
        MarketSummaryJobResult,
        determine_cli_exit_code,
    )

    result = MarketSummaryJobResult(
        status=status,
        report_type="closing",
        started_at="2026-08-29T18:30:00+00:00",
        email_status=email_status,
        dry_run=dry_run,
        no_email=no_email,
    )
    assert determine_cli_exit_code(result) == expected_code


@pytest.mark.asyncio
async def test_closing_job_email_not_configured_records_tracking_and_exits_nonzero():
    """Verify when email is not configured: no SMTP call, notification_status=not_configured, email_status=skipped_not_configured, CLI exit=1."""
    from app.models.market_summary_job import determine_cli_exit_code

    store = StatefulFakeConnectionStore()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = False  # Email not configured

    mock_market = MagicMock()
    mock_market.generate = AsyncMock(return_value=_build_test_snapshot())

    mock_movers = MagicMock()
    mock_movers.get_movers = AsyncMock(return_value=_build_test_movers())

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        result = await run_closing_job(
            market_service=mock_market,
            movers_service=mock_movers,
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


