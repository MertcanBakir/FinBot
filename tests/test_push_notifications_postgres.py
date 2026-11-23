"""Opt-in, disposable loopback PostgreSQL tests. Never accept a production DSN.

RUN_PUSH_POSTGRES=1 venv/bin/pytest -q tests/test_push_notifications_postgres.py
Requires local initdb/pg_ctl (TEST_PG_BIN overrides the Homebrew default).
"""

import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.repositories.push_notifications_repository import PushRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PUSH_POSTGRES") != "1", reason="Opt-in disposable PostgreSQL"
)
ROOT = Path(__file__).parents[1]
_connect = psycopg.connect


def connect_local(dsn, **kwargs):
    # Keep global test network/DB guards intact; this helper only accepts the
    # ephemeral cluster created below, and never reads application credentials.
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    assert info["host"] == "127.0.0.1" and "hostaddr" not in info
    return _connect(dsn, **kwargs)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    binaries = Path(
        os.environ.get("TEST_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin")
    )
    assert (binaries / "initdb").is_file(), "Install PostgreSQL or set TEST_PG_BIN"
    root = tmp_path_factory.mktemp("push-postgres")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run(
        [
            str(binaries / "initdb"),
            "-D",
            str(root / "db"),
            "-U",
            "postgres",
            "-A",
            "trust",
            "--no-locale",
            "-E",
            "UTF8",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(binaries / "pg_ctl"),
            "-D",
            str(root / "db"),
            "-l",
            str(root / "log"),
            "-o",
            f"-h 127.0.0.1 -p {port} -c unix_socket_directories=''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        with connect_local(dsn, autocommit=True) as conn:
            conn.execute(
                """CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN; CREATE ROLE service_role NOLOGIN;
              CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY);
              CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
              $$ SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
              GRANT USAGE ON SCHEMA auth TO anon,authenticated;
              CREATE SCHEMA storage;
              CREATE TABLE storage.buckets(id text PRIMARY KEY,name text,public boolean,file_size_limit bigint,allowed_mime_types text[]);
              CREATE TABLE storage.objects(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),bucket_id text,name text,created_at timestamptz DEFAULT now());
              ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
              CREATE SCHEMA cron;
              CREATE TABLE cron.job(jobid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,jobname text UNIQUE,schedule text,command text,active boolean NOT NULL DEFAULT true);
              CREATE FUNCTION cron.schedule(text,text,text) RETURNS bigint LANGUAGE plpgsql AS $$
              DECLARE result bigint; BEGIN INSERT INTO cron.job(jobname,schedule,command) VALUES($1,$2,$3) RETURNING jobid INTO result; RETURN result; END $$;
              CREATE FUNCTION cron.alter_job(job_id bigint,schedule text DEFAULT NULL,command text DEFAULT NULL,database text DEFAULT NULL,username text DEFAULT NULL,active boolean DEFAULT NULL) RETURNS void LANGUAGE plpgsql AS $$
              BEGIN UPDATE cron.job AS j SET schedule=coalesce($2,j.schedule),command=coalesce($3,j.command),active=coalesce($6,j.active) WHERE j.jobid=$1; END $$;
              CREATE SCHEMA net;
              CREATE FUNCTION net.http_post(url text,body jsonb,headers jsonb,params jsonb DEFAULT NULL,timeout_milliseconds integer DEFAULT NULL) RETURNS bigint LANGUAGE sql AS $$ SELECT 1::bigint $$;
              CREATE SCHEMA vault;
              CREATE TABLE vault.decrypted_secrets(name text PRIMARY KEY,decrypted_secret text);""",
                prepare=False,
            )
            files = sorted((ROOT / "supabase/migrations").glob("*.sql"))
            assert len(files) == len({p.name.split("_")[0] for p in files})
            for file in files:
                if file.name.startswith(("202610", "20261102", "20261119")):
                    # Analysis and separate portfolio redesigns are not notification dependencies.
                    continue
                migration_sql = file.read_text()
                if file.name == "20261123202009_push_notifications_edge_worker.sql":
                    # This disposable cluster mocks net.http_post; the
                    # pg_net extension itself is available only on Supabase.
                    assert "create extension if not exists pg_net;" in migration_sql
                    migration_sql = migration_sql.replace(
                        "create extension if not exists pg_net;", "", 1
                    )
                conn.execute(migration_sql, prepare=False)
        yield dsn
    finally:
        subprocess.run(
            [
                str(binaries / "pg_ctl"),
                "-D",
                str(root / "db"),
                "-m",
                "fast",
                "-w",
                "stop",
            ],
            check=True,
            capture_output=True,
        )


@pytest.fixture
def db(cluster):
    with connect_local(cluster, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(
            "TRUNCATE notifications.deliveries,notifications.outbox,notifications.devices,notifications.preferences CASCADE"
        )
        conn.execute(
            "TRUNCATE public.earnings_events,public.economic_events,public.refresh_runs CASCADE"
        )
        a, b, project = uuid4(), uuid4(), uuid4()
        conn.execute("INSERT INTO auth.users VALUES(%s),(%s)", (a, b))
        yield conn, cluster, a, b, project


def register(conn, user, project, token=None):
    with conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(user),))
        return conn.execute(
            "SELECT public.register_push_device(%s,%s,'production',%s) AS id",
            (uuid4(), project, token or "ExponentPushToken[" + uuid4().hex + "]"),
        ).fetchone()["id"]


def test_rpc_auth_isolation_and_privileges(db):
    conn, _, a, b, project = db
    device = register(conn, a, project)
    with conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(b),))
        conn.execute("SELECT public.unregister_push_device(%s)", (device,))
    assert (
        conn.execute(
            "SELECT count(*) n FROM notifications.devices WHERE id=%s", (device,)
        ).fetchone()["n"]
        == 1
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT * FROM notifications.devices")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SET LOCAL ROLE anon")
        conn.execute("SELECT public.get_notification_preferences()")
    with (
        pytest.raises(
            psycopg.errors.RaiseException, match="notification_auth_required"
        ),
        conn.transaction(),
    ):
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT public.get_notification_preferences()")
    with (
        pytest.raises(
            psycopg.errors.RaiseException, match="notification_invalid_preferences"
        ),
        conn.transaction(),
    ):
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(a),))
        conn.execute(
            "SELECT public.set_notification_preferences('bad',true,true,true,true,ARRAY['TR'],1)"
        )
    rows = conn.execute(
        "SELECT prosecdef,proconfig FROM pg_proc WHERE proname IN ('register_push_device','unregister_push_device','get_notification_preferences','set_notification_preferences')"
    ).fetchall()
    assert len(rows) == 4 and all(
        r["prosecdef"] and 'search_path=""' in r["proconfig"] for r in rows
    )


