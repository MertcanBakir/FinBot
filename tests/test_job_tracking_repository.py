import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.job_tracking import (
    DuplicateJobRunError,
    JobRunContext,
    JobRunOutcome,
    JobRunSpecification,
    JobTrackingFinalizeError,
    JobTrackingStartError,
    ProviderRunOutcome,
)
from app.repositories.job_tracking_repository import (
    JobTrackingRepository,
    sanitize_tracking_error,
)


def test_sanitize_tracking_error_masks_secrets():
    raw = (
        "Connection failed to postgresql://postgres:SuperSecretPassword123@db.supabase.co:5432/postgres "
        "with token Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.SecretToken "
        "and smtp_password=SuperSmtpPassword!"
    )
    sanitized = sanitize_tracking_error(raw)
    assert "SuperSecretPassword123" not in sanitized
    assert "SuperSmtpPassword!" not in sanitized
    assert "SecretToken" not in sanitized
    assert sanitized == "Veritabanı veya servis erişim hatası."


def test_start_run_inserts_refresh_run():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    expected_id = uuid4()
    mock_cur.fetchone.return_value = (str(expected_id),)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    spec = JobRunSpecification(
        job_name="nasdaq_latest_prices_job",
        operation_type="prices_sync",
        scope="NASDAQ100",
        provider_code="YAHOO_FINANCE",
    )
    context = JobRunContext(
        started_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        trigger_type="scheduled",
        dry_run=False,
        no_email=False,
        github_run_id="12345678",
        github_run_attempt=1,
    )

    with patch.object(JobTrackingRepository, "get_provider_id_map", return_value={"YAHOO_FINANCE": uuid4()}):
        run_id = JobTrackingRepository.start_run(mock_conn, spec, context)

    assert run_id == expected_id
    mock_cur.execute.assert_called_once()
    args = mock_cur.execute.call_args[0][1]
    assert args[0] == "nasdaq_latest_prices_job"
    assert args[1] == "prices_sync"
    assert args[2] == "NASDAQ100"
    assert args[4] == context.started_at
    assert args[5] == "scheduled"
    assert args[6] == "12345678"
    assert args[7] == 1


def test_finalize_run_updates_record():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    run_id = uuid4()
    outcome = JobRunOutcome(
        status="success",
        sync_action="updated",
        finished_at=datetime(2026, 8, 28, 10, 1, tzinfo=timezone.utc),
        duration_seconds=60.0,
        warnings=["Non-critical warning"],
        metrics={"inserted_or_updated_count": 97, "requested_count": 97},
    )

    ok = JobTrackingRepository.finalize_run(
        mock_conn,
        run_id=run_id,
        outcome=outcome,
        notification_status="not_required",
    )

    assert ok is True
    mock_cur.execute.assert_called_once()
    query = mock_cur.execute.call_args[0][0]
    args = mock_cur.execute.call_args[0][1]
    assert "status = %s" in query
    assert "WHERE id = %s AND status = 'running'" in query
    assert "duration_seconds" not in query
    assert "notification_sent_at" not in query
    assert "notified_at = %s" in query
    assert args[0] == "success"
    assert args[1] == "updated"
    assert args[2] is None  # skip_reason
    assert json.loads(args[8]) == ["Non-critical warning"]
    assert json.loads(args[9]) == {"inserted_or_updated_count": 97, "requested_count": 97}
    assert args[11] == "not_required"
    assert args[14] == run_id


def test_finalize_run_returns_false_if_not_running():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.rowcount = 0  # No rows updated because status != 'running'
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    run_id = uuid4()
    outcome = JobRunOutcome(status="success", sync_action="updated")

    ok = JobTrackingRepository.finalize_run(mock_conn, run_id=run_id, outcome=outcome)
    assert ok is False


