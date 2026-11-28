from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.lambda_handlers.common import run_tracked_lambda_job


def test_connection_is_closed_before_external_job(monkeypatch):
    monkeypatch.setenv("TEST_JOB_MODE", "apply-db")
    connections = []
    active = []

    @contextmanager
    def connection():
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)
        connections.append(conn)
        active.append(conn)
        try:
            yield conn
        finally:
            active.remove(conn)

    def execute_job(**kwargs):
        assert not active, "database connection must be released before job execution"
        return SimpleNamespace(status="success")

    result = run_tracked_lambda_job(
        {"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"},
        job_name="test_job", mode_name="TEST_JOB_MODE", 
        busy_message="busy", failure_message="failed", connection_factory=connection,
        execute_job=execute_job,
    )
    assert result == {"status": "success", "dry_run": False}
    assert len(connections) == 2


def test_unexpected_error_records_failure_and_releases_lease(monkeypatch):
    monkeypatch.setenv("TEST_JOB_MODE", "apply-db")
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)

    @contextmanager
    def connection():
        yield conn

    def execute_job(**kwargs):
        raise ValueError("unexpected")

    with pytest.raises(ValueError, match="unexpected"):
        run_tracked_lambda_job(
            {"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"},
            job_name="test_job", mode_name="TEST_JOB_MODE", 
            busy_message="busy", failure_message="failed", connection_factory=connection,
            execute_job=execute_job,
        )
    calls = conn.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert calls[-2].args[1][0] == "failed"
    assert "DELETE FROM private.lambda_job_leases" in calls[-1].args[0]


def test_lease_covers_remaining_lambda_time_with_safety_margin(monkeypatch):
    monkeypatch.setenv("TEST_JOB_MODE", "dry-run")
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)

    @contextmanager
    def connection():
        yield conn

    run_tracked_lambda_job(
        {}, job_name="test_job", mode_name="TEST_JOB_MODE", 
        busy_message="busy", failure_message="failed", connection_factory=connection,
        execute_job=lambda **kwargs: SimpleNamespace(status="success"),
        lambda_context=SimpleNamespace(get_remaining_time_in_millis=lambda: 300000),
    )
    calls = conn.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert calls[0].args[1][2] == 420
