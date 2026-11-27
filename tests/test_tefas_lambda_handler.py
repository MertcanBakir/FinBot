from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.lambda_handlers import tefas_prices


@pytest.fixture
def connection(monkeypatch):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)

    @contextmanager
    def factory():
        yield conn

    monkeypatch.setattr(tefas_prices, "get_db_connection", factory)
    return conn


def test_default_is_manual_dry_run(monkeypatch, connection):
    job = MagicMock(return_value=SimpleNamespace(status="success"))
    monkeypatch.setattr(tefas_prices, "run_job", job)
    monkeypatch.delenv("TEFAS_JOB_MODE", raising=False)
    assert tefas_prices.lambda_handler({}, None) == {"status": "success", "dry_run": True}
    assert job.call_args.args[0] == ["--dry-run"]
    assert job.call_args.kwargs["run_context"].trigger_type == "manual"


def test_live_schedule_and_idempotency_key(monkeypatch, connection):
    job = MagicMock(return_value=SimpleNamespace(status="success"))
    monkeypatch.setattr(tefas_prices, "run_job", job)
    monkeypatch.setenv("TEFAS_JOB_MODE", "apply-db")
    event = {"source": "finbot.scheduler", "schedule_arn": "arn:aws:scheduler:eu-west-1:123:schedule/default/fx", "scheduled_time": "2026-11-27T12:00:00Z"}
    assert tefas_prices.lambda_handler(event, None) == {"status": "success", "dry_run": False}
    assert job.call_args.args[0] == ["--apply-db"]
    assert job.call_args.kwargs["run_context"].trigger_type == "scheduled"
    assert tefas_prices._execution_key(event).endswith("2026-11-27T12:00:00+00:00")


def test_successful_duplicate_does_not_run_job(monkeypatch, connection):
    monkeypatch.setenv("TEFAS_JOB_MODE", "apply-db")
    connection.cursor.return_value.__enter__.return_value.fetchone.side_effect = [(True,), None]
    job = MagicMock()
    monkeypatch.setattr(tefas_prices, "run_job", job)
    result = tefas_prices.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    assert result["reason"] == "already_completed"
    job.assert_not_called()


def test_failed_job_causes_lambda_error_and_records_failed(monkeypatch, connection):
    monkeypatch.setenv("TEFAS_JOB_MODE", "apply-db")
    monkeypatch.setattr(tefas_prices, "run_job", MagicMock(return_value=SimpleNamespace(status="failed")))
    with pytest.raises(RuntimeError, match="TEFAS price job failed"):
        tefas_prices.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    calls = connection.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert calls[-1].args[1][0] == "failed"


def test_busy_job_fails_for_retry(monkeypatch, connection):
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
    monkeypatch.setattr(tefas_prices, "run_job", MagicMock())
    with pytest.raises(RuntimeError, match="already running"):
        tefas_prices.lambda_handler({}, None)
    tefas_prices.run_job.assert_not_called()


@pytest.mark.parametrize("event", [{}, {"source": "finbot.scheduler"}])
def test_live_run_requires_valid_idempotency_key(monkeypatch, event):
    monkeypatch.setenv("TEFAS_JOB_MODE", "apply-db")
    with pytest.raises(ValueError):
        tefas_prices.lambda_handler(event, None)


def test_invalid_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("TEFAS_JOB_MODE", "typo")
    with pytest.raises(ValueError, match="TEFAS_JOB_MODE"):
        tefas_prices.lambda_handler({}, None)


def test_schedule_key_is_stable_for_equivalent_times_and_distinct_for_next_run():
    base = {"source": "finbot.scheduler", "schedule_arn": "arn:aws:scheduler:eu-west-1:123:schedule/default/fx"}
    utc = tefas_prices._execution_key({**base, "scheduled_time": "2026-11-30T07:00:00Z"})
    istanbul = tefas_prices._execution_key({**base, "scheduled_time": "2026-11-30T10:00:00+03:00"})
    next_hour = tefas_prices._execution_key({**base, "scheduled_time": "2026-11-30T08:00:00Z"})
    assert utc == istanbul
    assert utc != next_hour


def test_partial_result_is_recorded_without_triggering_lambda_failure(monkeypatch, connection):
    monkeypatch.setenv("TEFAS_JOB_MODE", "apply-db")
    monkeypatch.setattr(tefas_prices, "run_job", MagicMock(return_value=SimpleNamespace(status="partial")))
    result = tefas_prices.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    assert result == {"status": "partial", "dry_run": False}
    calls = connection.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert calls[-1].args[1][0] == "partial"
