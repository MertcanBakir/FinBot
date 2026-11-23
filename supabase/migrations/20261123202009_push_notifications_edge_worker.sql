begin;

create extension if not exists pg_net;

-- The scheduled job is created disabled. Cutover runbook enables it only after
-- the EC2 worker has stopped, preventing two independent senders.

create or replace function public.notification_worker_prepare(
    p_now timestamptz,
    p_project_id uuid,
    p_environment text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $function$
declare
    v_pref record;
    v_day date;
    v_snapshot_day date;
    v_portfolios jsonb;
    v_earnings jsonb;
    v_macro jsonb;
    v_snapshot jsonb;
    v_users jsonb := '[]'::jsonb;
    v_user_count integer := 0;
    v_earnings_fresh boolean;
begin
    if pg_catalog.current_setting('request.jwt.claim.role', true)
        is distinct from 'service_role'
    then
        raise exception using errcode = '42501', message = 'notification_worker_forbidden';
    end if;
    if p_now is null or p_project_id is null or p_environment is null
        or p_environment not in ('development', 'production')
    then
        raise exception using errcode = '22023', message = 'notification_worker_invalid_request';
    end if;

    update notifications.deliveries
       set status = 'unknown', error_code = 'SEND_OUTCOME_UNKNOWN'
     where status = 'sending' and lease_until <= p_now;

    update notifications.deliveries d
       set status = 'cancelled', error_code = 'EXPIRED'
      from notifications.outbox o
     where d.notification_id = o.id
       and d.status = 'pending'
       and o.expires_at <= p_now;

    for v_pref in
        select p.*
          from notifications.preferences p
         order by p.user_id
    loop
        v_day := (p_now at time zone v_pref.timezone)::date;
        v_snapshot_day := (p_now at time zone 'Europe/Istanbul')::date - 1;

        select coalesce(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object('id', p.id::text)
                order by p.is_default desc, p.created_at, p.id
            ),
            '[]'::jsonb
        )
          into v_portfolios
          from public.portfolios p
         where p.user_id = v_pref.user_id
           and p.archived_at is null;

        select coalesce(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', e.id::text,
                    'instrument_id', e.instrument_id::text,
                    'symbol', e.symbol,
                    'earnings_date', e.earnings_date::text,
                    'watched', e.watched,
                    'held', e.held
                )
                order by e.symbol, e.instrument_id
            ),
            '[]'::jsonb
        )
          into v_earnings
          from (
                select distinct on (e.instrument_id, e.earnings_date)
                       e.id, e.instrument_id, i.symbol, e.earnings_date,
                       exists (
                           select 1
                             from public.user_watchlist_items w
                            where w.user_id = v_pref.user_id
                              and w.instrument_id = e.instrument_id
                       ) as watched,
                       exists (
                           select 1
                             from public.portfolios pf
                             cross join lateral private.get_portfolio_position_summary(
                                 pf.id, e.instrument_id
                             ) position
                            where pf.user_id = v_pref.user_id
                              and pf.archived_at is null
                              and position.current_quantity > 0
                       ) as held
                  from public.earnings_events e
                  join public.instruments i on i.id = e.instrument_id
                 where e.earnings_date between v_day and v_day + 1
                   and e.is_active
                   and not e.is_stale
                   and i.is_active
                   and i.asset_type = 'stock'
                   and i.market in ('BIST100', 'NASDAQ100')
                   and e.last_seen_at >= p_now - interval '36 hours'
                 order by e.instrument_id, e.earnings_date,
                          e.last_seen_at desc, e.id desc
          ) e;

        select notifications.earnings_source_fresh(v_day, p_now)
          into v_earnings_fresh;

        select coalesce(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', e.id::text,
                    'name', e.name,
                    'scheduled_at', e.scheduled_at
                )
                order by e.scheduled_at, e.id
            ),
            '[]'::jsonb
        )
          into v_macro
          from public.economic_events e
          join notifications.event_importance m on m.event_code = e.event_code
         where e.is_active
           and not e.is_stale
           and e.last_seen_at >= p_now - interval '36 hours'
           and e.scheduled_at > p_now
           and e.scheduled_at <= p_now + interval '3 hours'
           and e.country = any(v_pref.countries)
           and m.importance >= v_pref.min_importance;

        select pg_catalog.jsonb_build_object(
                   'id', s.id::text,
                   'portfolio_id', s.portfolio_id::text,
                   'snapshot_date', s.snapshot_date::text,
                   'previous_date', prior.snapshot_date::text,
                   'valuation_status', s.valuation_status,
                   'previous_status', prior.valuation_status,
                   'calculation_version', s.calculation_version,
                   'previous_version', prior.calculation_version,
                   'absolute_pnl_base', s.absolute_pnl_base::text,
                   'previous_absolute_pnl', prior.absolute_pnl_base::text,
                   'base_currency', s.base_currency
               )
          into v_snapshot
          from public.portfolio_snapshots s
          join public.portfolio_snapshots prior
            on prior.portfolio_id = s.portfolio_id
           and prior.snapshot_date = s.snapshot_date - 1
           and prior.base_currency = s.base_currency
         where s.portfolio_id = (
                   select p.id
                     from public.portfolios p
                    where p.user_id = v_pref.user_id
                      and p.archived_at is null
                    order by p.is_default desc, p.created_at, p.id
                    limit 1
                    offset 0
               )
           and (select count(*)
                  from public.portfolios p
                 where p.user_id = v_pref.user_id
                   and p.archived_at is null) = 1
           and s.snapshot_date = v_snapshot_day;

        v_users := v_users || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'user_id', v_pref.user_id::text,
                'preferences', pg_catalog.jsonb_build_object(
                    'timezone', v_pref.timezone,
                    'earnings_morning', v_pref.earnings_morning,
                    'macro_morning', v_pref.macro_morning,
                    'earnings_midday', v_pref.earnings_midday,
                    'daily_portfolio', v_pref.daily_portfolio,
                    'watchlist_earnings', v_pref.watchlist_earnings,
                    'economic_reminders', v_pref.economic_reminders,
                    'earnings_today', v_pref.earnings_today,
                    'earnings_tomorrow', v_pref.earnings_tomorrow,
                    'countries', v_pref.countries,
                    'min_importance', v_pref.min_importance
                ),
                'portfolios', v_portfolios,
                'earnings', v_earnings,
                'macro', v_macro,
                'snapshot', v_snapshot,
                'earnings_fresh', coalesce(v_earnings_fresh, false)
            )
        );
        v_user_count := v_user_count + 1;
    end loop;

    return pg_catalog.jsonb_build_object(
        'users', v_users,
        'user_count', v_user_count
    );
