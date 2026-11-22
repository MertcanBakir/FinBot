"""Opt-in acceptance tests against a disposable local PostgreSQL cluster.

RUN_SNAPSHOT_POSTGRES=1 venv/bin/python -m pytest --allow-hosts=127.0.0.1 -q tests/test_daily_snapshot_postgres.py
No application credentials or remote database are used.
"""

import os
import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SNAPSHOT_POSTGRES") != "1", reason="Opt-in local PostgreSQL"
)
ROOT = Path(__file__).resolve().parents[1]
CONNECT = psycopg.connect


@pytest.fixture(scope="module")
def snapshot_cluster(tmp_path_factory):
    binaries = Path(os.environ.get("TEST_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin"))
    assert (binaries / "initdb").is_file()
    root = tmp_path_factory.mktemp("daily-snapshot-postgres")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run(
        [str(binaries / "initdb"), "-D", str(root / "db"), "-U", "postgres",
         "-A", "trust", "--no-locale", "-E", "UTF8"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(binaries / "pg_ctl"), "-D", str(root / "db"), "-l", str(root / "log"),
         "-o", f"-h 127.0.0.1 -p {port} -c unix_socket_directories=''", "-w", "start"],
        check=True, capture_output=True,
    )
    try:
        with CONNECT(host="127.0.0.1", port=port, user="postgres", dbname="postgres",
                     autocommit=True) as conn:
            conn.execute("""
                CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN;
                CREATE ROLE service_role NOLOGIN;
                CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY);
                CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
                $$ SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
                GRANT USAGE ON SCHEMA auth TO anon,authenticated;
                CREATE SCHEMA private;
                GRANT USAGE ON SCHEMA private TO service_role;
                CREATE TABLE public.portfolios(
                    id uuid PRIMARY KEY,user_id uuid REFERENCES auth.users(id),
                    name text,base_currency text,archived_at timestamptz);
                CREATE TABLE public.portfolio_cash_accounts(
                    portfolio_id uuid REFERENCES public.portfolios(id),currency text,
                    current_balance numeric,PRIMARY KEY(portfolio_id,currency));
                CREATE TABLE public.portfolio_snapshots(
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    portfolio_id uuid REFERENCES public.portfolios(id),
                    snapshot_date date,base_currency text,total_value_base numeric,
                    cash_value_base numeric,holdings_value_base numeric,total_cost_base numeric,
                    realized_pnl_base numeric,unrealized_pnl_base numeric,absolute_pnl_base numeric,
                    valuation_status text,calculation_version integer,captured_at timestamptz,
                    warnings jsonb DEFAULT '[]'::jsonb,UNIQUE(portfolio_id,snapshot_date));
                -- Boundary double only: the unchanged current-day builder is
                -- outside this resolver contract. Historical reads use real rows.
                CREATE FUNCTION private.build_portfolio_snapshot(p_portfolio_id uuid,p_snapshot_date date)
                RETURNS jsonb LANGUAGE sql AS $$ SELECT jsonb_build_object(
                    'delegated_portfolio_id',p_portfolio_id,'snapshot_date',p_snapshot_date,
                    'valuation_status','success') $$;
            """, prepare=False)
            # The production prerequisite shape is explicit. Do not depend on
            # unrelated, locally uncommitted migrations to test this gateway.
            files = sorted((ROOT / "supabase/migrations").glob("*.sql"))
            assert len(files) == len({p.name.split("_")[0] for p in files})
            conn.execute(
                (ROOT / "supabase/migrations/20261121000000_daily_snapshot_recorded_state.sql").read_text(),
                prepare=False,
            )
        yield dict(host="127.0.0.1", port=port, user="postgres", dbname="postgres")
    finally:
        subprocess.run(
            [str(binaries / "pg_ctl"), "-D", str(root / "db"), "-m", "fast", "-w", "stop"],
            check=True, capture_output=True,
        )


@pytest.fixture
def snapshot_db(snapshot_cluster):
    with CONNECT(**snapshot_cluster) as conn:
        user, portfolio = uuid4(), uuid4()
        conn.execute("INSERT INTO auth.users VALUES (%s)", (user,))
        conn.execute("""INSERT INTO public.portfolios(id,user_id,name,base_currency)
                        VALUES (%s,%s,'Snapshot acceptance','TRY')""", (portfolio, user))
        today = conn.execute("SELECT (now() AT TIME ZONE 'Europe/Istanbul')::date").fetchone()[0]
        yield conn, portfolio, today
        conn.rollback()


def resolve(conn, portfolio, day):
    return conn.execute("SELECT private.resolve_daily_portfolio_snapshot(%s,%s)",
                        (portfolio, day)).fetchone()[0]