def test_upsert_provider_results_multi_provider():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    tuik_id = uuid4()
    tcmb_id = uuid4()
    run_id = uuid4()

    outcomes = [
        ProviderRunOutcome(
            provider_code="TUIK",
            scope="ECONOMIC",
            status="success",
            structure_validated=True,
            observed_count=10,
            candidate_count=10,
            fresh_event_count=10,
            carried_forward_count=0,
            malformed_count=0,
            retry_count=0,
            metrics={"coverage_days": 90},
        ),
        ProviderRunOutcome(
            provider_code="TCMB",
            scope="ECONOMIC",
            status="partial",
            structure_validated=True,
            observed_count=5,
            candidate_count=5,
            fresh_event_count=4,
            carried_forward_count=1,
            malformed_count=0,
            retry_count=1,
            warnings=["1 event carried forward"],
        ),
    ]

    with patch.object(
        JobTrackingRepository,
        "get_provider_id_map",
        return_value={"TUIK": tuik_id, "TCMB": tcmb_id},
    ):
        JobTrackingRepository.upsert_provider_results(mock_conn, run_id=run_id, outcomes=outcomes)

    assert mock_cur.execute.call_count == 2


def test_start_run_detects_duplicate_github_run_and_raises_duplicate_error():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = None  # ON CONFLICT DO NOTHING returned no rows
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    spec = JobRunSpecification(
        job_name="nasdaq_latest_prices_job",
        operation_type="prices_sync",
        scope="NASDAQ100",
        provider_code="YAHOO_FINANCE",
    )
    context = JobRunContext(
        started_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        trigger_type="scheduled",
        dry_run=False,
        no_email=False,
        github_run_id="999888777",
        github_run_attempt=2,
    )

    with patch.object(JobTrackingRepository, "get_provider_id_map", return_value={"YAHOO_FINANCE": uuid4()}):
        with pytest.raises(DuplicateJobRunError) as exc_info:
            JobTrackingRepository.start_run(mock_conn, spec, context)

    assert "Duplicate GitHub run detected" in str(exc_info.value)
    assert "999888777" in str(exc_info.value)


def test_upsert_provider_results_sql_has_on_conflict_do_update():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    tuik_id = uuid4()
    run_id = uuid4()

    outcomes = [
        ProviderRunOutcome(
            provider_code="TUIK",
            scope="ECONOMIC",
            status="success",
            structure_validated=True,
            fresh_event_count=10,
        )
    ]

    with patch.object(
        JobTrackingRepository,
        "get_provider_id_map",
        return_value={"TUIK": tuik_id},
    ):
        JobTrackingRepository.upsert_provider_results(mock_conn, run_id=run_id, outcomes=outcomes)

    mock_cur.execute.assert_called_once()
    query = mock_cur.execute.call_args[0][0]
    assert "ON CONFLICT (refresh_run_id, provider_id, scope)" in query
    assert "DO UPDATE SET" in query
    assert "status = EXCLUDED.status" in query
    assert "updated_at = EXCLUDED.metrics" in query or "metrics = EXCLUDED.metrics" in query


def test_upsert_provider_results_raises_on_unknown_provider():
    # Concrete connection object without MagicMock name to test real fail-closed path
    class RealDbConn:
        def cursor(self):
            cur = MagicMock()
            cur.fetchall.return_value = [("TUIK", str(uuid4()))]
            ctx = MagicMock()
            ctx.__enter__.return_value = cur
            return ctx

    conn = RealDbConn()
    run_id = uuid4()

    outcomes = [
        ProviderRunOutcome(
            provider_code="FINBOT_UNKNOWN",
            scope="ECONOMIC",
            status="success",
        )
    ]

    with pytest.raises(JobTrackingFinalizeError) as exc_info:
        JobTrackingRepository.upsert_provider_results(conn, run_id=run_id, outcomes=outcomes)

    assert "FINBOT_UNKNOWN" in str(exc_info.value)
    assert "Unknown provider_code" in str(exc_info.value)


def test_get_provider_id_map_fails_closed_when_empty():
    class RealDbConn:
        def cursor(self):
            cur = MagicMock()
            cur.fetchall.return_value = []  # Empty table in DB
            ctx = MagicMock()
            ctx.__enter__.return_value = cur
            return ctx

    with pytest.raises(JobTrackingStartError) as exc_info:
        JobTrackingRepository.get_provider_id_map(RealDbConn())

    assert "empty or inaccessible" in str(exc_info.value)
