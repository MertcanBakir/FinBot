from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.lambda_handlers import crypto_prices, fx_price, tefas_prices


@pytest.mark.parametrize(("handler", "mode"), [
    (crypto_prices, "CRYPTO_JOB_MODE"),
    (fx_price, "FX_JOB_MODE"),
    (tefas_prices, "TEFAS_JOB_MODE"),
])
@pytest.mark.parametrize("reason", ["not_ready", None])
def test_skipped_result_preserves_optional_reason(monkeypatch, handler, mode, reason):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(handler, "get_db_connection", connection)
    monkeypatch.setattr(handler, "execute_tracked_job", lambda *args, **kwargs: SimpleNamespace(status="skipped", skip_reason=reason))
    monkeypatch.setenv(mode, "apply-db")
    response = handler.lambda_handler({"request_id": "fa0bbfe5-b247-4ac4-a79e-5ec953dc6450"}, None)
    expected = {"status": "skipped", "dry_run": False}
    if reason is not None:
        expected["reason"] = reason
    assert response == expected
    calls = conn.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert calls[-2].args[1][0] == "skipped"