def test_edge_worker_migration_is_service_role_only_and_cron_starts_disabled(db):
    conn, _, _, _, _ = db
    cron = conn.execute(
        "SELECT jobname,schedule,active FROM cron.job WHERE jobname='finbot-push-notifications-worker'"
    ).fetchone()
    assert cron == {
        "jobname": "finbot-push-notifications-worker",
        "schedule": "*/5 * * * *",
        "active": False,
    }
    signatures = [
        "notification_worker_prepare(timestamp with time zone, uuid, text)",
        "notification_worker_persist(uuid, timestamp with time zone, uuid, text, jsonb)",
        "notification_worker_due(timestamp with time zone, uuid, text)",
        "notification_worker_claim(timestamp with time zone, uuid, text, boolean)",
        "notification_worker_finish(uuid, uuid, timestamp with time zone, text, text, text, uuid, timestamp with time zone, boolean, integer)",
        "notification_worker_start_tracking(timestamp with time zone, integer)",
        "notification_worker_finish_tracking(uuid, timestamp with time zone, text, jsonb, text)",
    ]
    for signature in signatures:
        row = conn.execute(
            """SELECT p.prosecdef,p.proconfig,
              has_function_privilege('anon',p.oid,'EXECUTE') AS anon_exec,
              has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth_exec,
              has_function_privilege('service_role',p.oid,'EXECUTE') AS service_exec,
              EXISTS(SELECT 1 FROM pg_catalog.aclexplode(coalesce(p.proacl,pg_catalog.acldefault('f',p.proowner))) acl WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') AS public_exec
              FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
              WHERE n.nspname='public' AND p.oid=pg_catalog.to_regprocedure(%s)""",
            (signature,),
        ).fetchone()
        assert row is not None
        assert row["prosecdef"] is True
        assert 'search_path=""' in row["proconfig"]
        assert row["anon_exec"] is False
        assert row["auth_exec"] is False
        assert row["service_exec"] is True
        assert row["public_exec"] is False
        definition = conn.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", (signature,)
        ).fetchone()["pg_get_functiondef"]
        assert "request.jwt.claim.role" not in definition

    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute(
            "SELECT public.notification_worker_due(now(),%s,'production')",
            (uuid4(),),
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SET LOCAL ROLE anon")
        conn.execute(
            "SELECT public.notification_worker_due(now(),%s,'production')",
            (uuid4(),),
        )

    with conn.transaction():
        conn.execute("SET LOCAL ROLE service_role")
        assert conn.execute(
            "SELECT public.notification_worker_prepare(now(),%s,'production') AS result",
            (uuid4(),),
        ).fetchone()["result"]["user_count"] == 0


def test_edge_worker_tracking_runs_only_for_due_work_and_finishes(db):
    conn, _, _, _, _ = db
    with conn.transaction():
        conn.execute("SET LOCAL ROLE service_role")
        conn.execute(
            "SELECT set_config('request.jwt.claim.role','service_role',true)"
        )
        run_id = conn.execute(
            "SELECT public.notification_worker_start_tracking(now(),2) AS result"
        ).fetchone()["result"]
        assert conn.execute(
            "SELECT public.notification_worker_finish_tracking(%s,now(),'success','{\"processed_count\":2}'::jsonb,NULL) AS result",
            (run_id,),
        ).fetchone()["result"] is True
    row = conn.execute(
        "SELECT job_name,status,operation_type,scope,metrics->>'prepared_count' prepared_count FROM public.refresh_runs WHERE id=%s",
        (run_id,),
    ).fetchone()
    assert row == {
        "job_name": "push_notifications_batch",
        "status": "success",
        "operation_type": "notification_dispatch",
        "scope": "PUSH",
        "prepared_count": "2",
    }
    assert conn.execute(
        "SELECT count(*) n FROM public.provider_run_results WHERE refresh_run_id=%s",
        (run_id,),
    ).fetchone()["n"] == 0


def test_edge_worker_claim_and_finish_persists_expo_ticket(db):
    conn, _, user, _, project = db
    device_id = register(conn, user, project)
    now = datetime.now(timezone.utc)
    fresh_source(conn, now)
    notice_id = conn.execute(
        """INSERT INTO notifications.outbox(
          user_id,kind,event_date,event_key,title,body,data,scheduled_at,expires_at
        ) VALUES(%s,'earnings_today',%s,'daily','Title','Body',
          '{"count":0,"instrument_ids":[]}'::jsonb,%s,%s) RETURNING id""",
        (user, now.date(), now, now + timedelta(hours=1)),
    ).fetchone()["id"]
    delivery_id = conn.execute(
        "INSERT INTO notifications.deliveries(notification_id,device_id,next_at) VALUES(%s,%s,%s) RETURNING id",
        (notice_id, device_id, now),
    ).fetchone()["id"]

    with conn.transaction():
        conn.execute("SET LOCAL ROLE service_role")
        conn.execute(
            "SELECT set_config('request.jwt.claim.role','service_role',true)"
        )
        item = conn.execute(
            "SELECT public.notification_worker_claim(%s,%s,'production',false) AS result",
            (now, project),
        ).fetchone()["result"]
        assert item["id"] == str(delivery_id)
        assert item["attempts"] == 1
        assert item["token"].startswith("ExponentPushToken[")
        assert conn.execute(
            """SELECT public.notification_worker_finish(
              %s,%s,%s,'ticket',NULL,'expo-ticket-1',%s,%s,false,%s) AS result""",
            (
                delivery_id,
                item["lease_token"],
                now,
                device_id,
                item["device_version"],
                item["attempts"],
            ),
        ).fetchone()["result"] is True

    row = conn.execute(
        "SELECT status,ticket_id,attempts,lease_token,lease_until FROM notifications.deliveries WHERE id=%s",
        (delivery_id,),
    ).fetchone()
    assert row == {
        "status": "ticket",
        "ticket_id": "expo-ticket-1",
        "attempts": 1,
        "lease_token": None,
        "lease_until": None,
    }


def test_edge_worker_prepare_reads_sources_for_each_preference_user(db):
    conn, _, user, _, project = db
    conn.execute("INSERT INTO notifications.preferences(user_id) VALUES(%s)", (user,))
    with conn.transaction():
        conn.execute("SET LOCAL ROLE service_role")
        conn.execute(
            "SELECT set_config('request.jwt.claim.role','service_role',true)"
        )
        result = conn.execute(
            "SELECT public.notification_worker_prepare(%s,%s,'production') AS result",
            (datetime.now(timezone.utc), project),
        ).fetchone()["result"]
    assert result["user_count"] == 1
    prepared_user = result["users"][0]
    assert prepared_user["user_id"] == str(user)
    assert prepared_user["portfolios"] == []
    assert prepared_user["earnings"] == []
    assert prepared_user["macro"] == []
    assert prepared_user["snapshot"] is None
    assert prepared_user["earnings_fresh"] is False


def test_edge_worker_persist_is_idempotent_and_binds_devices(db):
    conn, _, user, _, project = db
    device_id = register(conn, user, project)
    now = datetime.now(timezone.utc)
    item = {
        "kind": "earnings_today",
        "event_key": "daily",
        "title": "Bugünün bilanço takvimi",
        "body": "Bugün açıklama yok.",
        "data": {"event_date": now.date().isoformat(), "count": 0},
        "scheduled_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }
    with conn.transaction():
        conn.execute("SET LOCAL ROLE service_role")
        conn.execute(
            "SELECT set_config('request.jwt.claim.role','service_role',true)"
        )
        for _ in range(2):
            count = conn.execute(
                "SELECT public.notification_worker_persist(%s,%s,%s,'production',%s) AS result",
                (user, now, project, Jsonb([item])),
            ).fetchone()["result"]
            assert count == 1

    assert conn.execute(
        "SELECT count(*) n FROM notifications.outbox WHERE user_id=%s",
        (user,),
    ).fetchone()["n"] == 1
    delivery = conn.execute(
        "SELECT notification_id,device_id,status FROM notifications.deliveries"
    ).fetchone()
    assert delivery["device_id"] == device_id
    assert delivery["status"] == "pending"


def fresh_source(conn, now, status="success"):
    return conn.execute(
        """INSERT INTO public.refresh_runs(job_name,started_at,finished_at,status,coverage_start,coverage_end,metrics)
        VALUES('earnings_refresh_job',%s,%s,%s,%s,%s,'{"database_status":"success"}') RETURNING id""",
        (now, now, status, now.date(), now.date() + timedelta(days=3)),
    ).fetchone()["id"]


def enqueue(conn, user, device, now, kind="earnings_today", key="daily"):
    fresh_source(conn, now)
    row = conn.execute(
        """INSERT INTO notifications.outbox(user_id,kind,event_date,event_key,title,body,data,scheduled_at,expires_at)
      VALUES(%s,%s,%s,%s,'Title','Body','{"count":0,"instrument_ids":[]}',%s,%s) RETURNING id""",
        (user, kind, now.date(), key, now, now + timedelta(hours=2)),
    ).fetchone()
    conn.execute(
        "INSERT INTO notifications.deliveries(notification_id,device_id,next_at) VALUES(%s,%s,%s)",
        (row["id"], device, now),
    )
    return row["id"]


def repository(dsn, project):
    @contextmanager
    def factory():
        with connect_local(dsn) as conn:
            yield conn

    return PushRepository(project, "production", factory)


def test_parallel_claim_spacing_idempotency_and_receipt_fencing(db):
    conn, dsn, a, b, project = db
    device = register(conn, a, project)
    now = datetime.now(timezone.utc)
    enqueue(conn, a, device, now, key="one")
    enqueue(conn, a, device, now, key="two")
    repo = repository(dsn, project)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: repo.claim(now), range(4)))
    item = next(c for c in claims if c)
    assert sum(bool(c) for c in claims) == 1
    repo.finish(item, now, "ticket", ticket="test-ticket")
    assert repo.claim(now + timedelta(minutes=14)) is None
    assert repo.claim(now + timedelta(minutes=15)) is not None
    receipt = repo.claim(now + timedelta(minutes=15), True)
    assert receipt["ticket_id"] == "test-ticket"
    # A receipt for the old registration cannot deactivate a refreshed token.
    conn.execute(
        "UPDATE notifications.devices SET token='ExponentPushToken[rotated12345]',updated_at=updated_at+interval '1 second' WHERE id=%s",
        (device,),
    )
    repo.finish(
        receipt,
        now + timedelta(minutes=15),
        "failed",
        "DeviceNotRegistered",
        disable=True,
    )
    assert conn.execute(
        "SELECT active FROM notifications.devices WHERE id=%s", (device,)
    ).fetchone()["active"]


