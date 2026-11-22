\set ON_ERROR_STOP on

BEGIN;

-- Setup test user
INSERT INTO auth.users (id) VALUES
    ('00000000-0000-0000-0000-000000000001')
ON CONFLICT (id) DO NOTHING;

-- 1. Metadata Verification on pg_proc (SECURITY DEFINER, owner = postgres, search_path = pg_catalog, no-arg overload)
DO $$
DECLARE
    v_prosecdef boolean;
    v_owner text;
    v_config text[];
    v_overload_count integer;
    v_public_execute_count integer;
BEGIN
    SELECT count(*) INTO v_overload_count
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname = 'get_mobile_system_health';

    IF v_overload_count <> 1 THEN
        RAISE EXCEPTION 'Metadata Failed: Expected exactly 1 overload for get_mobile_system_health, found %', v_overload_count;
    END IF;

    SELECT p.prosecdef, r.rolname, p.proconfig
    INTO v_prosecdef, v_owner, v_config
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_catalog.pg_roles r ON r.oid = p.proowner
    WHERE n.nspname = 'public' AND p.proname = 'get_mobile_system_health';

    IF NOT v_prosecdef THEN
        RAISE EXCEPTION 'Metadata Failed: get_mobile_system_health is not SECURITY DEFINER';
    END IF;

    IF v_owner <> 'postgres' THEN
        RAISE EXCEPTION 'Metadata Failed: Owner is %, expected postgres', v_owner;
    END IF;

    IF NOT (ARRAY['search_path=pg_catalog'] <@ v_config) THEN
        RAISE EXCEPTION 'Metadata Failed: search_path is not pg_catalog: %', v_config;
    END IF;

    -- Verify PUBLIC has no EXECUTE permission via aclexplode (grantee = 0 is PUBLIC)
    SELECT count(*) INTO v_public_execute_count
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace,
    LATERAL pg_catalog.aclexplode(p.proacl) a
    WHERE n.nspname = 'public'
      AND p.proname = 'get_mobile_system_health'
      AND a.grantee = 0
      AND a.privilege_type = 'EXECUTE';

    IF v_public_execute_count > 0 THEN
        RAISE EXCEPTION 'Privilege Failed: PUBLIC has EXECUTE ACL entry on get_mobile_system_health';
    END IF;

    -- Verify role-level has_function_privilege
    IF has_function_privilege('anon', 'public.get_mobile_system_health()', 'execute') THEN
        RAISE EXCEPTION 'Privilege Failed: anon has execute privilege on get_mobile_system_health()';
    END IF;

    IF NOT has_function_privilege('authenticated', 'public.get_mobile_system_health()', 'execute') THEN
        RAISE EXCEPTION 'Privilege Failed: authenticated does not have execute privilege on get_mobile_system_health()';
    END IF;
END $$;

-- 2. Verify Return Columns & Types from PostgreSQL Catalog
DO $$
DECLARE
    v_col_count integer;
    v_forbidden_count integer;
    r record;
    v_expected text[] := ARRAY[
        'job_code:text',
        'category:text',
        'display_name:text',
        'sort_order:integer',
        'execution_status:text',
        'health_status:text',
        'last_started_at:timestamp with time zone',
        'last_finished_at:timestamp with time zone',
        'last_success_at:timestamp with time zone',
        'duration_seconds:numeric',
        'is_overdue:boolean',
        'message_code:text',
        'checked_at:timestamp with time zone'
    ];
    v_actual text[];
