from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from app.models.job_tracking import (
    DuplicateJobRunError,
    JobRunContext,
    JobRunOutcome,
    JobRunSpecification,
    JobTrackingFinalizeError,
    JobTrackingNotificationError,
    JobTrackingStartError,
    ProviderRunOutcome,
)
from app.repositories.job_tracking_repository import JobTrackingRepository
from app.services.job_tracking.adapters.base import JobResultAdapter
from app.services.job_tracking.notification.policy import (
    JobNotificationRule,
    NotificationPolicy,
)
from app.services.job_tracking.notification.renderer import JobEmailRenderer
from app.services.job_tracking.runner import TrackedJobRunner
from tests.conftest import create_mock_db_connection


class DummyResult(BaseModel):
    status: str
    count: int = 10
    email_status: str | None = None
    email_error: str | None = None


class DummyAdapter(JobResultAdapter[DummyResult]):
    def to_outcome(self, result: DummyResult) -> JobRunOutcome:
        return JobRunOutcome(
            status=result.status,
            sync_action="updated" if result.status == "success" else "failed_no_write",
            metrics={"count": result.count},
        )


class DummyRenderer(JobEmailRenderer[DummyResult]):
    def render(self, result: DummyResult):
        return ("Subject: Dummy Alert", f"Body Text: {result.status}", f"<p>Body HTML: {result.status}</p>")


def test_dry_run_executes_zero_db_writes_and_zero_emails():
    mock_conn_factory = MagicMock()
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    spec = JobRunSpecification(
        job_name="dummy_job",
        operation_type="prices_sync",
        scope="TEST",
    )
    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=mock_email,
        db_connection_factory=mock_conn_factory,
    )

    executed = False

    def business_fn():
        nonlocal executed
        executed = True
        return DummyResult(status="success", count=42)

    res = runner.run(business_fn, dry_run=True, no_email=False)

    assert executed is True
    assert res.count == 42
    assert res.email_status == "skipped_dry_run"
    mock_conn_factory.assert_not_called()
    mock_email.send_email.assert_not_called()


def test_successful_run_flow_with_isolated_transactions():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    run_id = uuid4()
    mock_cur.fetchone.return_value = (str(run_id),)
    mock_cur.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn_factory = MagicMock()
    mock_conn_factory.return_value.__enter__.return_value = mock_conn

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    spec = JobRunSpecification(
        job_name="dummy_job",
        operation_type="prices_sync",
        scope="TEST",
        provider_code="YAHOO_FINANCE",
    )
    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=mock_email,
        db_connection_factory=mock_conn_factory,
    )

    with patch.object(JobTrackingRepository, "get_provider_id_map", return_value={"YAHOO_FINANCE": uuid4()}):
        res = runner.run(lambda: DummyResult(status="success", count=100), dry_run=False)

    assert res.status == "success"
    assert res.email_status is None
    mock_email.send_email.assert_not_called()


def test_partial_run_sends_alert_and_records_sent_status():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    run_id = uuid4()
    mock_cur.fetchone.return_value = (str(run_id),)
    mock_cur.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn_factory = MagicMock()
    mock_conn_factory.return_value.__enter__.return_value = mock_conn

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    spec = JobRunSpecification(
        job_name="dummy_job",
        operation_type="prices_sync",
        scope="TEST",
        provider_code="YAHOO_FINANCE",
    )
    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=mock_email,
        db_connection_factory=mock_conn_factory,
    )

    with patch.object(JobTrackingRepository, "get_provider_id_map", return_value={"YAHOO_FINANCE": uuid4()}):
        res = runner.run(lambda: DummyResult(status="partial", count=80), dry_run=False)

    assert res.status == "partial"
    assert res.email_status == "sent"
    mock_email.send_email.assert_called_once()


def test_unhandled_exception_recorded_as_failed_in_tracking():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    run_id = uuid4()
    mock_cur.fetchone.return_value = (str(run_id),)
    mock_cur.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn_factory = MagicMock()
    mock_conn_factory.return_value.__enter__.return_value = mock_conn

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    spec = JobRunSpecification(
        job_name="dummy_job",
        operation_type="prices_sync",
        scope="TEST",
    )
    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=mock_email,
        db_connection_factory=mock_conn_factory,
    )

    def failing_fn():
        raise RuntimeError("Fatal database crash with SecretPass=123")

    with patch.object(JobTrackingRepository, "get_provider_id_map", return_value={}):
        with pytest.raises(RuntimeError) as exc_info:
            runner.run(failing_fn, dry_run=False)

    assert "Fatal database crash" in str(exc_info.value)


def test_notification_policy_matrix():
    # 1. Alert on partial or failed
    rule = JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED

    # Success -> not required
    d1 = NotificationPolicy.evaluate(rule=rule, status="success", dry_run=False, no_email=False, is_email_configured=True)
    assert d1.should_send is False
    assert d1.initial_notification_status == "not_required"

    # Partial -> should send
    d2 = NotificationPolicy.evaluate(rule=rule, status="partial", dry_run=False, no_email=False, is_email_configured=True)
    assert d2.should_send is True

    # Failed -> should send
    d3 = NotificationPolicy.evaluate(rule=rule, status="failed", dry_run=False, no_email=False, is_email_configured=True)
    assert d3.should_send is True

    # Skipped -> not required
    d4 = NotificationPolicy.evaluate(rule=rule, status="skipped", dry_run=False, no_email=False, is_email_configured=True)
    assert d4.should_send is False
    assert d4.initial_notification_status == "not_required"

    # --no-email -> disabled
    d5 = NotificationPolicy.evaluate(rule=rule, status="failed", dry_run=False, no_email=True, is_email_configured=True)
    assert d5.should_send is False
    assert d5.initial_notification_status == "disabled"

    # Not configured -> not_configured
    d6 = NotificationPolicy.evaluate(rule=rule, status="failed", dry_run=False, no_email=False, is_email_configured=False)
    assert d6.should_send is False
    assert d6.initial_notification_status == "not_configured"

    # 2. Always on finish
    always_rule = JobNotificationRule.ALWAYS_ON_FINISH

    # Success -> should send
    d7 = NotificationPolicy.evaluate(rule=always_rule, status="success", dry_run=False, no_email=False, is_email_configured=True)
    assert d7.should_send is True

    # Dry run -> skipped_dry_run
    d8 = NotificationPolicy.evaluate(rule=always_rule, status="success", dry_run=True, no_email=False, is_email_configured=True)
    assert d8.should_send is False
    assert d8.initial_notification_status == "skipped_dry_run"