def test_unknown_send_after_crash_never_replays(db):
    conn, dsn, a, b, project = db
    device = register(conn, a, project)
    now = datetime.now(timezone.utc)
    enqueue(conn, a, device, now)
    repo = repository(dsn, project)
    item = repo.claim(now)
    assert item
    assert repo.claim(now + timedelta(minutes=3)) is None
    assert (
        conn.execute("SELECT status FROM notifications.deliveries").fetchone()["status"]
        == "unknown"
    )
    repo.finish(item, now + timedelta(minutes=3), "ticket", ticket="too-late")
    assert (
        conn.execute("SELECT status FROM notifications.deliveries").fetchone()["status"]
        == "unknown"
    )


def test_preparation_uses_real_empty_source_queries_and_is_repeatable(db):
    conn, dsn, a, b, project = db
    register(conn, a, project)
    repo = repository(dsn, project)
    now = datetime(2026, 11, 20, 7, tzinfo=timezone.utc)
    assert repo.prepare(now) == 0
    assert repo.prepare(now) == 0


def test_real_macro_filter_preparation_replay_and_preferences(db):
    conn, dsn, a, b, project = db
    register(conn, a, project)
    now = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0)
    provider = conn.execute(
        "INSERT INTO public.data_providers(code,name,provider_type) VALUES(%s,'Test','economic') RETURNING id",
        (uuid4().hex,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO public.economic_events(identity_key,provider_id,event_code,name,country,scheduled_at,last_seen_at) VALUES(%s,%s,'TR_CPI','TÜFE','TR',%s,%s)",
        (uuid4().hex, provider, now + timedelta(hours=3), now),
    )
    repo = repository(dsn, project)
    repo.prepare(now)
    repo.prepare(now)
    assert (
        conn.execute(
            "SELECT count(*) n FROM notifications.outbox WHERE user_id=%s", (a,)
        ).fetchone()["n"]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) n FROM notifications.deliveries").fetchone()["n"]
        == 1
    )
    item = repo.claim(now)
    assert item and item["title"] == "TÜFE verisine 180 dakika kaldı"
    repo.finish(item, now, "ticket", ticket="macro-ticket")
    repo.prepare(now)
    assert repo.claim(now) is None
    with conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(a),))
        conn.execute(
            "SELECT public.set_notification_preferences('Europe/Istanbul',true,true,true,true,ARRAY['US'],3)"
        )
    assert conn.execute(
        "SELECT countries FROM notifications.preferences WHERE user_id=%s", (a,)
    ).fetchone()["countries"] == ["US"]