BEGIN
    SELECT array_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod) ORDER BY a.attnum)
    INTO v_actual
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_catalog.pg_type t ON t.oid = p.prorettype
    JOIN pg_catalog.pg_attribute a ON a.attrelid = t.typrelid
    WHERE n.nspname = 'public'
      AND p.proname = 'get_mobile_system_health'
      AND a.attnum > 0
      AND NOT a.attisdropped;

    IF v_actual <> v_expected THEN
        RAISE EXCEPTION 'Catalog Contract Failed! Expected: %, Got: %', v_expected, v_actual;
    END IF;

    -- Verify forbidden column names are absent
    SELECT count(*) INTO v_forbidden_count
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_catalog.pg_type t ON t.oid = p.prorettype
    JOIN pg_catalog.pg_attribute a ON a.attrelid = t.typrelid
    WHERE n.nspname = 'public'
      AND p.proname = 'get_mobile_system_health'
      AND a.attname IN (
          'job_name', 'last_status', 'last_sync_action', 'last_warnings',
          'last_error', 'fresh_event_count', 'final_event_count',
          'github_run_id', 'notification_status'
      );

    IF v_forbidden_count > 0 THEN
        RAISE EXCEPTION 'Catalog Contract Failed: Forbidden columns present in return type: count %', v_forbidden_count;
    END IF;
END $$;

-- 3. Real Role Switching: Execution and Direct Table Access Denial
DO $$
DECLARE
    v_caught_anon boolean := false;
    v_caught_auth_no_uid boolean := false;
    v_caught_anon_select_rr boolean := false;
    v_caught_anon_select_prr boolean := false;
    v_caught_auth_select_rr boolean := false;
    v_caught_auth_select_prr boolean := false;
    v_auth_rows integer := 0;
BEGIN
    -- 3a. anon role execution denial
    SET LOCAL ROLE anon;
    BEGIN
        PERFORM * FROM public.get_mobile_system_health();
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught_anon := true;
    END;

    -- 3b. anon role direct SELECT denial
    BEGIN
        PERFORM count(*) FROM public.refresh_runs;
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught_anon_select_rr := true;
    END;

    BEGIN
        PERFORM count(*) FROM public.provider_run_results;
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught_anon_select_prr := true;
    END;

    RESET ROLE;

    IF NOT v_caught_anon THEN
        RAISE EXCEPTION 'Role Test Failed: anon was able to call get_mobile_system_health()';
    END IF;
    IF NOT v_caught_anon_select_rr OR NOT v_caught_anon_select_prr THEN
        RAISE EXCEPTION 'Role Test Failed: anon was able to directly SELECT operational tables';
    END IF;

    -- 3c. authenticated role with empty UID -> authentication_required
    SET LOCAL ROLE authenticated;
    PERFORM set_config('request.jwt.claim.sub', '', true);
    BEGIN
        PERFORM * FROM public.get_mobile_system_health();
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM = 'authentication_required' THEN
            v_caught_auth_no_uid := true;
        END IF;
    END;

    -- 3d. authenticated role direct SELECT denial
    BEGIN
        PERFORM count(*) FROM public.refresh_runs;
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught_auth_select_rr := true;
    END;

    BEGIN
        PERFORM count(*) FROM public.provider_run_results;
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught_auth_select_prr := true;
    END;

    -- 3e. authenticated role with valid UID -> success
    PERFORM set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-000000000001', true);
    SELECT count(*) INTO v_auth_rows FROM public.get_mobile_system_health();

    RESET ROLE;

    IF NOT v_caught_auth_no_uid THEN
        RAISE EXCEPTION 'Role Test Failed: authenticated with no UID did not raise authentication_required';
    END IF;
    IF NOT v_caught_auth_select_rr OR NOT v_caught_auth_select_prr THEN
        RAISE EXCEPTION 'Role Test Failed: authenticated was able to directly SELECT operational tables';
    END IF;
    IF v_auth_rows <> 10 THEN
        RAISE EXCEPTION 'Role Test Failed: authenticated with valid UID returned % rows, expected 10', v_auth_rows;
    END IF;
END $$;

-- Set valid user context for remaining functional tests
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-000000000001', true);

