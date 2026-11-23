begin;

-- Supabase secret API keys are not JWTs and do not guarantee the legacy
-- request.jwt.claim.role GUC. Function EXECUTE grants are the authorization
-- boundary; this removes only the redundant claim check from the fixed worker
-- RPC allowlist. PUBLIC, anon, and authenticated remain revoked.
do $migration$
declare
    v_signature text;
    v_function regprocedure;
    v_definition text;
    v_guard constant text := $guard$    if pg_catalog.current_setting('request.jwt.claim.role', true)
        is distinct from 'service_role'
    then
        raise exception using errcode = '42501', message = 'notification_worker_forbidden';
    end if;
$guard$;
    v_signatures constant text[] := array[
        'public.notification_worker_prepare(timestamp with time zone, uuid, text)',
        'public.notification_worker_persist(uuid, timestamp with time zone, uuid, text, jsonb)',
        'public.notification_worker_due(timestamp with time zone, uuid, text)',
        'public.notification_worker_claim(timestamp with time zone, uuid, text, boolean)',
        'public.notification_worker_finish(uuid, uuid, timestamp with time zone, text, text, text, uuid, timestamp with time zone, boolean, integer)',
        'public.notification_worker_start_tracking(timestamp with time zone, integer)',
        'public.notification_worker_finish_tracking(uuid, timestamp with time zone, text, jsonb, text)'
    ];
begin
    foreach v_signature in array v_signatures
    loop
        v_function := pg_catalog.to_regprocedure(v_signature);
        if v_function is null then
            raise exception using
                errcode = '55000',
                message = 'notification_worker_function_missing';
        end if;

        select pg_catalog.pg_get_functiondef(v_function)
          into v_definition;

        if pg_catalog.strpos(v_definition, v_guard) = 0 then
            raise exception using
                errcode = '55000',
                message = 'notification_worker_role_guard_unexpected';
        end if;

        execute pg_catalog.replace(v_definition, v_guard, '');
    end loop;
end;
$migration$;

commit;