def test_reset_preserves_push_settings_and_financial_sql_regressions(db):
    conn, _, a, b, project = db
    device = register(conn, a, project)
    conn.execute(
        "INSERT INTO public.portfolios(user_id,name,base_currency,is_default) VALUES(%s,'Push test','TRY',true)",
        (a,),
    )
    with conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(a),))
        conn.execute(
            "SELECT public.reset_my_investments(%s,'RESET_ALL_INVESTMENTS')", (uuid4(),)
        )
    assert (
        conn.execute(
            "SELECT count(*) n FROM notifications.devices WHERE id=%s", (device,)
        ).fetchone()["n"]
        == 1
    )
    for name in (
        "portfolio_reset_rpc_integration.sql",
        "portfolio_cash_reversal_snapshot_integration.sql",
        "user_watchlist_rpc_test.sql",
    ):
        conn.execute(
            (ROOT / "tests/sql" / name)
            .read_text()
            .replace("\\set ON_ERROR_STOP on", ""),
            prepare=False,
        )


def test_tracking_commits_dispatch_metrics_without_provider_or_secrets(db):
    conn, dsn, a, b, project = db
    from app.services.push_tracking import tracked_batch

    tracked_batch(
        lambda: dict(processed_count=1, failed=1), repository(dsn, project).factory
    )
    row = conn.execute(
        "SELECT status,metrics,notification_status FROM public.refresh_runs WHERE job_name='push_notifications_batch' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "partial" and row["metrics"] == dict(
        processed_count=1, failed=1
    )
    assert row["notification_status"] == "disabled"