-- Isolate and prepare clean fixture data
DELETE FROM public.refresh_runs WHERE job_name IN (
    'bist_latest_prices_job', 'nasdaq_latest_prices_job', 'binance_crypto_latest_prices_job',
    'tefas_latest_prices_job', 'fx_latest_price_job', 'earnings_refresh_job',
    'economic_calendar_refresh_job', 'portfolio_snapshots_job', 'morning_job', 'closing_job',
    'internal_fake_unallowed_job'
);

-- Test 4: Empty table behavior
DO $$
DECLARE
    v_count integer;
    v_unknown_count integer;
    v_distinct_checked_at_count integer;
BEGIN
    SELECT count(*),
           count(*) FILTER (WHERE execution_status = 'never_run' AND health_status = 'unknown' AND message_code = 'JOB_NEVER_RUN' AND is_overdue = false),
           count(DISTINCT checked_at)
    INTO v_count, v_unknown_count, v_distinct_checked_at_count
    FROM public.get_mobile_system_health();

    IF v_count <> 10 OR v_unknown_count <> 10 OR v_distinct_checked_at_count <> 1 THEN
        RAISE EXCEPTION 'Empty Table Test Failed: count=%, unknown=%, checked_at_distinct=%', v_count, v_unknown_count, v_distinct_checked_at_count;
    END IF;
END $$;

-- Populate Discriminative Fixtures
-- 1. bist_latest_prices_job: On-time success
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000001', 'bist_latest_prices_job', 'prices_sync', 'BIST100',
    'success', 'updated', now() - interval '20 minutes', now() - interval '19 minutes', now() - interval '20 minutes', now() - interval '19 minutes'
);

-- 2. nasdaq_latest_prices_job: Overdue success (> 72 hours)
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000002', 'nasdaq_latest_prices_job', 'prices_sync', 'NASDAQ100',
    'success', 'updated', now() - interval '80 hours', now() - interval '79 hours 50 minutes', now() - interval '80 hours', now() - interval '79 hours 50 minutes'
);

-- 3. binance_crypto_latest_prices_job: Normal running (< 2 hours, duration_seconds must be NULL)
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000003', 'binance_crypto_latest_prices_job', 'prices_sync', 'CRYPTO',
    'running', NULL, now() - interval '10 minutes', NULL, now() - interval '10 minutes', now() - interval '10 minutes'
);

-- 4. tefas_latest_prices_job: Overdue running (> 2 hours)
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000004', 'tefas_latest_prices_job', 'prices_sync', 'TEFAS',
    'running', NULL, now() - interval '3 hours', NULL, now() - interval '3 hours', now() - interval '3 hours'
);

-- 5. fx_latest_price_job: Uncompleted newer success vs Completed older success
-- Older completed success (5 hours ago):
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000005', 'fx_latest_price_job', 'prices_sync', 'FX',
    'success', 'updated', now() - interval '5 hours', now() - interval '4 hours 55 minutes', now() - interval '5 hours', now() - interval '4 hours 55 minutes'
);
-- Newer uncompleted success (1 hour ago, finished_at IS NULL):
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000006', 'fx_latest_price_job', 'prices_sync', 'FX',
    'success', 'updated', now() - interval '1 hour', NULL, now() - interval '1 hour', now() - interval '1 hour'
);

-- 6. earnings_refresh_job: Failed with recent success (10 hours ago -> warning / JOB_FAILED)
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000007', 'earnings_refresh_job', 'calendar_refresh', 'EARNINGS',
    'success', 'updated', now() - interval '10 hours', now() - interval '9 hours 55 minutes', now() - interval '10 hours', now() - interval '9 hours 55 minutes'
);
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, error, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000008', 'earnings_refresh_job', 'calendar_refresh', 'EARNINGS',
    'failed', 'failed_no_write', 'KAP network timeout', now() - interval '1 hour', now() - interval '59 minutes', now() - interval '1 hour', now() - interval '59 minutes'
);

