"""Opt-in tests using an isolated local PostgreSQL cluster."""

import os
import socket
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from app.lambda_handlers import (
    bist_prices,
    crypto_prices,
    fx_price,
    nasdaq_prices,
    tefas_prices,
)

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LAMBDA_POSTGRES") != "1", reason="Opt-in local PostgreSQL")
ROOT = Path(__file__).resolve().parents[1]
CONNECT = psycopg.connect


@pytest.fixture(scope="module")
def pg_connection_info(tmp_path_factory):
    binaries = Path(os.environ.get("TEST_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin"))
    root = tmp_path_factory.mktemp("lambda-skipped-postgres")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run([str(binaries / "initdb"), "-D", str(root / "db"), "-U", "postgres", "-A", "trust", "--no-locale", "-E", "UTF8"], check=True, capture_output=True)
    subprocess.run([str(binaries / "pg_ctl"), "-D", str(root / "db"), "-l", str(root / "log"), "-o", f"-h 127.0.0.1 -p {port} -c unix_socket_directories=''", "-w", "start"], check=True, capture_output=True)
    info = dict(host="127.0.0.1", port=port, user="postgres", dbname="postgres")
    try:
        with CONNECT(**info, autocommit=True) as conn:
            conn.execute("CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role; CREATE SCHEMA private", prepare=False)
            conn.execute((ROOT / "supabase/migrations/20261127080046_crypto_job_idempotency.sql").read_text(), prepare=False)
            conn.execute((ROOT / "supabase/migrations/20261128102109_job_execution_skipped.sql").read_text(), prepare=False)
            conn.execute((ROOT / "supabase/migrations/20261128102125_lambda_job_leases.sql").read_text(), prepare=False)
        yield info
    finally:
        subprocess.run([str(binaries / "pg_ctl"), "-D", str(root / "db"), "-m", "fast", "-w", "stop"], check=True, capture_output=True)


@pytest.mark.parametrize(("handler", "mode", "job_name"), [
    (bist_prices, "BIST_JOB_MODE", "bist_latest_prices_job"),
    (nasdaq_prices, "NASDAQ_JOB_MODE", "nasdaq_latest_prices_job"),
    (crypto_prices, "CRYPTO_JOB_MODE", "binance_crypto_latest_prices_job"),
    (fx_price, "FX_JOB_MODE", "fx_latest_price_job"),
    (tefas_prices, "TEFAS_JOB_MODE", "tefas_latest_prices_job"),
])
def test_skipped_persists_and_duplicate_does_not_run(monkeypatch, pg_connection_info, handler, mode, job_name):
    @contextmanager
    def connection():
        with CONNECT(**pg_connection_info) as conn:
            yield conn

    calls = []
    def job(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status="skipped", skip_reason="market_closed")

    monkeypatch.setattr(handler, "get_db_connection", connection)
    monkeypatch.setattr(handler, "execute_tracked_job", job)
    monkeypatch.setenv(mode, "apply-db")
    event = {"request_id": str(uuid4())}
    assert handler.lambda_handler(event, None) == {"status": "skipped", "dry_run": False, "reason": "market_closed"}
    assert handler.lambda_handler(event, None) == {"status": "skipped", "dry_run": False, "reason": "already_completed"}
    assert len(calls) == 1
    with CONNECT(**pg_connection_info) as conn:
        assert conn.execute("SELECT status, attempt_count FROM private.job_execution_keys WHERE job_name = %s AND execution_key = %s", (job_name, handler._execution_key(event))).fetchone() == ("skipped", 1)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE private.job_execution_keys SET status = 'invalid' WHERE job_name = %s", (job_name,))


def test_active_lease_blocks_distinct_triggers_and_expired_lease_recovers(monkeypatch, pg_connection_info):
    from app.lambda_handlers import common

    job_name = f"lease-test-{uuid4()}"
    old_owner = uuid4()
    old_key = "manual:" + str(uuid4())
    with CONNECT(**pg_connection_info) as conn:
        conn.execute("INSERT INTO private.lambda_job_leases VALUES (%s, %s, clock_timestamp() + interval '1 minute')", (job_name, old_owner))
        conn.execute("INSERT INTO private.job_execution_keys(job_name, execution_key, status) VALUES (%s, %s, 'running')", (job_name, old_key))

    @contextmanager
    def connection():
        with CONNECT(**pg_connection_info) as conn:
            yield conn

    monkeypatch.setenv("LEASE_TEST_MODE", "apply-db")
    event = {"request_id": str(uuid4())}
    kwargs = dict(job_name=job_name, mode_name="LEASE_TEST_MODE",
                  busy_message="busy", failure_message="failed", connection_factory=connection,
                  execute_job=lambda **kw: SimpleNamespace(status="success"))
    with pytest.raises(RuntimeError, match="busy"):
        common.run_tracked_lambda_job(event, **kwargs)
    with CONNECT(**pg_connection_info) as conn:
        conn.execute("UPDATE private.lambda_job_leases SET expires_at = clock_timestamp() - interval '1 second' WHERE job_name = %s", (job_name,))
    assert common.run_tracked_lambda_job(event, **kwargs)["status"] == "success"
    with CONNECT(**pg_connection_info) as conn:
        assert conn.execute("SELECT status FROM private.job_execution_keys WHERE job_name = %s AND execution_key = %s", (job_name, old_key)).fetchone() == ("failed",)
        assert conn.execute("SELECT count(*) FROM private.lambda_job_leases WHERE job_name = %s", (job_name,)).fetchone() == (0,)


def test_previous_owner_cannot_finish_or_remove_new_owner(pg_connection_info):
    from app.lambda_handlers.common import _finish_lease

    job_name, old_owner, new_owner = f"lease-test-{uuid4()}", uuid4(), uuid4()
    with CONNECT(**pg_connection_info) as conn:
        conn.execute("INSERT INTO private.lambda_job_leases VALUES (%s, %s, clock_timestamp() + interval '1 minute')", (job_name, new_owner))

    @contextmanager
    def connection():
        with CONNECT(**pg_connection_info) as conn:
            yield conn

    with pytest.raises(RuntimeError, match="ownership was lost"):
        _finish_lease(connection, job_name, None, old_owner, "success")
    with CONNECT(**pg_connection_info) as conn:
        assert conn.execute("SELECT owner_id FROM private.lambda_job_leases WHERE job_name = %s", (job_name,)).fetchone() == (new_owner,)


def test_concurrent_different_triggers_cannot_run_same_job(monkeypatch, pg_connection_info):
    from concurrent.futures import ThreadPoolExecutor

    from app.lambda_handlers.common import run_tracked_lambda_job

    @contextmanager
    def connection():
        with CONNECT(**pg_connection_info) as conn:
            yield conn

    entered, release = threading.Event(), threading.Event()

    def job(**kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test job was not released")
        return SimpleNamespace(status="success")

    monkeypatch.setenv("LEASE_TEST_MODE", "apply-db")
    kwargs = dict(job_name=f"lease-test-{uuid4()}", mode_name="LEASE_TEST_MODE",
                  busy_message="busy", failure_message="failed", connection_factory=connection,
                  execute_job=job)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(run_tracked_lambda_job, {"request_id": str(uuid4())}, **kwargs)
        try:
            assert entered.wait(10)
            with pytest.raises(RuntimeError, match="busy"):
                run_tracked_lambda_job({"request_id": str(uuid4())}, **kwargs)
        finally:
            release.set()
        assert first.result(timeout=10)["status"] == "success"