def test_only_open_position_earnings_is_prepared_and_reset_cancels_unsent(db):
    conn, dsn, a, b, project = db
    register(conn, a, project)
    now = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0)
    portfolio = conn.execute(
        "INSERT INTO public.portfolios(user_id,name,base_currency) VALUES(%s,'Earnings','TRY') RETURNING id",
        (a,),
    ).fetchone()["id"]
    instrument = conn.execute(
        "INSERT INTO public.instruments(market,symbol,company_name,is_active,asset_type,currency) VALUES('NASDAQ100',%s,'Test',true,'stock','TRY') RETURNING id",
        (uuid4().hex[:10],),
    ).fetchone()["id"]
    with conn.transaction():
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(a),))
        conn.execute(
            "SELECT public.record_cash_flow(%s,'DEPOSIT',1000,'TRY',%s,%s,null)",
            (portfolio, now - timedelta(days=2), uuid4()),
        )
        conn.execute(
            "SELECT public.record_portfolio_transaction(%s,%s,'BUY',2,100,'TRY',%s,%s,null)",
            (portfolio, instrument, now - timedelta(days=1), uuid4()),
        )
    conn.execute(
        "INSERT INTO public.earnings_events(instrument_id,market,identity_key,earnings_date,session,last_seen_at) VALUES(%s,'NASDAQ100',%s,%s,'after_hours',%s)",
        (instrument, uuid4().hex, now.date(), now),
    )
    repo = repository(dsn, project)
    repo.prepare(now)
    repo.prepare(now)
    rows = conn.execute(
        "SELECT kind FROM notifications.outbox WHERE user_id=%s AND kind='earnings_morning'",
        (a,),
    ).fetchall()
    assert len(rows) == 1
    with conn.transaction():
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(a),))
        conn.execute(
            "SELECT public.reset_my_investments(%s,'RESET_ALL_INVESTMENTS')", (uuid4(),)
        )
    repo.prepare(now)
    assert (
        conn.execute(
            "SELECT count(*) n FROM notifications.deliveries d JOIN notifications.outbox o ON o.id=d.notification_id WHERE o.user_id=%s AND o.kind='earnings_morning' AND d.status='pending'",
            (a,),
        ).fetchone()["n"]
        == 0
    )


def test_midnight_builds_real_cash_snapshots_and_deposit_is_not_profit(db):
    from zoneinfo import ZoneInfo

    conn, dsn, a, b, project = db
    register(conn, a, project)
    local = datetime.now(ZoneInfo("Europe/Istanbul")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    now = local.astimezone(timezone.utc)
    portfolio = conn.execute(
        "INSERT INTO public.portfolios(user_id,name,base_currency,created_at) VALUES(%s,'Cash','TRY',%s) RETURNING id",
        (a, now - timedelta(days=5)),
    ).fetchone()["id"]
    # Represent accounts created with this historical portfolio. The INSERT
    # trigger otherwise timestamps their initialization at today's test time.
    conn.execute(
        "UPDATE public.portfolio_cash_accounts SET initialized_at=%s WHERE portfolio_id=%s",
        (now - timedelta(days=5), portfolio),
    )
    with conn.transaction():
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(a),))
        conn.execute(
            "SELECT public.record_cash_flow(%s,'DEPOSIT',1000,'TRY',%s,%s,null)",
            (portfolio, now - timedelta(days=3), uuid4()),
        )
        conn.execute(
            "SELECT private.build_portfolio_snapshot(%s,%s)",
            (portfolio, local.date() - timedelta(days=2)),
        )
        conn.execute(
            "SELECT public.record_cash_flow(%s,'DEPOSIT',100,'TRY',%s,%s,null)",
            (portfolio, now - timedelta(hours=12), uuid4()),
        )
        conn.execute(
            "SELECT private.build_portfolio_snapshot(%s,%s)",
            (portfolio, local.date() - timedelta(days=1)),
        )
    repo = repository(dsn, project)
    repo.prepare(now)
    row = conn.execute(
        "SELECT body,data FROM notifications.outbox WHERE user_id=%s AND kind='daily_portfolio'",
        (a,),
    ).fetchone()
    assert row, conn.execute(
        "SELECT snapshot_date,valuation_status,warnings,calculation_version,total_value_base FROM public.portfolio_snapshots WHERE portfolio_id=%s",
        (portfolio,),
    ).fetchall()
    assert "+0.00 TRY" in row["body"] and "1100.00 TRY" not in row["body"]
    assert (
        row["data"]["valuation_date"] == (local.date() - timedelta(days=1)).isoformat()
    )