end;
$function$;

create or replace function public.notification_worker_persist(
    p_user_id uuid,
    p_now timestamptz,
    p_project_id uuid,
    p_environment text,
    p_items jsonb
)
returns integer
language plpgsql
security definer
set search_path = ''
as $function$
declare
    v_item jsonb;
    v_id uuid;
    v_kind text;
    v_event_date date;
    v_event_key text;
    v_keys text[] := array[]::text[];
    v_count integer := 0;
begin
    if pg_catalog.current_setting('request.jwt.claim.role', true)
        is distinct from 'service_role'
    then
        raise exception using errcode = '42501', message = 'notification_worker_forbidden';
    end if;
    if p_user_id is null or p_now is null or p_project_id is null or p_environment is null
       or p_environment not in ('development', 'production')
       or p_items is null or pg_catalog.jsonb_typeof(p_items) <> 'array'
    then
        raise exception using errcode = '22023', message = 'notification_worker_invalid_request';
    end if;

    for v_item in select value from pg_catalog.jsonb_array_elements(p_items)
    loop
        v_kind := v_item ->> 'kind';
        v_event_date := (v_item -> 'data' ->> 'event_date')::date;
        v_event_key := v_item ->> 'event_key';
        if v_kind is null or v_event_date is null or v_event_key is null
           or v_item ->> 'title' is null or v_item ->> 'body' is null
           or pg_catalog.jsonb_typeof(v_item -> 'data') <> 'object'
        then
            raise exception using errcode = '22023', message = 'notification_worker_invalid_notice';
        end if;

        v_keys := pg_catalog.array_append(
            v_keys,
            v_kind || ':' || v_event_date::text || ':' || v_event_key
        );

        insert into notifications.outbox as existing_outbox(
            user_id, kind, event_date, event_key, title, body, data,
            scheduled_at, expires_at
        )
        values (
            p_user_id, v_kind, v_event_date, v_event_key,
            v_item ->> 'title', v_item ->> 'body', v_item -> 'data',
            (v_item ->> 'scheduled_at')::timestamptz,
            (v_item ->> 'expires_at')::timestamptz
        )
        on conflict (user_id, kind, event_date, event_key)
        do update set
            title = excluded.title,
            body = excluded.body,
            data = excluded.data,
            scheduled_at = excluded.scheduled_at,
            expires_at = excluded.expires_at
        where not exists (
            select 1
              from notifications.deliveries d
             where d.notification_id = existing_outbox.id
               and d.attempted_at is not null
        )
        returning id into v_id;
        if found then v_count := v_count + 1; end if;
    end loop;

    update notifications.deliveries d
       set status = 'cancelled',
           error_code = 'SOURCE_NO_LONGER_ELIGIBLE'
      from notifications.outbox o
     where d.notification_id = o.id
       and o.user_id = p_user_id
       and d.status = 'pending'
       and o.kind <> 'analysis_ready'
       and not (
           (o.kind || ':' || o.event_date::text || ':' || o.event_key)
           = any(v_keys)
       );

    insert into notifications.deliveries as existing_delivery(
        notification_id, device_id, next_at
    )
    select o.id, d.id, greatest(o.scheduled_at, p_now)
      from notifications.outbox o
      join notifications.devices d on d.user_id = o.user_id
     where o.user_id = p_user_id
       and o.expires_at > p_now
       and d.active
       and d.project_id = p_project_id
       and d.environment = p_environment
       and d.updated_at <= o.created_at
       and (o.kind || ':' || o.event_date::text || ':' || o.event_key)
           = any(v_keys)
    on conflict (notification_id, device_id)
    do update set
        status = 'pending',
        error_code = null,
        next_at = excluded.next_at
    where existing_delivery.status = 'cancelled'
      and existing_delivery.attempted_at is null
      and existing_delivery.error_code in (
          'SOURCE_NO_LONGER_ELIGIBLE', 'EXPIRED_OR_DISABLED'
      );

    return v_count;