def test_runner_fails_closed_on_start_error_and_does_not_execute_business_fn():
    from app.models.job_tracking import JobTrackingStartError

    mock_conn = MagicMock()
    mock_conn_factory = MagicMock()
    mock_conn_factory.return_value.__enter__.return_value = mock_conn
    mock_email = MagicMock()

    spec = JobRunSpecification(
        job_name="dummy_job",
        operation_type="prices_sync",
        scope="TEST",
    )
    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=mock_email,
        db_connection_factory=mock_conn_factory,
    )

    executed = False

    def business_fn():
        nonlocal executed
        executed = True
        return DummyResult(status="success")

    with patch.object(JobTrackingRepository, "start_run", side_effect=JobTrackingStartError("Tx A DB Connection Died")):
        with pytest.raises(JobTrackingStartError) as exc_info:
            runner.run(business_fn, dry_run=False)

    assert "Tx A DB Connection Died" in str(exc_info.value) or "dummy_job" in str(exc_info.value)
    assert executed is False
    mock_email.send_email.assert_not_called()


def test_runner_aborts_safely_on_duplicate_run_error():
    from app.models.job_tracking import DuplicateJobRunError

    mock_conn = MagicMock()
    mock_conn_factory = MagicMock()
    mock_conn_factory.return_value.__enter__.return_value = mock_conn
    mock_email = MagicMock()

    spec = JobRunSpecification(
        job_name="dummy_job",
        operation_type="prices_sync",
        scope="TEST",
    )
    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=mock_email,
        db_connection_factory=mock_conn_factory,
    )

    executed = False

    def business_fn():
        nonlocal executed
        executed = True
        return DummyResult(status="success")

    with patch.object(JobTrackingRepository, "start_run", side_effect=DuplicateJobRunError("Duplicate run detected")):
        with pytest.raises(DuplicateJobRunError):
            runner.run(business_fn, dry_run=False)

    assert executed is False
    mock_email.send_email.assert_not_called()


def test_runner_fails_closed_on_finalize_failure():
    from app.models.job_tracking import JobTrackingFinalizeError

    mock_conn = MagicMock()
    mock_conn_factory = MagicMock()
    mock_conn_factory.return_value.__enter__.return_value = mock_conn
    mock_email = MagicMock()

    spec = JobRunSpecification(
        job_name="dummy_job",
        operation_type="prices_sync",
        scope="TEST",
    )
    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        email_service=mock_email,
        db_connection_factory=mock_conn_factory,
    )

    with patch.object(JobTrackingRepository, "start_run", return_value=uuid4()), \
         patch.object(JobTrackingRepository, "finalize_run", return_value=False):
        with pytest.raises(JobTrackingFinalizeError) as exc_info:
            runner.run(lambda: DummyResult(status="success"), dry_run=False)

    assert "dummy_job" in str(exc_info.value)


def test_portfolio_snapshot_adapter_rules():
    from app.models.portfolio_snapshots import PortfolioSnapshotJobResult
    from app.services.job_tracking.adapters.portfolio_snapshot_adapter import (
        PortfolioSnapshotResultAdapter,
    )

    adapter = PortfolioSnapshotResultAdapter()
    result = PortfolioSnapshotJobResult(
        status="success",
        snapshot_date="2026-08-28",
        dry_run=False,
        requested_count=5,
        completed_count=5,
        partial_count=0,
        failed_count=0,
        started_at="2026-08-28T01:00:00+00:00",
        finished_at="2026-08-28T01:01:00+00:00",
        duration_seconds=60.0,
    )

    outcome = adapter.to_outcome(result)
    assert outcome.status == "success"
    assert outcome.sync_action == "updated"
    assert outcome.provider_outcomes == []  # No FINBOT provider outcomes


def test_economic_adapter_effective_status_failed_on_database_error():
    from app.models.economic_calendar_refresh import (
        EconomicCalendarRefreshJobResult,
        EconomicEventDiff,
    )
    from app.services.job_tracking.adapters.economic_calendar_adapter import (
        EconomicCalendarRefreshResultAdapter,
    )

    adapter = EconomicCalendarRefreshResultAdapter()
    result = EconomicCalendarRefreshJobResult(
        generated_at="2026-08-28T01:00:00+00:00",
        coverage_start="2026-08-28",
        coverage_end="2026-11-26",
        state_source="state_store/economic_calendar.json",
        started_at="2026-08-28T01:00:00+00:00",
        finished_at="2026-08-28T01:01:00+00:00",
        duration_seconds=60.0,
        status="success",
        sync_action="replaced",
        diff=EconomicEventDiff(),
        database_status="failed",
        database_error="Connection to postgresql://user:SecretPass@host:5432/db failed",
    )

    outcome = adapter.to_outcome(result)
    assert outcome.status == "failed"
    assert outcome.sync_action == "failed_no_write"
    assert "SecretPass" not in str(outcome.error)
    assert "Veritabanı veya servis erişim hatası." in str(outcome.error)


def test_earnings_adapter_effective_status_failed_on_database_error():
    from app.models.earnings_refresh import (
        EarningsRefreshJobResult,
        MarketRefreshOutcome,
    )
    from app.services.job_tracking.adapters.earnings_adapter import (
        EarningsRefreshResultAdapter,
    )

    adapter = EarningsRefreshResultAdapter()
    result = EarningsRefreshJobResult(
        started_at="2026-08-28T01:00:00+00:00",
        finished_at="2026-08-28T01:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-28",
        coverage_end="2026-11-26",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-28",
            coverage_end="2026-11-26",
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-28",
            coverage_end="2026-11-26",
        ),
        database_status="failed",
        database_error="Database write failed with token=SecretToken123",
    )

    outcome = adapter.to_outcome(result)
    assert outcome.status == "failed"
    assert outcome.sync_action == "failed_no_write"
    assert "SecretToken123" not in str(outcome.error)
    assert "[REDACTED]" in str(outcome.error)


def test_business_fn_typeerror_not_retried_and_strictly_called_once():
    """Verify that if business fn raises TypeError internally, it is called exactly ONCE and not retried with 0 args."""
    spec = JobRunSpecification(job_name="bist_latest_prices", operation_type="prices_sync", scope="BIST100", provider_code="YAHOO_FINANCE")
    mock_email = MagicMock()
    mock_email.is_configured.return_value = False

    call_count = 0

    def faulty_business_fn(ctx):
        nonlocal call_count
        call_count += 1
        # TypeError raised from internal logic (not signature mismatch)
        raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=create_mock_db_connection,
    )

    with pytest.raises(TypeError, match="unsupported operand type"):
        runner.run(faulty_business_fn, dry_run=True)

    assert call_count == 1  # Strictly 1 invocation