@pytest.fixture(scope="module")
def production_cluster(cluster):
    with connect_local(cluster, autocommit=True) as conn:
        # Exercise the production column-removal contract without importing
        # unrelated analysis/portfolio migrations into this feature's release.
        conn.execute(
            "ALTER TABLE public.portfolio_snapshots DROP COLUMN external_cash_flow_base CASCADE"
        )
    return cluster


def test_current_production_schema_prepares_without_removed_cash_fields(
    production_cluster,
):
    from zoneinfo import ZoneInfo

    with connect_local(
        production_cluster, autocommit=True, row_factory=dict_row
    ) as conn:
        user, project, portfolio = uuid4(), uuid4(), uuid4()
        conn.execute("INSERT INTO auth.users VALUES(%s)", (user,))
        register(conn, user, project)
        conn.execute(
            "INSERT INTO public.portfolios(id,user_id,name,base_currency) VALUES(%s,%s,'Current model','TRY')",
            (portfolio, user),
        )
        day = datetime.now(ZoneInfo("Europe/Istanbul")).date()
        now = datetime.combine(
            day, datetime.min.time(), ZoneInfo("Europe/Istanbul")
        ).astimezone(timezone.utc)
        for offset, value, pnl in [(2, 1000, 25), (1, 1500, 35)]:
            conn.execute(
                """INSERT INTO public.portfolio_snapshots(portfolio_id,snapshot_date,base_currency,total_value_base,
                cash_value_base,holdings_value_base,total_cost_base,realized_pnl_base,unrealized_pnl_base,absolute_pnl_base,
                net_contributions_base,valuation_status,calculation_version) VALUES(%s,%s,'TRY',%s,%s,0,0,0,%s,%s,0,'success',2)""",
                (portfolio, day - timedelta(days=offset), value, value, pnl, pnl),
            )
        repo = repository(production_cluster, project)
        repo.prepare(now)
        row = conn.execute(
            "SELECT body FROM notifications.outbox WHERE user_id=%s AND kind='daily_portfolio'",
            (user,),
        ).fetchone()
        assert row and "+10.00 TRY" in row["body"] and "1500.00 TRY" not in row["body"]
        assert (
            conn.execute(
                "SELECT count(*) n FROM information_schema.columns WHERE table_schema='public' AND table_name='portfolio_snapshots' AND column_name='external_cash_flow_base'"
            ).fetchone()["n"]
            == 0
        )


def test_v2_preferences_seven_switches_legacy_alias_and_user_isolation(db):
    conn, _, a, b, project = db
    register(conn, a, project)
    register(conn, b, project)
    with conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(a),))
        conn.execute(
            "SELECT public.set_notification_preferences_v2('Europe/Istanbul',true,false,true,false,false,true,false,ARRAY['TR'],3)"
        )
        p = conn.execute(
            "SELECT public.get_notification_preferences_v2() p"
        ).fetchone()["p"]
        assert len(p) == 10 and "macro_morning" not in p and "user_id" not in p
        assert not p["economic_reminders"] and not p["watchlist_earnings"]
        old = conn.execute("SELECT public.get_notification_preferences() p").fetchone()[
            "p"
        ]
        assert len(old) == 7 and old["macro_morning"] is False
        conn.execute(
            "SELECT public.set_notification_preferences('Europe/Istanbul',true,true,true,true,ARRAY['US'],1)"
        )
        p = conn.execute(
            "SELECT public.get_notification_preferences_v2() p"
        ).fetchone()["p"]
        assert (
            p["economic_reminders"]
            and not p["watchlist_earnings"]
            and not p["earnings_tomorrow"]
        )
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(b),))
        assert conn.execute(
            "SELECT public.get_notification_preferences_v2() p"
        ).fetchone()["p"]["watchlist_earnings"]
    for signature in [
        "public.get_notification_preferences_v2()",
        "public.set_notification_preferences_v2(text,boolean,boolean,boolean,boolean,boolean,boolean,boolean,text[],integer)",
    ]:
        r = conn.execute(
            "SELECT has_function_privilege('anon',%s,'EXECUTE') a,has_function_privilege('authenticated',%s,'EXECUTE') b",
            (signature, signature),
        ).fetchone()
        assert not r["a"] and r["b"]
    for call in [
        "public.get_notification_preferences_v2()",
        "public.set_notification_preferences_v2('Europe/Istanbul',true,true,true,true,true,true,true,ARRAY['TR'],1)",
    ]:
        with (
            pytest.raises(
                psycopg.errors.RaiseException, match="notification_auth_required"
            ),
            conn.transaction(),
        ):
            conn.execute("SET LOCAL ROLE authenticated")
            conn.execute("SELECT " + call)
    with (
        pytest.raises(
            psycopg.errors.RaiseException, match="notification_invalid_preferences"
        ),
        conn.transaction(),
    ):
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(a),))
        conn.execute(
            "SELECT public.set_notification_preferences_v2('Europe/Istanbul',true,true,true,NULL,true,true,true,ARRAY['TR'],1)"
        )


