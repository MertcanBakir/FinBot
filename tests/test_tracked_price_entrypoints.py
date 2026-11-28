from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.jobs import (
    binance_crypto_latest_prices_job,
    bist_latest_prices_job,
    fx_latest_price_job,
    nasdaq_latest_prices_job,
    tefas_latest_prices_job,
)


@pytest.mark.parametrize("module", [bist_latest_prices_job, nasdaq_latest_prices_job,
                                    binance_crypto_latest_prices_job, fx_latest_price_job,
                                    tefas_latest_prices_job])
def test_python_entrypoint_uses_runner_without_cli_parsing(monkeypatch, module):
    runner = MagicMock()
    expected = SimpleNamespace(status="success")
    runner.run.return_value = expected
    monkeypatch.setattr(module, "TrackedJobRunner", lambda **kwargs: runner)
    if hasattr(module, "_print_summary"):
        monkeypatch.setattr(module, "_print_summary", lambda result: None)
    monkeypatch.setattr(module.argparse.ArgumentParser, "parse_args", lambda *a, **k: pytest.fail("CLI parser called"))
    context = object()
    assert module.execute_tracked_job(dry_run=False, no_email=True, run_context=context) is expected
    assert runner.run.call_args.kwargs == {"dry_run": False, "no_email": True, "context": context}
