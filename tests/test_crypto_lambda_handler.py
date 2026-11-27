from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.lambda_handlers import crypto_prices


@pytest.fixture
def connection(monkeypatch):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)

    @contextmanager
    def factory():
        yield conn

    monkeypatch.setattr(crypto_prices, "get_db_connection", factory)
    return conn


def test_default_is_manual_dry_run(monkeypatch, connection):
    job = MagicMock(return_value=SimpleNamespace(status="success"))
    monkeypatch.setattr(crypto_prices, "run_job", job)
    monkeypatch.delenv("CRYPTO_JOB_MODE", raising=False)
    assert crypto_prices.lambda_handler({}, None) == {"status": "success", "dry_run": True}
    assert job.call_args.args[0] == ["--dry-run"]
    assert job.call_args.kwargs["run_context"].trigger_type == "manual"


def test_scheduled_live_run_and_failure(monkeypatch, connection):
    job = MagicMock(return_value=SimpleNamespace(status="failed"))
    monkeypatch.setattr(crypto_prices, "run_job", job)
    monkeypatch.setenv("CRYPTO_JOB_MODE", "apply-db")
    with pytest.raises(RuntimeError):
        crypto_prices.lambda_handler({"source": "finbot.scheduler", "schedule_arn": "arn:aws:scheduler:eu-west-1:123:schedule/default/crypto", "scheduled_time": "2026-11-27T12:17:00Z"}, None)
    assert job.call_args.args[0] == ["--apply-db"]
    assert job.call_args.kwargs["run_context"].trigger_type == "scheduled"


def test_overlap_does_not_execute(monkeypatch, connection):
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
    job = MagicMock()
    monkeypatch.setattr(crypto_prices, "run_job", job)
    with pytest.raises(RuntimeError, match="already running"):
        crypto_prices.lambda_handler({}, None)
    job.assert_not_called()


def test_invalid_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("CRYPTO_JOB_MODE", "typo")
    with pytest.raises(ValueError):
        crypto_prices.lambda_handler({}, None)


def test_manual_request_requires_stable_id(monkeypatch, connection):
    monkeypatch.setenv("CRYPTO_JOB_MODE", "apply-db")
    with pytest.raises(ValueError, match="request_id"):
        crypto_prices.lambda_handler({}, None)


def test_completed_key_does_not_rerun(monkeypatch, connection):
    monkeypatch.setenv("CRYPTO_JOB_MODE", "apply-db")
    connection.cursor.return_value.__enter__.return_value.fetchone.side_effect = [(True,), None]
    job = MagicMock()
    monkeypatch.setattr(crypto_prices, "run_job", job)
    result = crypto_prices.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    assert result["reason"] == "already_completed"
    job.assert_not_called()