def test_business_fn_signature_inspection_zero_and_kwargs_args():
    """Verify runner properly invokes business functions taking 0 args, 1 arg, or *args/**kwargs."""
    spec = JobRunSpecification(job_name="bist_latest_prices", operation_type="prices_sync", scope="BIST100", provider_code="YAHOO_FINANCE")
    mock_email = MagicMock()
    mock_email.is_configured.return_value = False

    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=create_mock_db_connection,
    )

    # 1. 0-arg function
    zero_called = False
    def zero_arg_fn():
        nonlocal zero_called
        zero_called = True
        return DummyResult(status="success", count=100)

    runner.run(zero_arg_fn, dry_run=True)
    assert zero_called is True

    # 2. **kwargs function
    kwargs_called = False
    def kwargs_fn(**kwargs):
        nonlocal kwargs_called
        kwargs_called = True
        return DummyResult(status="success", count=100)

    runner.run(kwargs_fn, dry_run=True)
    assert kwargs_called is True


def test_lifecycle_error_boundary_adapter_failure_finalizes_as_failed():
    """Verify that if adapter.to_outcome raises an exception, the run is finalized as failed and does not remain running."""
    spec = JobRunSpecification(job_name="bist_latest_prices", operation_type="prices_sync", scope="BIST100", provider_code="YAHOO_FINANCE")
    mock_email = MagicMock()
    mock_email.is_configured.return_value = False

    class BrokenAdapter(JobResultAdapter):
        def to_outcome(self, result):
            raise ValueError("Adapter exploded parsing result")

    runner = TrackedJobRunner(
        spec=spec,
        adapter=BrokenAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=create_mock_db_connection,
    )

    with patch.object(JobTrackingRepository, "finalize_run", return_value=True) as mock_finalize, \
         patch.object(JobTrackingRepository, "update_notification_status", return_value=True):
        with pytest.raises(JobTrackingFinalizeError, match="Adapter failed to convert result"):
            runner.run(lambda: DummyResult(status="success", count=100), dry_run=False)

        assert mock_finalize.called
        outcome = mock_finalize.call_args.kwargs["outcome"]
        assert outcome.status == "failed"
        assert "Adapter exception: ValueError" in outcome.warnings


def test_lifecycle_error_boundary_notification_policy_failure():
    """Verify that if NotificationPolicy.evaluate raises an exception, notification_status is recorded as 'failed' in finalize_run and JobTrackingNotificationError is raised."""
    spec = JobRunSpecification(job_name="bist_latest_prices", operation_type="prices_sync", scope="BIST100", provider_code="YAHOO_FINANCE")
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=create_mock_db_connection,
    )

    with patch.object(NotificationPolicy, "evaluate", side_effect=RuntimeError("Notification policy evaluation crashed")), \
         patch.object(JobTrackingRepository, "finalize_run") as mock_finalize:
        mock_finalize.return_value = True
        with pytest.raises(JobTrackingNotificationError, match="Notification policy evaluation failed"):
            runner.run(lambda: DummyResult(status="success", count=100), dry_run=False)

        assert mock_finalize.called
        notif_status = mock_finalize.call_args.kwargs["notification_status"]
        assert notif_status == "failed"


def test_notification_persistence_failure_raises_job_tracking_notification_error():
    """Verify update_notification_status failure raises JobTrackingNotificationError causing non-zero exit."""
    spec = JobRunSpecification(job_name="bist_latest_prices", operation_type="prices_sync", scope="BIST100", provider_code="YAHOO_FINANCE")
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=create_mock_db_connection,
    )

    # Simulate update_notification_status returning False (e.g. rowcount == 0)
    with patch.object(JobTrackingRepository, "update_notification_status", return_value=False):
        with pytest.raises(JobTrackingNotificationError, match="Failed to record notification status"):
            runner.run(
                lambda: DummyResult(status="partial", count=85),
                dry_run=False,
            )


def test_end_to_end_secret_sanitization_no_leak_in_logs_sql_email(caplog):
    """Verify that secrets, DSNs, and tokens are never leaked into caplog, SQL params, or emails."""
    spec = JobRunSpecification(job_name="test_job", operation_type="prices_sync", scope="BIST100", provider_code="YAHOO_FINANCE")
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    secret_dsn = "postgresql://myuser:SuperSecretPassword123@db.supabase.co:5432/finbot"
    secret_token = "ghp_" + "VerySecretGitHubPersonalAccessToken999"

    class LeakyAdapter(JobResultAdapter):
        def to_outcome(self, result):
            return JobRunOutcome(
                status="failed",
                sync_action="failed_no_write",
                error=f"Connection failed: {secret_dsn}",
                warnings=[f"Auth failed with token: {secret_token}"],
                metrics={"leaky_key": secret_token, "db_url": secret_dsn},
            )

    runner = TrackedJobRunner(
        spec=spec,
        adapter=LeakyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALWAYS_ON_FINISH,
        db_connection_factory=create_mock_db_connection,
    )

    with patch.object(JobTrackingRepository, "finalize_run") as mock_finalize, \
         patch.object(JobTrackingRepository, "update_notification_status", return_value=True):
        mock_finalize.return_value = True
        with caplog.at_level("DEBUG"):
            runner.run(lambda: DummyResult(status="failed", count=0), dry_run=False)

        # 1. Verify caplog has no secrets
        for record in caplog.records:
            assert "SuperSecretPassword123" not in record.message
            assert "SuperSecretPassword123" not in str(record.exc_info)
            assert secret_token not in record.message

        # 2. Verify finalize_run received sanitized outcome
        assert mock_finalize.called
        outcome = mock_finalize.call_args.kwargs["outcome"]
        assert "SuperSecretPassword123" not in str(outcome.error)
        assert "SuperSecretPassword123" not in str(outcome.warnings)
        assert "SuperSecretPassword123" not in str(outcome.metrics)
        assert secret_token not in str(outcome.warnings)
        assert secret_token not in str(outcome.metrics)

        # 3. Verify sent email has no secrets
        assert mock_email.send_email.called
        sent_subject = mock_email.send_email.call_args.kwargs["subject"]
        sent_body = mock_email.send_email.call_args.kwargs["body_text"]
        assert "SuperSecretPassword123" not in sent_subject
        assert "SuperSecretPassword123" not in sent_body
        assert secret_token not in sent_subject
        assert secret_token not in sent_body