@pytest.mark.parametrize(
    "status,age,expected",
    [("success", 1, 2), ("partial", 1, 0), ("failed", 1, 0), ("success", 40, 0)],
)
def test_fresh_empty_summaries_and_send_time_failure_guard(db, status, age, expected):
    from zoneinfo import ZoneInfo

    conn, dsn, a, b, project = db
    register(conn, a, project)
    now = datetime.now(ZoneInfo("Europe/Istanbul")).replace(
        hour=10, minute=15, second=0, microsecond=0
    )
    conn.execute(
        "INSERT INTO public.portfolios(user_id,name,base_currency) VALUES(%s,'Empty','TRY')",
        (a,),
    )
    fresh_source(conn, now - timedelta(hours=age), status)
    repo = repository(dsn, project)
    repo.prepare(now)
    rows = conn.execute(
        "SELECT kind,data FROM notifications.outbox WHERE user_id=%s ORDER BY kind",
        (a,),
    ).fetchall()
    assert len(rows) == expected
    if expected:
        assert all(r["data"]["count"] == 0 for r in rows)
        # A later failed refresh invalidates even an already prepared empty claim.
        fresh_source(conn, now, "failed")
        assert repo.claim(now) is None


def test_economic_slots_reschedule_filter_recheck_and_no_spacing(db):
    conn, dsn, a, b, project = db
    register(conn, a, project)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    provider = conn.execute(
        "INSERT INTO public.data_providers(code,name,provider_type) VALUES(%s,'Test','economic') RETURNING id",
        (uuid4().hex,),
    ).fetchone()["id"]
    events = []
    for code in ["TR_CPI", "US_CPI"]:
        events.append(
            conn.execute(
                "INSERT INTO public.economic_events(identity_key,provider_id,event_code,name,country,scheduled_at,last_seen_at) VALUES(%s,%s,%s,'CPI',%s,%s,%s) RETURNING id",
                (uuid4().hex, provider, code, code[:2], now + timedelta(hours=3), now),
            ).fetchone()["id"]
        )
    repo = repository(dsn, project)
    repo.prepare(now)
    assert repo.claim(now) and repo.claim(
        now
    )  # distinct simultaneous events do not throttle each other
    # Next slot is independent of the first attempt and is never replayed late.
    next_slot = now + timedelta(hours=2)
    repo.prepare(next_slot + timedelta(minutes=6))
    assert repo.claim(next_slot + timedelta(minutes=6)) is None
    conn.execute(
        "UPDATE public.economic_events SET scheduled_at=%s WHERE id=%s",
        (next_slot + timedelta(hours=3), events[0]),
    )
    repo.prepare(next_slot)
    conn.execute(
        "UPDATE notifications.preferences SET countries=ARRAY['US'] WHERE user_id=%s",
        (a,),
    )
    item = repo.claim(next_slot)
    assert item and item["data"]["event_id"] == str(events[1])
    assert repo.claim(next_slot) is None


def test_watchlist_general_tomorrow_and_holdings_are_independent(db):
    from zoneinfo import ZoneInfo

    conn, dsn, a, b, project = db
    register(conn, a, project)
    register(conn, b, project)
    now = datetime.now(ZoneInfo("Europe/Istanbul")).replace(
        hour=14, minute=30, second=0, microsecond=0
    )
    instrument = conn.execute(
        "INSERT INTO public.instruments(market,symbol,company_name,is_active,asset_type,currency) VALUES('NASDAQ100',%s,'Watched',true,'stock','USD') RETURNING id",
        (uuid4().hex[:10],),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO public.user_watchlist_items(user_id,instrument_id) VALUES(%s,%s)",
        (a, instrument),
    )
    for day in [now.date(), now.date() + timedelta(days=1)]:
        conn.execute(
            "INSERT INTO public.earnings_events(instrument_id,market,identity_key,earnings_date,session,last_seen_at) VALUES(%s,'NASDAQ100',%s,%s,'after_hours',%s)",
            (instrument, uuid4().hex, day, now),
        )
    repo = repository(dsn, project)
    repo.prepare(now)
    rows = conn.execute(
        "SELECT user_id,kind,data FROM notifications.outbox ORDER BY user_id,kind"
    ).fetchall()
    assert [(r["user_id"], r["kind"]) for r in rows] == [(a, "watchlist_earnings")]
    assert repo.claim(now)["kind"] == "watchlist_earnings"
    # General tomorrow digest applies to both users, without exposing ownership.
    late = now.replace(hour=20)
    repo.prepare(late)
    assert (
        conn.execute(
            "SELECT count(*) n FROM notifications.outbox WHERE kind='earnings_tomorrow'"
        ).fetchone()["n"]
        == 2
    )
    conn.execute("DELETE FROM public.user_watchlist_items WHERE user_id=%s", (a,))
    assert not conn.execute(
        "SELECT notifications.notice_eligible(id,%s) ok FROM notifications.outbox WHERE kind='watchlist_earnings'",
        (now,),
    ).fetchone()["ok"]