-- 7. economic_calendar_refresh_job: Overdue partial (> 36 hours -> warning / JOB_OVERDUE)
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000009', 'economic_calendar_refresh_job', 'calendar_refresh', 'ECONOMIC',
    'partial', 'partial_update', now() - interval '40 hours', now() - interval '39 hours 50 minutes', now() - interval '40 hours', now() - interval '39 hours 50 minutes'
);

-- 8. portfolio_snapshots_job: Overdue skipped (> 36 hours -> warning / JOB_OVERDUE)
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000010', 'portfolio_snapshots_job', 'portfolio_snapshot', 'PORTFOLIO',
    'skipped', 'skipped', now() - interval '50 hours', now() - interval '49 hours 59 minutes', now() - interval '50 hours', now() - interval '49 hours 59 minutes'
);

-- 9. morning_job: Deterministic Tie-Break Test (identical started_at & created_at, differing status/finished_at/UUID)
-- Record A (lower UUID): success, finished 1h50m ago
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000011', 'morning_job', 'market_summary', 'MARKET_SUMMARY',
    'success', 'no_op', now() - interval '2 hours', now() - interval '1 hour 50 minutes', now() - interval '2 hours', now() - interval '1 hour 50 minutes'
);
-- Record B (higher UUID): partial, finished 1h40m ago -> id DESC MUST pick this record!
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000012', 'morning_job', 'market_summary', 'MARKET_SUMMARY',
    'partial', 'partial_update', now() - interval '2 hours', now() - interval '1 hour 40 minutes', now() - interval '2 hours', now() - interval '1 hour 40 minutes'
);

-- 10. closing_job: Failed with NO previous success -> critical / JOB_FAILED
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, error, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000013', 'closing_job', 'market_summary', 'MARKET_SUMMARY',
    'failed', 'failed_no_write', 'Initial crash', now() - interval '30 minutes', now() - interval '29 minutes', now() - interval '30 minutes', now() - interval '29 minutes'
);

-- Unallowed fake job (must be ignored)
INSERT INTO public.refresh_runs (
    id, job_name, operation_type, scope, status, sync_action, started_at, finished_at, created_at, updated_at
) VALUES (
    '10000000-0000-0000-0000-000000000099', 'internal_fake_unallowed_job', 'prices_sync', 'BIST100',
    'success', 'updated', now(), now(), now(), now()
);

-- 5. Execute and Assert Business Logic
DO $$
DECLARE
    r record;
    v_total_rows integer;
    v_unique_codes integer;
    v_neg_durations integer;
    v_before_rr_count integer;
    v_before_prr_count integer;
    v_after_rr_count integer;
    v_after_prr_count integer;