@pytest.mark.asyncio
async def test_adapter_failure_sync_and_async_raises_job_tracking_finalize_error():
    """Verify that when adapter.to_outcome fails, JobTrackingFinalizeError is raised for sync and async, generic email sent, and success result is never returned."""
    spec = JobRunSpecification(job_name="bist_latest_prices", operation_type="prices_sync", scope="BIST100", provider_code="YAHOO_FINANCE")
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    class CrashingAdapter(JobResultAdapter):
        def to_outcome(self, result):
            raise RuntimeError("Adapter crashing error")

    mock_renderer = MagicMock(spec=DummyRenderer)

    runner = TrackedJobRunner(
        spec=spec,
        adapter=CrashingAdapter(),
        renderer=mock_renderer,
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=create_mock_db_connection,
    )

    business_fn_calls = 0
    def business_fn():
        nonlocal business_fn_calls
        business_fn_calls += 1
        return DummyResult(status="success", count=100)

    # 1. Sync runner raises JobTrackingFinalizeError
    with patch.object(JobTrackingRepository, "finalize_run", return_value=True) as mock_finalize, \
         patch.object(JobTrackingRepository, "update_notification_status", return_value=True):
        with pytest.raises(JobTrackingFinalizeError, match="Adapter failed to convert result"):
            runner.run(business_fn, dry_run=False)

        assert business_fn_calls == 1
        assert not mock_renderer.render.called
        assert mock_finalize.called
        outcome = mock_finalize.call_args.kwargs["outcome"]
        assert outcome.status == "failed"
        assert mock_email.send_email.called

    # 2. Async runner raises JobTrackingFinalizeError
    async_business_calls = 0
    async def async_business_fn():
        nonlocal async_business_calls
        async_business_calls += 1
        return DummyResult(status="success", count=100)

    mock_email.reset_mock()
    mock_renderer.reset_mock()
    with patch.object(JobTrackingRepository, "finalize_run", return_value=True) as mock_finalize_async, \
         patch.object(JobTrackingRepository, "update_notification_status", return_value=True):
        with pytest.raises(JobTrackingFinalizeError, match="Adapter failed to convert result"):
            await runner.run_async(async_business_fn, dry_run=False)

        assert async_business_calls == 1
        assert not mock_renderer.render.called
        assert mock_finalize_async.called
        outcome = mock_finalize_async.call_args.kwargs["outcome"]
        assert outcome.status == "failed"
        assert mock_email.send_email.called


class FakePsycopgTransaction:
    """Transaction context manager representing real Psycopg with conn.transaction() behavior."""

    def __init__(self, conn: "FakePsycopgConnection"):
        self.conn = conn
        self.store = conn.store
        self.snapshot: Dict[UUID, dict] = {}

    def __enter__(self):
        self.store.transactions_opened += 1
        # Snapshot staged state for rollback on exception
        self.snapshot = {k: dict(v) for k, v in self.store.refresh_runs.items()}
        self.conn.in_transaction = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.conn.in_transaction = False
        if exc_type is not None:
            # Transaction failed -> rollback to snapshot
            self.store.refresh_runs = {k: dict(v) for k, v in self.snapshot.items()}
            self.store.transactions_rolled_back += 1
            return False
        else:
            # Transaction succeeded -> commit staged changes
            self.store.committed_runs = {k: dict(v) for k, v in self.store.refresh_runs.items()}
            self.store.transactions_committed += 1
            return False


class FakePsycopgConnection:
    """Connection context manager representing real get_db_connection() behavior."""

    def __init__(self, store: "StatefulFakeConnectionStore"):
        self.store = store
        self.in_transaction = False
        self.closed = False

    def __enter__(self):
        if self.store.connection_factory_should_fail_on_compensation and self.store.connections_opened >= 2:
            raise RuntimeError("PostgreSQL connection refused: database down")
        self.store.connections_opened += 1
        # Connection opens with committed DB state
        self.store.refresh_runs = {k: dict(v) for k, v in self.store.committed_runs.items()}
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.closed = True
        # If connection closes, uncommitted changes that were not in a committed transaction are rolled back
        self.store.refresh_runs = {k: dict(v) for k, v in self.store.committed_runs.items()}
        return False

    def transaction(self):
        return FakePsycopgTransaction(self)

    def cursor(self):
        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_cur.__enter__.return_value = mock_cur
        return mock_cur


class StatefulFakeConnectionStore:
    """Simulates real PostgreSQL connection and transaction boundaries for stateful lifecycle testing."""

    def __init__(self):
        self.refresh_runs: Dict[UUID, dict] = {}
        self.committed_runs: Dict[UUID, dict] = {}
        self.connections_opened = 0
        self.transactions_opened = 0
        self.transactions_committed = 0
        self.transactions_rolled_back = 0
        self.provider_upsert_should_fail = False
        self.provider_upsert_exc: Optional[Exception] = None
        self.finalize_return_value: Optional[bool] = None
        self.connection_factory_should_fail_on_compensation = False

    @contextmanager
    def create_connection(self):
        conn = FakePsycopgConnection(self)
        with conn:
            yield conn

    def start_run(self, spec, context):
        run_id = uuid4()
        self.refresh_runs[run_id] = {
            "id": run_id,
            "job_name": spec.job_name,
            "status": "running",
            "sync_action": None,
            "notification_status": "not_required",
            "notified_at": None,
            "notification_error": None,
            "error": None,
            "warnings": [],
        }
        return run_id

    def finalize_run(self, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None):
        if self.finalize_return_value is not None:
            return self.finalize_return_value
        row = self.refresh_runs.get(run_id)
        if not row or row["status"] != "running":
            return False
        row["status"] = outcome.status
        row["sync_action"] = outcome.sync_action
        row["error"] = outcome.error
        row["warnings"] = outcome.warnings
        row["notification_status"] = notification_status
        row["notification_error"] = notification_error
        row["notified_at"] = notified_at
        return True

    def upsert_provider_results(self, run_id, outcomes):
        if self.provider_upsert_should_fail:
            raise self.provider_upsert_exc or RuntimeError("Provider upsert DB crash")

    def update_notification_status(self, run_id, status, error=None, notified_at=None):
        row = self.refresh_runs.get(run_id)
        if not row:
            return False
        row["notification_status"] = status
        row["notification_error"] = error
        row["notified_at"] = notified_at
        return True