def test_yesterday_reuses_recorded_values_despite_new_cash_and_missing_live_prices(snapshot_db):
    conn, portfolio, today = snapshot_db
    yesterday = today - timedelta(days=1)
    captured = datetime.combine(yesterday, datetime.min.time(), ZoneInfo("Europe/Istanbul")) + timedelta(hours=23, minutes=55)
    conn.execute("""INSERT INTO public.portfolio_cash_accounts(portfolio_id,currency,current_balance)
                    VALUES (%s,'TRY',12000)""", (portfolio,))
    snapshot_id = conn.execute("""
        INSERT INTO public.portfolio_snapshots(portfolio_id,snapshot_date,base_currency,
            total_value_base,cash_value_base,holdings_value_base,total_cost_base,
            realized_pnl_base,unrealized_pnl_base,absolute_pnl_base,valuation_status,
            calculation_version,captured_at)
        VALUES(%s,%s,'TRY',10000,9000,1000,800,0,200,200,'success',2,%s) RETURNING id
    """, (portfolio, yesterday, captured)).fetchone()[0]
    before = conn.execute("SELECT to_jsonb(s) FROM public.portfolio_snapshots s WHERE id=%s", (snapshot_id,)).fetchone()[0]
    # A broken builder proves the historical path never attempts recalculation.
    conn.execute("""CREATE OR REPLACE FUNCTION private.build_portfolio_snapshot(p_portfolio_id uuid,p_snapshot_date date)
        RETURNS jsonb LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'must not rebuild history'; END $$""")
    for _ in range(2):
        result = resolve(conn, portfolio, yesterday)
        assert result["reused"] is True
        assert result["valuation_status"] == "success"
        assert float(result["total_value_base"]) == 10000
    after = conn.execute("SELECT to_jsonb(s) FROM public.portfolio_snapshots s WHERE id=%s", (snapshot_id,)).fetchone()[0]
    assert before == after
    assert conn.execute("SELECT count(*) FROM public.portfolio_snapshots WHERE portfolio_id=%s", (portfolio,)).fetchone()[0] == 1


def test_missing_history_returns_warning_without_backfill(snapshot_db):
    conn, portfolio, today = snapshot_db
    result = resolve(conn, portfolio, today - timedelta(days=1))
    assert result["valuation_status"] == "missing"
    assert result["warnings"] == ["portfolio_snapshot_not_captured"]
    assert conn.execute("SELECT count(*) FROM public.portfolio_snapshots WHERE portfolio_id=%s", (portfolio,)).fetchone()[0] == 0


@pytest.mark.parametrize("captured_day_offset,version", [(0, 2), (None, 2), (-1, 1)])
def test_mislabeled_or_legacy_snapshot_is_not_reused(snapshot_db, captured_day_offset, version):
    conn, portfolio, today = snapshot_db
    yesterday = today - timedelta(days=1)
    captured = None if captured_day_offset is None else datetime.combine(
        today + timedelta(days=captured_day_offset), datetime.min.time(), ZoneInfo("Europe/Istanbul")
    )
    conn.execute("""INSERT INTO public.portfolio_snapshots(portfolio_id,snapshot_date,base_currency,
        total_value_base,cash_value_base,holdings_value_base,total_cost_base,realized_pnl_base,
        unrealized_pnl_base,absolute_pnl_base,valuation_status,calculation_version,captured_at)
        VALUES(%s,%s,'TRY',100,100,0,0,0,0,0,'success',%s,%s)""", (portfolio,yesterday,version,captured))
    assert resolve(conn, portfolio, yesterday)["valuation_status"] == "missing"
    assert conn.execute("SELECT total_value_base FROM public.portfolio_snapshots WHERE portfolio_id=%s", (portfolio,)).fetchone()[0] == 100


def test_recorded_partial_and_archived_portfolio_keep_their_semantics(snapshot_db):
    conn, portfolio, today = snapshot_db
    yesterday = today - timedelta(days=1)
    captured = datetime.combine(yesterday, datetime.min.time(), ZoneInfo("Europe/Istanbul")) + timedelta(hours=23)
    conn.execute("""INSERT INTO public.portfolio_snapshots(portfolio_id,snapshot_date,base_currency,
        total_value_base,cash_value_base,holdings_value_base,total_cost_base,realized_pnl_base,
        unrealized_pnl_base,absolute_pnl_base,valuation_status,calculation_version,captured_at,warnings)
        VALUES(%s,%s,'TRY',100,100,0,0,0,0,0,'partial',2,%s,'["stale price"]'::jsonb)""", (portfolio,yesterday,captured))
    result = resolve(conn, portfolio, yesterday)
    assert result["reused"] is True and result["valuation_status"] == "partial"
    assert result["warnings"] == ["stale price"]
    conn.execute("UPDATE public.portfolios SET archived_at=now() WHERE id=%s", (portfolio,))
    with pytest.raises(psycopg.errors.RaiseException, match="portfolio_snapshot_portfolio_not_found"), conn.transaction():
        resolve(conn, portfolio, yesterday)


def test_current_day_delegates_to_existing_builder(snapshot_db):
    conn, portfolio, today = snapshot_db
    conn.execute("""INSERT INTO public.portfolio_cash_accounts(portfolio_id,currency,current_balance)
                    VALUES (%s,'TRY',12000)""", (portfolio,))
    result = resolve(conn, portfolio, today)
    assert result["reused"] is False
    assert result["delegated_portfolio_id"] == str(portfolio)
    assert result["snapshot_date"] == today.isoformat()
    assert resolve(conn, portfolio, today) == result


def test_future_and_null_dates_fail_closed(snapshot_db):
    conn, portfolio, today = snapshot_db
    for day in (None, today + timedelta(days=1)):
        with pytest.raises(psycopg.errors.RaiseException, match="portfolio_invalid_snapshot_date"), conn.transaction():
            resolve(conn, portfolio, day)


def test_mobile_roles_cannot_execute_and_service_role_can(snapshot_db):
    conn, portfolio, today = snapshot_db
    for role in ("anon", "authenticated"):
        assert conn.execute("SELECT has_function_privilege(%s,'private.resolve_daily_portfolio_snapshot(uuid,date)','EXECUTE')", (role,)).fetchone()[0] is False
    with conn.transaction():
        conn.execute("SET LOCAL ROLE service_role")
        assert resolve(conn, portfolio, today - timedelta(days=1))["valuation_status"] == "missing"