end;
$function$;

create or replace function public.notification_worker_due(
    p_now timestamptz,
    p_project_id uuid,
    p_environment text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $function$
declare
    v_send boolean;
    v_receipts boolean;
begin
    if pg_catalog.current_setting('request.jwt.claim.role', true)
        is distinct from 'service_role'
    then
        raise exception using errcode = '42501', message = 'notification_worker_forbidden';
    end if;
    if p_now is null or p_project_id is null or p_environment is null
       or p_environment not in ('development', 'production')
    then
        raise exception using errcode = '22023', message = 'notification_worker_invalid_request';
    end if;

    select exists (
        select 1
          from notifications.deliveries d
          join notifications.devices v on v.id = d.device_id
          join notifications.outbox o on o.id = d.notification_id
         where d.status = 'pending'
           and d.next_at <= p_now
           and o.scheduled_at <= p_now
           and v.project_id = p_project_id
           and v.environment = p_environment
           and (d.lease_until is null or d.lease_until <= p_now)
         limit 1
    ) into v_send;

    select exists (
        select 1
          from notifications.deliveries d
          join notifications.devices v on v.id = d.device_id
         where d.status = 'ticket'
           and d.next_at <= p_now
           and v.project_id = p_project_id
           and v.environment = p_environment
           and (d.lease_until is null or d.lease_until <= p_now)
         limit 1
    ) into v_receipts;

    return pg_catalog.jsonb_build_object('send', v_send, 'receipts', v_receipts);
end;
$function$;

create or replace function public.notification_worker_claim(
    p_now timestamptz,
    p_project_id uuid,
    p_environment text,
    p_receipts boolean
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $function$
declare
    v_item record;
    v_lease uuid := pg_catalog.gen_random_uuid();
begin
    if pg_catalog.current_setting('request.jwt.claim.role', true)
        is distinct from 'service_role'
    then
        raise exception using errcode = '42501', message = 'notification_worker_forbidden';
    end if;
    if p_now is null or p_project_id is null or p_receipts is null or p_environment is null
       or p_environment not in ('development', 'production')
    then
        raise exception using errcode = '22023', message = 'notification_worker_invalid_request';
    end if;

    perform pg_catalog.pg_advisory_xact_lock(710192);

    update notifications.deliveries
       set status = 'unknown', error_code = 'SEND_OUTCOME_UNKNOWN'
     where status = 'sending' and lease_until <= p_now;

    update notifications.deliveries d
       set status = 'cancelled', error_code = 'EXPIRED_OR_DISABLED'
      from notifications.outbox o,
           notifications.devices v,
           notifications.preferences p
     where d.notification_id = o.id
       and d.device_id = v.id
       and p.user_id = o.user_id
       and d.status = 'pending'
       and (
           o.expires_at <= p_now
           or not v.active
           or not notifications.kind_enabled(o.user_id, o.kind)
       );

    if p_receipts then
        select d.*, v.token, v.updated_at as device_updated_at,
               o.data, o.title, o.body, o.expires_at, o.user_id, o.kind
          into v_item
          from notifications.deliveries d
          join notifications.devices v on v.id = d.device_id
          join notifications.outbox o on o.id = d.notification_id
         where d.status = 'ticket'
           and d.next_at <= p_now
           and (d.lease_until is null or d.lease_until <= p_now)
           and v.project_id = p_project_id
           and v.environment = p_environment
         order by d.next_at
         limit 1
         for update of d skip locked;
    else
        select d.*, v.token, v.updated_at as device_updated_at,
               o.data, o.title, o.body, o.expires_at, o.user_id, o.kind
          into v_item
          from notifications.deliveries d
          join notifications.devices v on v.id = d.device_id
          join notifications.outbox o on o.id = d.notification_id
          join notifications.preferences p on p.user_id = o.user_id
         where d.status = 'pending'
           and d.next_at <= p_now
           and o.scheduled_at <= p_now
           and o.expires_at > p_now
           and v.active
           and v.project_id = p_project_id
           and v.environment = p_environment
           and notifications.kind_enabled(o.user_id, o.kind)
           and notifications.notice_eligible(o.id, p_now)
           and (
               o.kind <> 'daily_portfolio'
               or (
                   exists (
                       select 1
                         from public.portfolio_snapshots s
                         join public.portfolios pf on pf.id = s.portfolio_id
                        where s.id::text = o.data ->> 'snapshot_id'
                          and pf.user_id = o.user_id
                          and pf.archived_at is null
                          and s.valuation_status = 'success'
                          and s.calculation_version = 2
                   )
                   and (
                       select count(*)
                         from public.portfolios pf
                        where pf.user_id = o.user_id
                          and pf.archived_at is null
                   ) = 1
               )
           )
           and (
               o.kind = 'economic_reminder'
               or not exists (
                   select 1
                     from notifications.deliveries d2
                     join notifications.outbox o2 on o2.id = d2.notification_id
                    where o2.user_id = o.user_id
                      and o2.id <> o.id
                      and d2.attempted_at > p_now - interval '15 minutes'
                      and o2.kind = o.kind
               )
           )
         order by o.scheduled_at, o.created_at, d.id
         limit 1
         for update of d skip locked;
    end if;

    if not found then return null; end if;

    if not p_receipts and v_item.kind <> 'economic_reminder' then
        update notifications.deliveries d
           set next_at = greatest(d.next_at, p_now + interval '15 minutes')
          from notifications.outbox o
         where d.notification_id = o.id
           and d.status = 'pending'
           and o.user_id = v_item.user_id
           and o.id <> v_item.notification_id
           and case
               when v_item.kind in ('earnings_morning', 'macro_morning')
               then o.kind in ('earnings_morning', 'macro_morning')
               else o.kind = v_item.kind
           end;
    end if;

    update notifications.deliveries
       set lease_token = v_lease,
           lease_until = p_now + interval '2 minutes',
           status = case when p_receipts then status else 'sending' end,
           attempted_at = case when p_receipts then attempted_at else p_now end,
           device_version = case when p_receipts then device_version else v_item.device_updated_at end,
           attempts = attempts + case when p_receipts then 0 else 1 end
     where id = v_item.id;

    return pg_catalog.jsonb_build_object(
        'id', v_item.id,
        'notification_id', v_item.notification_id,
        'device_id', v_item.device_id,
        'device_version', case when p_receipts then v_item.device_version
                               else v_item.device_updated_at end,
        'lease_token', v_lease,
        'attempts', case when p_receipts then v_item.attempts
                         else v_item.attempts + 1 end,
        'ticket_id', v_item.ticket_id,
        'attempted_at', v_item.attempted_at,
        'token', v_item.token,
        'data', v_item.data,
        'title', v_item.title,
        'body', v_item.body,
        'expires_at', v_item.expires_at
    );
end;
$function$;

create or replace function public.notification_worker_finish(
    p_delivery_id uuid,
    p_lease_token uuid,
    p_now timestamptz,
    p_status text,
    p_error_code text,
    p_ticket_id text,
    p_device_id uuid,
    p_device_version timestamptz,
    p_disable_device boolean,
    p_attempts integer
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $function$
declare
    v_updated boolean := false;
    v_retry_minutes integer;
begin
    if pg_catalog.current_setting('request.jwt.claim.role', true)
        is distinct from 'service_role'
    then
        raise exception using errcode = '42501', message = 'notification_worker_forbidden';
    end if;
    if p_delivery_id is null or p_lease_token is null or p_now is null
       or p_status is null
       or p_status not in ('pending', 'ticket', 'accepted', 'failed', 'unknown')
       or p_attempts is null or p_attempts < 0
    then
        raise exception using errcode = '22023', message = 'notification_worker_invalid_result';
    end if;

    v_retry_minutes := case when p_status = 'ticket'
        then 15
        else least(60, (2 ^ least(p_attempts + 1, 6))::integer)
    end;

    update notifications.deliveries
       set status = p_status,
           error_code = p_error_code,
           ticket_id = coalesce(p_ticket_id, ticket_id),
           next_at = p_now + pg_catalog.make_interval(mins => v_retry_minutes),
           lease_until = null,
           lease_token = null
     where id = p_delivery_id
       and lease_token = p_lease_token
       and lease_until > p_now
    returning true into v_updated;

    if coalesce(v_updated, false) and p_disable_device then
        update notifications.devices
           set active = false
         where id = p_device_id
           and updated_at = p_device_version;
    end if;
    return coalesce(v_updated, false);
end;
$function$;

create or replace function public.notification_worker_start_tracking(
    p_started_at timestamptz,
    p_prepared_count integer
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $function$
declare
    v_run_id uuid;
begin
    if pg_catalog.current_setting('request.jwt.claim.role', true)
        is distinct from 'service_role'
    then
        raise exception using errcode = '42501', message = 'notification_worker_forbidden';
    end if;
    if p_started_at is null or p_prepared_count is null or p_prepared_count < 0
    then
        raise exception using errcode = '22023', message = 'notification_worker_invalid_request';
    end if;

    insert into public.refresh_runs(
        job_name, trigger_type, started_at, status, sync_action,
        fresh_event_count, final_event_count, warnings, metrics,
        operation_type, provider_id, scope, notification_status
    )
    values (
        'push_notifications_batch', 'scheduled', p_started_at, 'running', null,
        0, 0, '[]'::jsonb,
        pg_catalog.jsonb_build_object(
            'runtime', 'supabase_edge_cron',
            'prepared_count', p_prepared_count,
            'cron_interval_minutes', 5
        ),
        'notification_dispatch', null, 'PUSH', 'not_required'
    )
    returning id into v_run_id;
    return v_run_id;
end;
$function$;

create or replace function public.notification_worker_finish_tracking(
    p_run_id uuid,
    p_finished_at timestamptz,
    p_status text,
    p_metrics jsonb,
    p_error text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $function$
declare
    v_updated boolean := false;
begin
    if pg_catalog.current_setting('request.jwt.claim.role', true)
        is distinct from 'service_role'
    then
        raise exception using errcode = '42501', message = 'notification_worker_forbidden';
    end if;
    if p_run_id is null or p_finished_at is null or p_status is null
       or p_status not in ('success', 'partial', 'failed')
       or p_metrics is null
       or pg_catalog.jsonb_typeof(p_metrics) <> 'object'
       or p_error not in ('push_worker_cycle_failed')
          and p_error is not null
    then
        raise exception using errcode = '22023', message = 'notification_worker_invalid_result';
    end if;

    update public.refresh_runs
       set finished_at = p_finished_at,
           status = p_status,
           sync_action = case
               when p_status = 'failed' then 'failed_no_write'
               when p_status = 'partial' then 'partial_update'
               else 'updated'
           end,
           metrics = metrics || p_metrics,
           error = p_error
     where id = p_run_id
       and status = 'running'
    returning true into v_updated;
    return coalesce(v_updated, false);
end;
$function$;

alter function public.notification_worker_prepare(timestamptz, uuid, text) owner to postgres;
alter function public.notification_worker_persist(uuid, timestamptz, uuid, text, jsonb) owner to postgres;
alter function public.notification_worker_due(timestamptz, uuid, text) owner to postgres;
alter function public.notification_worker_claim(timestamptz, uuid, text, boolean) owner to postgres;
alter function public.notification_worker_finish(uuid, uuid, timestamptz, text, text, text, uuid, timestamptz, boolean, integer) owner to postgres;
alter function public.notification_worker_start_tracking(timestamptz, integer) owner to postgres;
alter function public.notification_worker_finish_tracking(uuid, timestamptz, text, jsonb, text) owner to postgres;

revoke all on function public.notification_worker_prepare(timestamptz, uuid, text),
    public.notification_worker_persist(uuid, timestamptz, uuid, text, jsonb),
    public.notification_worker_due(timestamptz, uuid, text),
    public.notification_worker_claim(timestamptz, uuid, text, boolean),
    public.notification_worker_finish(uuid, uuid, timestamptz, text, text, text, uuid, timestamptz, boolean, integer),
    public.notification_worker_start_tracking(timestamptz, integer),
    public.notification_worker_finish_tracking(uuid, timestamptz, text, jsonb, text)
from public, anon, authenticated, service_role;

grant execute on function public.notification_worker_prepare(timestamptz, uuid, text),
    public.notification_worker_persist(uuid, timestamptz, uuid, text, jsonb),
    public.notification_worker_due(timestamptz, uuid, text),
    public.notification_worker_claim(timestamptz, uuid, text, boolean),
    public.notification_worker_finish(uuid, uuid, timestamptz, text, text, text, uuid, timestamptz, boolean, integer),
    public.notification_worker_start_tracking(timestamptz, integer),
    public.notification_worker_finish_tracking(uuid, timestamptz, text, jsonb, text)
to service_role;

do $schedule$
declare
    v_job_id bigint;
    v_command text;
begin
    if pg_catalog.to_regprocedure('cron.schedule(text,text,text)') is null
       or pg_catalog.to_regprocedure('cron.alter_job(bigint,text,text,text,text,boolean)') is null
       or pg_catalog.to_regprocedure('net.http_post(text,jsonb,jsonb,jsonb,integer)') is null
    then
        raise exception using errcode = '55000',
            message = 'notification_worker_cron_extensions_required';
    end if;

    v_command := $command$
        select net.http_post(
            url := (
                select decrypted_secret
                  from vault.decrypted_secrets
                 where name = 'push_worker_project_url'
            ) || '/functions/v1/push-notifications-worker',
            headers := pg_catalog.jsonb_build_object(
                'Content-Type', 'application/json',
                'apikey', (
                    select decrypted_secret
                      from vault.decrypted_secrets
                     where name = 'push_worker_publishable_key'
                ),
                'x-worker-secret', (
                    select decrypted_secret
                      from vault.decrypted_secrets
                     where name = 'push_worker_secret'
                )
            ),
            body := pg_catalog.jsonb_build_object('triggered_by', 'supabase_cron'),
            timeout_milliseconds := 120000
        ) as request_id;
    $command$;

    select jobid into v_job_id
      from cron.job
     where jobname = 'finbot-push-notifications-worker';

    if v_job_id is null then
        v_job_id := cron.schedule(
            'finbot-push-notifications-worker',
            '*/5 * * * *',
            v_command
        );
    else
        perform cron.alter_job(
            job_id := v_job_id,
            schedule := '*/5 * * * *',
            command := v_command,
            active := false
        );
    end if;

    perform cron.alter_job(job_id := v_job_id, active := false);
end;
$schedule$;

comment on function public.notification_worker_prepare(timestamptz, uuid, text)
    is 'Service-role-only notification source loader and stale lease cleanup.';
comment on function public.notification_worker_persist(uuid, timestamptz, uuid, text, jsonb)
    is 'Service-role-only idempotent notification outbox writer and delivery binder.';
comment on function public.notification_worker_claim(timestamptz, uuid, text, boolean)
    is 'Service-role-only lease claim for push delivery or Expo receipt processing.';

commit;