def test_stateful_transaction_rollback_and_compensation_lifecycle(caplog):
    """Verify end-to-end rollback of staged Tx C success, execution of compensation transaction, and secret safety."""
    spec = JobRunSpecification(job_name="calendar_refresh", operation_type="calendar_refresh", scope="ECONOMIC")
    store = StatefulFakeConnectionStore()
    secret_token = "ghp_" + "SecretToken_ProviderUpsert_999"
    secret_pass = "SecretPostgresPassword888"
    store.provider_upsert_should_fail = True
    store.provider_upsert_exc = RuntimeError(f"Database error with DSN postgresql://user:{secret_pass}@db.prod:5432 and token={secret_token}")

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    class MultiProviderAdapter(JobResultAdapter):
        def to_outcome(self, result):
            return JobRunOutcome(
                status="success",
                sync_action="updated",
                provider_outcomes=[
                    ProviderRunOutcome(
                        provider_code="TR_TCMB",
                        scope="ECONOMIC",
                        status="success",
                        fresh_event_count=10,
                    )
                ],
            )

    runner = TrackedJobRunner(
        spec=spec,
        adapter=MultiProviderAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    business_fn_calls = 0
    def business_fn():
        nonlocal business_fn_calls
        business_fn_calls += 1
        return DummyResult(status="success", count=10)

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "upsert_provider_results", side_effect=lambda conn, run_id, outcomes: store.upsert_provider_results(run_id, outcomes)):
        
        with caplog.at_level("DEBUG"):
            with pytest.raises(JobTrackingFinalizeError, match="Failed to finalize tracked job"):
                runner.run(business_fn, dry_run=False)

    # 1. Business function strictly executed once
    assert business_fn_calls == 1

    # 2. Stateful transaction boundaries
    assert store.transactions_opened == 3       # Tx A, Tx C, Tx Comp
    assert store.transactions_committed == 2    # Tx A (running), Tx Comp (failed)
    assert store.transactions_rolled_back == 1 # Tx C (staged success rolled back)

    # 3. Final committed DB row
    assert len(store.committed_runs) == 1
    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "failed"
    assert run_row["sync_action"] == "failed_no_write"
    assert run_row["notification_status"] == "sent"
    assert run_row["notified_at"] is not None
    assert "Finalize transaction failed" in run_row["warnings"][0]

    # 4. Central notification email
    assert mock_email.send_email.call_count == 1
    email_call_kwargs = mock_email.send_email.call_args.kwargs
    assert "[FinBot UYARI] calendar_refresh (ECONOMIC) — FAILED" == email_call_kwargs["subject"]
    assert "Merkezi iş takibi veya finalization sırasında operasyonel hata oluştu." in email_call_kwargs["body_text"]
    assert email_call_kwargs["body_html"] is None

    # 5. Secret sanitization across all layers
    assert secret_pass not in caplog.text
    assert secret_token not in caplog.text
    assert secret_pass not in str(run_row)
    assert secret_token not in str(run_row)
    assert secret_pass not in email_call_kwargs["subject"]
    assert secret_pass not in email_call_kwargs["body_text"]
    assert secret_token not in email_call_kwargs["subject"]
    assert secret_token not in email_call_kwargs["body_text"]


def test_compensation_finalize_run_returns_false_logs_warning_and_raises(caplog):
    """Verify that when finalize_run returns False during compensation, a warning is logged and JobTrackingFinalizeError is raised."""
    spec = JobRunSpecification(job_name="calendar_refresh", operation_type="calendar_refresh", scope="ECONOMIC")
    store = StatefulFakeConnectionStore()
    store.provider_upsert_should_fail = True

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    class MultiProviderAdapter(JobResultAdapter):
        def to_outcome(self, result):
            return JobRunOutcome(status="success", sync_action="updated", provider_outcomes=[ProviderRunOutcome(provider_code="TR_TCMB", scope="ECONOMIC", status="success")])

    runner = TrackedJobRunner(
        spec=spec,
        adapter=MultiProviderAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    call_count = 0
    def fake_finalize(conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return True  # Tx C call
        return False  # Compensation call returns False

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=fake_finalize), \
         patch.object(JobTrackingRepository, "upsert_provider_results", side_effect=lambda conn, run_id, outcomes: store.upsert_provider_results(run_id, outcomes)):
        
        with caplog.at_level("DEBUG"):
            with pytest.raises(JobTrackingFinalizeError, match="Failed to finalize tracked job"):
                runner.run(lambda: DummyResult(status="success", count=10), dry_run=False)

    assert "Failed to apply compensation transaction" in caplog.text
    assert "Applied compensation transaction" not in caplog.text


def test_compensation_connection_completely_fails_logs_safely_and_sends_email_best_effort(caplog):
    """Verify that when compensation DB connection fails, email is still dispatched and DB failure is logged safely."""
    spec = JobRunSpecification(job_name="calendar_refresh", operation_type="calendar_refresh", scope="ECONOMIC")
    store = StatefulFakeConnectionStore()
    store.provider_upsert_should_fail = True
    store.connection_factory_should_fail_on_compensation = True

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    class MultiProviderAdapter(JobResultAdapter):
        def to_outcome(self, result):
            return JobRunOutcome(status="success", sync_action="updated", provider_outcomes=[ProviderRunOutcome(provider_code="TR_TCMB", scope="ECONOMIC", status="success")])

    runner = TrackedJobRunner(
        spec=spec,
        adapter=MultiProviderAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "upsert_provider_results", side_effect=lambda conn, run_id, outcomes: store.upsert_provider_results(run_id, outcomes)):
        
        with caplog.at_level("DEBUG"):
            with pytest.raises(JobTrackingFinalizeError, match="Failed to finalize tracked job"):
                runner.run(lambda: DummyResult(status="success", count=10), dry_run=False)

    assert mock_email.send_email.call_count == 1
    assert "Compensation transaction DB write failed for 'calendar_refresh'" in caplog.text


def test_compensation_email_send_returns_false_sets_notification_status_failed():
    """Verify that when email delivery returns False during compensation, notification_status is recorded as failed."""
    spec = JobRunSpecification(job_name="calendar_refresh", operation_type="calendar_refresh", scope="ECONOMIC")
    store = StatefulFakeConnectionStore()
    store.provider_upsert_should_fail = True

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = False

    class MultiProviderAdapter(JobResultAdapter):
        def to_outcome(self, result):
            return JobRunOutcome(status="success", sync_action="updated", provider_outcomes=[ProviderRunOutcome(provider_code="TR_TCMB", scope="ECONOMIC", status="success")])

    runner = TrackedJobRunner(
        spec=spec,
        adapter=MultiProviderAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "upsert_provider_results", side_effect=lambda conn, run_id, outcomes: store.upsert_provider_results(run_id, outcomes)):
        
        with pytest.raises(JobTrackingFinalizeError, match="Failed to finalize tracked job"):
            runner.run(lambda: DummyResult(status="success", count=10), dry_run=False)

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "failed"
    assert run_row["notification_status"] == "failed"
    assert run_row["notification_error"] == "E-posta gönderimi başarısız oldu (SMTP teslimat hatası)."


def test_compensation_email_not_configured_sets_status_not_configured():
    """Verify that when email is not configured, compensation records notification_status as not_configured without sending."""
    spec = JobRunSpecification(job_name="calendar_refresh", operation_type="calendar_refresh", scope="ECONOMIC")
    store = StatefulFakeConnectionStore()
    store.provider_upsert_should_fail = True

    mock_email = MagicMock()
    mock_email.is_configured.return_value = False

    class MultiProviderAdapter(JobResultAdapter):
        def to_outcome(self, result):
            return JobRunOutcome(status="success", sync_action="updated", provider_outcomes=[ProviderRunOutcome(provider_code="TR_TCMB", scope="ECONOMIC", status="success")])

    runner = TrackedJobRunner(
        spec=spec,
        adapter=MultiProviderAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "upsert_provider_results", side_effect=lambda conn, run_id, outcomes: store.upsert_provider_results(run_id, outcomes)):
        
        with pytest.raises(JobTrackingFinalizeError, match="Failed to finalize tracked job"):
            runner.run(lambda: DummyResult(status="success", count=10), dry_run=False)

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "failed"
    assert run_row["notification_status"] == "not_configured"
    mock_email.send_email.assert_not_called()


def test_compensation_no_email_flag_sets_status_disabled():
    """Verify that when no_email is True, compensation records notification_status as disabled without sending email."""
    spec = JobRunSpecification(job_name="calendar_refresh", operation_type="calendar_refresh", scope="ECONOMIC")
    store = StatefulFakeConnectionStore()
    store.provider_upsert_should_fail = True

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    class MultiProviderAdapter(JobResultAdapter):
        def to_outcome(self, result):
            return JobRunOutcome(status="success", sync_action="updated", provider_outcomes=[ProviderRunOutcome(provider_code="TR_TCMB", scope="ECONOMIC", status="success")])

    runner = TrackedJobRunner(
        spec=spec,
        adapter=MultiProviderAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "upsert_provider_results", side_effect=lambda conn, run_id, outcomes: store.upsert_provider_results(run_id, outcomes)):
        
        with pytest.raises(JobTrackingFinalizeError, match="Failed to finalize tracked job"):
            runner.run(lambda: DummyResult(status="success", count=10), dry_run=False, no_email=True)

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "failed"
    assert run_row["notification_status"] == "disabled"
    mock_email.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_economic_calendar_production_execution_path_secret_sanitization(caplog, tmp_path):
    """Verify that exceptions with DSN, password, and tokens in real economic calendar job flow are never leaked."""
    from app.jobs.economic_calendar_refresh_job import EconomicCalendarRefreshJob
    from app.services.economic_calendar_state_store import (
        JsonEconomicCalendarStateStore,
    )

    secret_dsn = "postgresql://prod_user:SecretEcoPassword999@db.prod.internal:5432/finbot"
    secret_token = "ghp_" + "SecretEcoTokenABC123456789"

    cache_path = tmp_path / "economic_calendar.json"
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    mock_service = MagicMock()
    mock_service.providers = []
    mock_service.get_rolling_calendar = AsyncMock(
        side_effect=RuntimeError(f"Database network crash: {secret_dsn} using {secret_token}")
    )

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    job = EconomicCalendarRefreshJob(
        state_store=mock_cache,
        service=mock_service,
        no_email=False,
        email_service=mock_email,
        db_connection_factory=create_mock_db_connection,
    )

    with patch.object(JobTrackingRepository, "finalize_run") as mock_finalize, \
         patch.object(JobTrackingRepository, "update_notification_status", return_value=True):
        mock_finalize.return_value = True
        with caplog.at_level("DEBUG"):
            res = await job.run(as_of=datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc))

        # 1. Logs
        for record in caplog.records:
            assert "SecretEcoPassword999" not in record.message
            assert "SecretEcoPassword999" not in str(record.exc_info)
            assert secret_token not in record.message

        # 2. Finalize outcome
        assert mock_finalize.called
        outcome = mock_finalize.call_args.kwargs["outcome"]
        assert "SecretEcoPassword999" not in str(outcome.error)
        assert "SecretEcoPassword999" not in str(outcome.warnings)
        assert "SecretEcoPassword999" not in str(outcome.metrics)
        assert secret_token not in str(outcome.error)
        assert secret_token not in str(outcome.warnings)
        assert secret_token not in str(outcome.metrics)

        # 3. Email
        assert mock_email.send_email.called
        subject = mock_email.send_email.call_args.kwargs["subject"]
        body_text = mock_email.send_email.call_args.kwargs["body_text"]
        body_html = mock_email.send_email.call_args.kwargs.get("body_html") or ""
        assert "SecretEcoPassword999" not in subject
        assert "SecretEcoPassword999" not in body_text
        assert "SecretEcoPassword999" not in body_html
        assert secret_token not in subject
        assert secret_token not in body_text
        assert secret_token not in body_html