def test_empty_portfolio_digest_rechecks_archive_before_claim(db):
    from zoneinfo import ZoneInfo

    conn, dsn, a, b, project = db
    register(conn, a, project)
    now = datetime.now(ZoneInfo("Europe/Istanbul")).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    conn.execute(
        "INSERT INTO public.portfolios(user_id,name,base_currency) VALUES(%s,'Empty','TRY')",
        (a,),
    )
    fresh_source(conn, now - timedelta(hours=1))
    repo = repository(dsn, project)
    repo.prepare(now)
    assert (
        conn.execute(
            "SELECT count(*) n FROM notifications.outbox WHERE kind='earnings_morning'"
        ).fetchone()["n"]
        == 1
    )
    conn.execute(
        "UPDATE public.portfolios SET archived_at=%s WHERE user_id=%s", (now, a)
    )
    assert repo.claim(now) is None


def test_digest_claim_rejects_same_count_replacement_after_twentieth_id(db):
    from zoneinfo import ZoneInfo

    conn, dsn, a, b, project = db
    register(conn, a, project)
    now = datetime.now(ZoneInfo("Europe/Istanbul")).replace(
        hour=10, minute=15, second=0, microsecond=0
    )
    instruments = []
    for index in range(21):
        instrument = conn.execute(
            "INSERT INTO public.instruments(market,symbol,company_name,is_active,asset_type,currency) VALUES('NASDAQ100',%s,'Company',true,'stock','USD') RETURNING id",
            (f"A{index:02}" + uuid4().hex[:5],),
        ).fetchone()["id"]
        instruments.append(instrument)
        conn.execute(
            "INSERT INTO public.earnings_events(instrument_id,market,identity_key,earnings_date,session,last_seen_at) VALUES(%s,'NASDAQ100',%s,%s,'after_hours',%s)",
            (instrument, uuid4().hex, now.date(), now),
        )
    repo = repository(dsn, project)
    repo.prepare(now)
    assert conn.execute(
        "SELECT notifications.notice_eligible(id,%s) ok FROM notifications.outbox",
        (now,),
    ).fetchone()["ok"]
    # Last alphabetic symbol is in the visible body but beyond mobile routing IDs.
    conn.execute(
        "UPDATE public.instruments SET symbol=%s WHERE id=%s",
        ("ZZZ" + uuid4().hex[:5], instruments[-1]),
    )
    assert repo.claim(now) is None


@pytest.mark.parametrize("change", ["archive", "second_portfolio"])
def test_daily_rechecks_single_unarchived_portfolio_at_claim(db, change):
    conn, dsn, a, b, project = db
    device = register(conn, a, project)
    now = datetime.now(timezone.utc)
    portfolio = conn.execute(
        "INSERT INTO public.portfolios(user_id,name,base_currency) VALUES(%s,'Daily','TRY') RETURNING id",
        (a,),
    ).fetchone()["id"]
    snapshot = conn.execute(
        """INSERT INTO public.portfolio_snapshots(portfolio_id,snapshot_date,base_currency,
        total_value_base,cash_value_base,holdings_value_base,total_cost_base,realized_pnl_base,
        unrealized_pnl_base,absolute_pnl_base,net_contributions_base,valuation_status,calculation_version)
        VALUES(%s,%s,'TRY',10,10,0,0,0,0,0,0,'success',2) RETURNING id""",
        (portfolio, now.date()),
    ).fetchone()["id"]
    notice = enqueue(conn, a, device, now, kind="daily_portfolio")
    conn.execute(
        "UPDATE notifications.outbox SET data=jsonb_build_object('snapshot_id',%s::text) WHERE id=%s",
        (snapshot, notice),
    )
    assert conn.execute(
        "SELECT notifications.notice_eligible(%s,%s) ok", (notice, now)
    ).fetchone()["ok"]
    if change == "archive":
        conn.execute(
            "UPDATE public.portfolios SET archived_at=%s WHERE id=%s", (now, portfolio)
        )
    else:
        conn.execute(
            "INSERT INTO public.portfolios(user_id,name,base_currency) VALUES(%s,'Second','TRY')",
            (a,),
        )
    assert repository(dsn, project).claim(now) is None
