from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.lambda_handlers import nasdaq_prices


@pytest.fixture
def connection(monkeypatch):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)

    @contextmanager
    def factory():
        yield conn

    monkeypatch.setattr(nasdaq_prices, "get_db_connection", factory)
    return conn


def test_default_is_manual_dry_run(monkeypatch, connection):
    job = MagicMock(return_value=SimpleNamespace(status="success"))
    monkeypatch.setattr(nasdaq_prices, "run_job", job)
    monkeypatch.delenv("NASDAQ_JOB_MODE", raising=False)
    assert nasdaq_prices.lambda_handler({}, None) == {"status": "success", "dry_run": True}
    assert job.call_args.args[0] == ["--dry-run"]
    assert job.call_args.kwargs["run_context"].trigger_type == "manual"


def test_live_schedule_and_idempotency_key(monkeypatch, connection):
    job = MagicMock(return_value=SimpleNamespace(status="success"))
    monkeypatch.setattr(nasdaq_prices, "run_job", job)
    monkeypatch.setenv("NASDAQ_JOB_MODE", "apply-db")
    event = {"source": "finbot.scheduler", "schedule_arn": "arn:aws:scheduler:eu-west-1:123:schedule/default/fx", "scheduled_time": "2026-11-27T12:00:00Z"}
    assert nasdaq_prices.lambda_handler(event, None) == {"status": "success", "dry_run": False}
    assert job.call_args.args[0] == ["--apply-db"]
    assert job.call_args.kwargs["run_context"].trigger_type == "scheduled"
    assert nasdaq_prices._execution_key(event).endswith("2026-11-27T12:00:00+00:00")


def test_successful_duplicate_does_not_run_job(monkeypatch, connection):
    monkeypatch.setenv("NASDAQ_JOB_MODE", "apply-db")
    connection.cursor.return_value.__enter__.return_value.fetchone.side_effect = [(True,), None]
    job = MagicMock()
    monkeypatch.setattr(nasdaq_prices, "run_job", job)
    result = nasdaq_prices.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    assert result["reason"] == "already_completed"
    job.assert_not_called()


def test_failed_job_causes_lambda_error_and_records_failed(monkeypatch, connection):
    monkeypatch.setenv("NASDAQ_JOB_MODE", "apply-db")
    monkeypatch.setattr(nasdaq_prices, "run_job", MagicMock(return_value=SimpleNamespace(status="failed")))
    with pytest.raises(RuntimeError, match="NASDAQ price job failed"):
        nasdaq_prices.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    calls = connection.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert calls[-1].args[1][0] == "failed"


def test_busy_job_fails_for_retry(monkeypatch, connection):
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
    monkeypatch.setattr(nasdaq_prices, "run_job", MagicMock())
    with pytest.raises(RuntimeError, match="already running"):
        nasdaq_prices.lambda_handler({}, None)
    nasdaq_prices.run_job.assert_not_called()


@pytest.mark.parametrize("event", [{}, {"source": "finbot.scheduler"}])
def test_live_run_requires_valid_idempotency_key(monkeypatch, event):
    monkeypatch.setenv("NASDAQ_JOB_MODE", "apply-db")
    with pytest.raises(ValueError):
        nasdaq_prices.lambda_handler(event, None)


def test_invalid_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("NASDAQ_JOB_MODE", "typo")
    with pytest.raises(ValueError, match="NASDAQ_JOB_MODE"):
        nasdaq_prices.lambda_handler({}, None)


def test_schedule_key_is_stable_for_equivalent_times_and_distinct_for_next_run():
    base = {"source": "finbot.scheduler", "schedule_arn": "arn:aws:scheduler:eu-west-1:123:schedule/default/fx"}
    utc = nasdaq_prices._execution_key({**base, "scheduled_time": "2026-11-30T07:00:00Z"})
    istanbul = nasdaq_prices._execution_key({**base, "scheduled_time": "2026-11-30T10:00:00+03:00"})
    next_hour = nasdaq_prices._execution_key({**base, "scheduled_time": "2026-11-30T08:00:00Z"})
    assert utc == istanbul
    assert utc != next_hour


def test_partial_result_is_recorded_without_triggering_lambda_failure(monkeypatch, connection):
    monkeypatch.setenv("NASDAQ_JOB_MODE", "apply-db")
    monkeypatch.setattr(nasdaq_prices, "run_job", MagicMock(return_value=SimpleNamespace(status="partial")))
    result = nasdaq_prices.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    assert result == {"status": "partial", "dry_run": False}
    calls = connection.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert calls[-1].args[1][0] == "partial"


def test_market_closed_is_recorded_as_completed(monkeypatch, connection):
    monkeypatch.setenv("NASDAQ_JOB_MODE", "apply-db")
    monkeypatch.setattr(nasdaq_prices, "run_job", MagicMock(return_value=SimpleNamespace(status="skipped", skip_reason="market_closed")))
    result = nasdaq_prices.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    assert result == {"status": "skipped", "dry_run": False, "reason": "market_closed"}
    calls = connection.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert calls[-1].args[1][0] == "success"