@pytest.mark.asyncio
async def test_earnings_production_execution_path_secret_sanitization(caplog, tmp_path):
    """Verify that exceptions with DSN, password, and tokens in real earnings job flow are never leaked."""
    from app.jobs.earnings_refresh_job import EarningsRefreshJob
    from app.services.calendar_state_store import JsonCalendarStateStore

    secret_dsn = "postgresql://earn_user:SecretEarnPassword777@db.supabase.co:5432/finbot"
    secret_token = "ghp_" + "SecretEarnTokenXYZ987654321"

    mock_cache = JsonCalendarStateStore(cache_dir=str(tmp_path))
    mock_bist_svc = MagicMock()
    mock_bist_svc.get_refresh_bundle = AsyncMock(
        side_effect=RuntimeError(f"Connection refused to {secret_dsn} with auth {secret_token}")
    )
    from app.models.earnings import Nasdaq100EarningsCalendar
    mock_nasdaq_svc = MagicMock()
    mock_nasdaq_svc.get_rolling_calendar = AsyncMock(
        return_value=Nasdaq100EarningsCalendar(
            market="NASDAQ100",
            generated_at="2026-08-28T08:00:00+00:00",
            coverage_start="2026-08-28",
            coverage_end="2026-11-28",
            events=[],
            status="success",
        )
    )

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    job = EarningsRefreshJob(
        state_store=mock_cache,
        bist_service=mock_bist_svc,
        nasdaq_service=mock_nasdaq_svc,
        no_email=False,
        email_service=mock_email,
        db_connection_factory=create_mock_db_connection,
    )

    with patch.object(JobTrackingRepository, "finalize_run") as mock_finalize, \
         patch.object(JobTrackingRepository, "update_notification_status", return_value=True):
        mock_finalize.return_value = True
        with caplog.at_level("DEBUG"):
            res = await job.run(as_of=datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc))

        # 1. Logs
        for record in caplog.records:
            assert "SecretEarnPassword777" not in record.message
            assert "SecretEarnPassword777" not in str(record.exc_info)
            assert secret_token not in record.message

        # 2. Finalize outcome
        assert mock_finalize.called
        outcome = mock_finalize.call_args.kwargs["outcome"]
        assert "SecretEarnPassword777" not in str(outcome.error)
        assert "SecretEarnPassword777" not in str(outcome.warnings)
        assert "SecretEarnPassword777" not in str(outcome.metrics)
        assert secret_token not in str(outcome.error)
        assert secret_token not in str(outcome.warnings)
        assert secret_token not in str(outcome.metrics)

        # 3. Email
        assert mock_email.send_email.called
        subject = mock_email.send_email.call_args.kwargs["subject"]
        body_text = mock_email.send_email.call_args.kwargs["body_text"]
        body_html = mock_email.send_email.call_args.kwargs.get("body_html") or ""
        assert "SecretEarnPassword777" not in subject
        assert "SecretEarnPassword777" not in body_text
        assert "SecretEarnPassword777" not in body_html
        assert secret_token not in subject
        assert secret_token not in body_text
        assert secret_token not in body_html