BEGIN
    SELECT count(*) INTO v_before_rr_count FROM public.refresh_runs;
    SELECT count(*) INTO v_before_prr_count FROM public.provider_run_results;

    -- Execute RPC
    SELECT count(*), count(DISTINCT job_code), count(*) FILTER (WHERE duration_seconds < 0)
    INTO v_total_rows, v_unique_codes, v_neg_durations
    FROM public.get_mobile_system_health();

    SELECT count(*) INTO v_after_rr_count FROM public.refresh_runs;
    SELECT count(*) INTO v_after_prr_count FROM public.provider_run_results;

    -- Read-only count invariance
    IF v_before_rr_count <> v_after_rr_count OR v_before_prr_count <> v_after_prr_count THEN
        RAISE EXCEPTION 'Read-Only Test Failed: Row count changed across RPC call';
    END IF;

    IF v_total_rows <> 10 OR v_unique_codes <> 10 THEN
        RAISE EXCEPTION 'Allowlist Test Failed: Expected 10 unique job rows, got total=%, unique=%', v_total_rows, v_unique_codes;
    END IF;

    IF v_neg_durations > 0 THEN
        RAISE EXCEPTION 'Duration Test Failed: Found negative duration_seconds in results';
    END IF;

    -- 1. BIST prices: healthy / JOB_OK
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'BIST_PRICES';
    IF r.health_status <> 'healthy' OR r.execution_status <> 'success' OR r.message_code <> 'JOB_OK' OR r.is_overdue <> false OR r.duration_seconds <= 0 THEN
        RAISE EXCEPTION 'BIST assertion failed: %', r;
    END IF;

    -- 2. NASDAQ prices: overdue success -> warning / JOB_OVERDUE
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'NASDAQ_PRICES';
    IF r.health_status <> 'warning' OR r.execution_status <> 'success' OR r.message_code <> 'JOB_OVERDUE' OR r.is_overdue <> true THEN
        RAISE EXCEPTION 'NASDAQ assertion failed: %', r;
    END IF;

    -- 3. Crypto prices: running on-time -> duration must be NULL
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'CRYPTO_PRICES';
    IF r.health_status <> 'running' OR r.execution_status <> 'running' OR r.message_code <> 'JOB_RUNNING' OR r.duration_seconds IS NOT NULL THEN
        RAISE EXCEPTION 'Crypto assertion failed: %', r;
    END IF;

    -- 4. TEFAS prices: running overdue -> critical / JOB_OVERDUE
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'TEFAS_PRICES';
    IF r.health_status <> 'critical' OR r.execution_status <> 'running' OR r.message_code <> 'JOB_OVERDUE' OR r.is_overdue <> true THEN
        RAISE EXCEPTION 'TEFAS assertion failed: %', r;
    END IF;

    -- 5. FX prices: Discriminative Uncompleted Success Test
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'USDTRY_PRICE';
    IF r.last_started_at < (now() - interval '2 hours') THEN
        RAISE EXCEPTION 'FX last_started_at failed: expected recent uncompleted run, got %', r;
    END IF;
    IF r.last_success_at IS NULL OR r.last_success_at > (now() - interval '4 hours') THEN
        RAISE EXCEPTION 'FX last_success_at failed: expected older completed run, got %', r;
    END IF;

    -- 6. Earnings: Failed with recent success -> warning / JOB_FAILED
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'EARNINGS_CALENDAR';
    IF r.health_status <> 'warning' OR r.execution_status <> 'failed' OR r.message_code <> 'JOB_FAILED' OR r.last_success_at IS NULL THEN
        RAISE EXCEPTION 'Earnings assertion failed: %', r;
    END IF;

    -- 7. Economic calendar: Overdue partial -> warning / JOB_OVERDUE
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'ECONOMIC_CALENDAR';
    IF r.health_status <> 'warning' OR r.execution_status <> 'partial' OR r.message_code <> 'JOB_OVERDUE' OR r.is_overdue <> true THEN
        RAISE EXCEPTION 'Economic calendar assertion failed: %', r;
    END IF;

    -- 8. Portfolio snapshots: Overdue skipped -> warning / JOB_OVERDUE
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'PORTFOLIO_SNAPSHOTS';
    IF r.health_status <> 'warning' OR r.execution_status <> 'skipped' OR r.message_code <> 'JOB_OVERDUE' OR r.is_overdue <> true THEN
        RAISE EXCEPTION 'Portfolio snapshot assertion failed: %', r;
    END IF;

    -- 9. Morning job: Deterministic Tie-Break Test (Record B with higher UUID must win)
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'MORNING_SUMMARY';
    IF r.execution_status <> 'partial' THEN
        RAISE EXCEPTION 'Morning job tie-break failed: expected partial from higher UUID record, got %', r;
    END IF;

    -- 10. Closing job: Failed with NO previous success -> critical / JOB_FAILED
    SELECT * INTO r FROM public.get_mobile_system_health() WHERE job_code = 'CLOSING_SUMMARY';
    IF r.health_status <> 'critical' OR r.execution_status <> 'failed' OR r.message_code <> 'JOB_FAILED' OR r.last_success_at IS NOT NULL THEN
        RAISE EXCEPTION 'Closing job assertion failed: %', r;
    END IF;
END $$;

ROLLBACK;