def test_deterministic_email_status_mapping():
    """Verify that email_status on result model is deterministically mapped without ambiguities."""
    spec = JobRunSpecification(job_name="bist_latest_prices", operation_type="prices_sync", scope="BIST100", provider_code="YAHOO_FINANCE")

    # Case 1: not_required (rule is ALERT_ON_PARTIAL_OR_FAILED, status is success) -> email_status should be None
    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=create_mock_db_connection,
    )

    with patch.object(JobTrackingRepository, "finalize_run", return_value=True):
        res = runner.run(lambda: DummyResult(status="success", count=100), dry_run=False)
        assert res.email_status is None

    # Case 2: disabled (no_email=True) -> email_status should be skipped_no_email
    with patch.object(JobTrackingRepository, "finalize_run", return_value=True):
        res = runner.run(lambda: DummyResult(status="failed", count=0), dry_run=False, no_email=True)
        assert res.email_status == "skipped_no_email"

    # Case 3: not_configured (email not configured) -> email_status should be skipped_not_configured
    mock_email.is_configured.return_value = False
    with patch.object(JobTrackingRepository, "finalize_run", return_value=True):
        res = runner.run(lambda: DummyResult(status="failed", count=0), dry_run=False)
        assert res.email_status == "skipped_not_configured"


def test_github_idempotency_attempt_classification():
    """Verify start_run raises DuplicateJobRunError only when both github_run_id and github_run_attempt are present."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = None  # Simulates INSERT ON CONFLICT DO NOTHING returning empty
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    spec = JobRunSpecification(job_name="test_job", operation_type="prices_sync", scope="BIST100")

    # Case A: Both github_run_id and github_run_attempt present -> DuplicateJobRunError
    ctx_duplicate = JobRunContext(
        started_at=datetime.now(timezone.utc),
        github_run_id="12345678",
        github_run_attempt=1,
    )
    with pytest.raises(DuplicateJobRunError, match="Duplicate GitHub run detected"):
        JobTrackingRepository.start_run(mock_conn, spec, ctx_duplicate)

    # Case B: Only github_run_id present, no attempt -> JobTrackingStartError
    ctx_no_attempt = JobRunContext(
        started_at=datetime.now(timezone.utc),
        github_run_id="12345678",
        github_run_attempt=None,
    )
    with pytest.raises(JobTrackingStartError, match="Failed to insert refresh_runs record"):
        JobTrackingRepository.start_run(mock_conn, spec, ctx_no_attempt)


# ---------------------------------------------------------------------------
# Psycopg Transaction Isolation & Commit/Rollback Regression Tests
# ---------------------------------------------------------------------------


def test_tx_a_persists_running_record_across_connection_boundaries():
    """Verify Tx A commits 'running' record before business logic begins, so it is visible to separate connections."""
    spec = JobRunSpecification(job_name="test_sync_job", operation_type="prices_sync", scope="BIST100")
    store = StatefulFakeConnectionStore()

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    observed_running_in_db = False
    observed_run_id = None

    def business_fn(exec_ctx):
        nonlocal observed_running_in_db, observed_run_id
        observed_run_id = exec_ctx.run_id
        # Inspect committed store while business function is executing
        if exec_ctx.run_id in store.committed_runs:
            rec = store.committed_runs[exec_ctx.run_id]
            if rec["status"] == "running":
                observed_running_in_db = True
        return DummyResult(status="success", count=50)

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)):

        res = runner.run(business_fn, dry_run=False)

    assert observed_running_in_db is True
    assert observed_run_id is not None
    assert store.committed_runs[observed_run_id]["status"] == "success"
    assert store.transactions_committed == 2  # Tx A (start_run) and Tx C (finalize_run)


def test_business_failure_preserves_initial_record_and_commits_failed_state():
    """Verify that when business logic raises an exception, the initial run record is not lost and transitions to failed."""
    spec = JobRunSpecification(job_name="failing_job", operation_type="prices_sync", scope="TEST")
    store = StatefulFakeConnectionStore()

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    observed_run_id = None
    business_calls = 0

    def business_fn(exec_ctx):
        nonlocal observed_run_id, business_calls
        business_calls += 1
        observed_run_id = exec_ctx.run_id
        raise RuntimeError("Fatal calculation error in business logic")

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        with pytest.raises(RuntimeError, match="Fatal calculation error"):
            runner.run(business_fn, dry_run=False)

    assert business_calls == 1
    assert observed_run_id is not None
    # Initial record was created and updated to failed
    assert observed_run_id in store.committed_runs
    record = store.committed_runs[observed_run_id]
    assert record["status"] == "failed"
    assert record["sync_action"] == "failed_no_write"
    assert record["notification_status"] == "sent"


def test_tx_c_atomicity_finalization_and_provider_upsert_rollback_together():
    """Verify that if provider UPSERT fails in Tx C, finalization is rolled back atomically with it."""
    spec = JobRunSpecification(job_name="multi_provider_job", operation_type="calendar_refresh", scope="ECONOMIC")
    store = StatefulFakeConnectionStore()
    store.provider_upsert_should_fail = True

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    class MultiProviderAdapter(JobResultAdapter):
        def to_outcome(self, result):
            return JobRunOutcome(
                status="success",
                sync_action="updated",
                provider_outcomes=[
                    ProviderRunOutcome(provider_code="TUIK", scope="ECONOMIC", status="success"),
                    ProviderRunOutcome(provider_code="TCMB", scope="ECONOMIC", status="success"),
                ],
            )

    runner = TrackedJobRunner(
        spec=spec,
        adapter=MultiProviderAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    business_calls = 0
    def business_fn():
        nonlocal business_calls
        business_calls += 1
        return DummyResult(status="success", count=10)

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "upsert_provider_results", side_effect=lambda conn, run_id, outcomes: store.upsert_provider_results(run_id, outcomes)):

        with pytest.raises(JobTrackingFinalizeError):
            runner.run(business_fn, dry_run=False)

    assert business_calls == 1
    # 3 transactions: Tx A (committed), Tx C (rolled back), Tx Comp (committed)
    assert store.transactions_opened == 3
    assert store.transactions_committed == 2
    assert store.transactions_rolled_back == 1

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "failed"
    assert run_row["sync_action"] == "failed_no_write"


def test_tx_d_notification_commits_in_separate_isolated_transaction():
    """Verify that Tx D (notification status update) runs in its own explicit transaction and commits."""
    spec = JobRunSpecification(job_name="failing_notif_job", operation_type="prices_sync", scope="TEST")
    store = StatefulFakeConnectionStore()

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True
    mock_email.send_email.return_value = True

    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)), \
         patch.object(JobTrackingRepository, "update_notification_status", side_effect=lambda conn, run_id, status, error=None, notified_at=None: store.update_notification_status(run_id, status, error, notified_at)):

        res = runner.run(lambda: DummyResult(status="failed", count=0), dry_run=False)

    # 3 transactions: Tx A (start_run), Tx C (finalize_run), Tx D (update_notification_status)
    assert store.transactions_opened == 3
    assert store.transactions_committed == 3
    assert store.transactions_rolled_back == 0

    run_row = next(iter(store.committed_runs.values()))
    assert run_row["status"] == "failed"
    assert run_row["notification_status"] == "sent"
    assert run_row["notified_at"] is not None


@pytest.mark.asyncio
async def test_async_runner_commits_tx_a_and_tx_c_in_explicit_transactions():
    """Verify that run_async executes Tx A and Tx C within explicit transaction blocks."""
    spec = JobRunSpecification(job_name="async_tracked_job", operation_type="calendar_refresh", scope="ECONOMIC")
    store = StatefulFakeConnectionStore()

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    runner = TrackedJobRunner(
        spec=spec,
        adapter=DummyAdapter(),
        renderer=DummyRenderer(),
        email_service=mock_email,
        notification_rule=JobNotificationRule.ALERT_ON_PARTIAL_OR_FAILED,
        db_connection_factory=store.create_connection,
    )

    observed_in_db = False
    observed_run_id = None

    async def async_business_fn(exec_ctx):
        nonlocal observed_in_db, observed_run_id
        observed_run_id = exec_ctx.run_id
        if exec_ctx.run_id in store.committed_runs and store.committed_runs[exec_ctx.run_id]["status"] == "running":
            observed_in_db = True
        return DummyResult(status="success", count=100)

    with patch.object(JobTrackingRepository, "start_run", side_effect=lambda conn, spec, ctx: store.start_run(spec, ctx)), \
         patch.object(JobTrackingRepository, "finalize_run", side_effect=lambda conn, run_id, outcome, notification_status="not_required", notification_error=None, notified_at=None: store.finalize_run(run_id, outcome, notification_status, notification_error, notified_at)):

        res = await runner.run_async(async_business_fn, dry_run=False)

    assert observed_in_db is True
    assert observed_run_id is not None
    assert store.committed_runs[observed_run_id]["status"] == "success"
    assert store.transactions_committed == 2


def test_economic_calendar_adapter_multi_provider_results_in_tx_c():
    """Verify EconomicCalendarRefreshResultAdapter generates multi-provider outcomes written in Tx C."""
    from app.models.economic_calendar_refresh import (
        EconomicCalendarRefreshJobResult,
        EconomicEventDiff,
        EconomicProviderRefreshOutcome,
    )
    from app.services.job_tracking.adapters.economic_calendar_adapter import (
        EconomicCalendarRefreshResultAdapter,
    )

    adapter = EconomicCalendarRefreshResultAdapter()
    job_result = EconomicCalendarRefreshJobResult(
        status="success",
        sync_action="replaced",
        state_source="network",
        generated_at="2026-08-28T08:00:00+00:00",
        coverage_start="2026-08-28",
        coverage_end="2026-09-04",
        diff=EconomicEventDiff(
            previous_event_count=20,
            fetched_event_count=25,
            final_event_count=25,
        ),
        database_status="success",
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="tuik", provider_name="TUIK", status="success", event_count=5),
            EconomicProviderRefreshOutcome(provider_id="tcmb", provider_name="TCMB", status="success", event_count=5),
            EconomicProviderRefreshOutcome(provider_id="bls", provider_name="BLS", status="success", event_count=5),
            EconomicProviderRefreshOutcome(provider_id="bea", provider_name="BEA", status="success", event_count=5),
            EconomicProviderRefreshOutcome(provider_id="fed", provider_name="FED", status="success", event_count=5),
        ],
    )

    outcome = adapter.to_outcome(job_result)
    assert outcome.status == "success"
    assert outcome.sync_action == "replaced"
    assert outcome.provider_outcomes is not None
    assert len(outcome.provider_outcomes) == 5
    codes = {p.provider_code for p in outcome.provider_outcomes}
    assert codes == {"TUIK", "TCMB", "BLS", "BEA", "FED"}


def test_earnings_adapter_multi_market_results_in_tx_c():
    """Verify EarningsRefreshResultAdapter generates multi-provider outcomes for Tx C."""
    from app.models.earnings_refresh import (
        EarningsRefreshJobResult,
        MarketRefreshOutcome,
        OfficialIrRefreshSummary,
    )
    from app.services.job_tracking.adapters.earnings_adapter import (
        EarningsRefreshResultAdapter,
    )

    adapter = EarningsRefreshResultAdapter()
    job_result = EarningsRefreshJobResult(
        started_at="2026-08-28T08:00:00+00:00",
        finished_at="2026-08-28T08:00:05+00:00",
        duration_seconds=5.0,
        coverage_start="2026-08-28",
        coverage_end="2026-11-28",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-28",
            coverage_end="2026-11-28",
            official_ir_summary=OfficialIrRefreshSummary(
                kap_fetch_status="success",
                kap_fetched_event_count=15,
                providers=[],
            ),
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-28",
            coverage_end="2026-11-28",
        ),
        database_status="success",
    )

    outcome = adapter.to_outcome(job_result)
    assert outcome.status == "success"
    assert outcome.provider_outcomes is not None
    assert len(outcome.provider_outcomes) >= 2
    codes = {p.provider_code for p in outcome.provider_outcomes}
    assert "KAP" in codes
    assert "YAHOO_FINANCE" in codes


def test_psycopg_uncommitted_connection_close_rolls_back_demonstration():
    """Explicit demonstration: uncommitted changes on connection close are discarded by the test double."""
    store = StatefulFakeConnectionStore()

    # Simulate an uncommitted operation
    with store.create_connection() as conn:
        run_id = store.start_run(
            JobRunSpecification(job_name="uncommitted_job", operation_type="prices_sync", scope="TEST"),
            JobRunContext(started_at=datetime.now(timezone.utc)),
        )
        assert run_id in store.refresh_runs
        # Notice: with conn.transaction(): is NOT called here!

    # When connection closes without transaction commit, the record is discarded
    assert run_id not in store.committed_runs
    assert len(store.committed_runs) == 0
