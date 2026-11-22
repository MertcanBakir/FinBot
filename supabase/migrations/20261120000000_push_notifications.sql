begin;
create schema notifications;
revoke all on schema notifications from public,anon,authenticated;
create table notifications.devices (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 installation_id uuid not null, project_id uuid not null, environment text not null check(environment in ('development','production')),
 token text not null check(length(token) between 20 and 256), active boolean not null default true,
 updated_at timestamptz not null default now(), unique(user_id,installation_id,project_id,environment), unique(project_id,environment,token)
);
create table notifications.preferences (
 user_id uuid primary key references auth.users(id) on delete cascade,
 timezone text not null default 'Europe/Istanbul', earnings_morning boolean not null default true,
 macro_morning boolean not null default true, earnings_midday boolean not null default true,
 daily_portfolio boolean not null default true, countries text[] not null default array['TR','US'],
 min_importance integer not null default 1 check(min_importance between 1 and 3)
);
-- Unknown importance is excluded, never guessed. Operator-curated by event_code.
create table notifications.event_importance(event_code text primary key, importance integer not null check(importance between 1 and 3));
insert into notifications.event_importance values
 ('TR_CPI',3),('TR_GDP',3),('TR_POLICY_RATE',3),('TR_INFLATION_REPORT',2),
 ('US_CPI',3),('US_NFP',3),('US_FOMC',3),('US_GDP',3),('US_PCE',3),('US_PPI',2),
 ('TR_LABOR',2),('TR_INDUSTRIAL_PRODUCTION',2),('US_JOLTS',2);
create table notifications.outbox (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 kind text not null check(kind in ('earnings_morning','macro_morning','earnings_midday','daily_portfolio','analysis_ready')),
 event_date date not null, event_key text not null, title text not null, body text not null,
 data jsonb not null, scheduled_at timestamptz not null, expires_at timestamptz not null,
 created_at timestamptz not null default now(), unique(user_id,kind,event_date,event_key), check(expires_at>scheduled_at)
);
create table notifications.deliveries (
 id uuid primary key default gen_random_uuid(), notification_id uuid not null references notifications.outbox(id) on delete cascade,
 device_id uuid not null references notifications.devices(id) on delete cascade,
 status text not null default 'pending' check(status in ('pending','sending','ticket','accepted','failed','cancelled','unknown')),
 attempts integer not null default 0, next_at timestamptz not null default now(), lease_token uuid, lease_until timestamptz,
 ticket_id text, error_code text, attempted_at timestamptz, device_version timestamptz, unique(notification_id,device_id)
);
create index notification_due on notifications.deliveries(next_at) where status in ('pending','ticket');
do $$ declare t text; begin
 foreach t in array array['devices','preferences','event_importance','outbox','deliveries'] loop
  execute format('alter table notifications.%I enable row level security',t);
  execute format('revoke all on notifications.%I from public,anon,authenticated',t);
 end loop;
end $$;
-- Mobile writes through exact-signature RPCs only. Worker uses trusted DB login.
create function public.register_push_device(p_installation_id uuid,p_project_id uuid,p_environment text,p_token text)
returns uuid language plpgsql security definer set search_path='' as $$
declare v_id uuid; begin
 if auth.uid() is null then raise exception 'notification_auth_required'; end if;
 if p_installation_id is null or p_project_id is null or p_environment is null or p_environment not in ('development','production')
 or p_token is null or length(p_token) not between 20 and 256 or p_token !~ '^(ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_-]+\]$' then raise exception 'notification_invalid_device'; end if;
 -- Do not silently transfer another user's token. Logout/release the old registration first.
 if exists(select 1 from notifications.devices where project_id=p_project_id and environment=p_environment and token=p_token and user_id<>auth.uid()) then
  raise exception 'notification_device_conflict'; end if;
 insert into notifications.devices(user_id,installation_id,project_id,environment,token)
 values(auth.uid(),p_installation_id,p_project_id,p_environment,p_token)
 on conflict(user_id,installation_id,project_id,environment) do update set token=excluded.token,active=true,updated_at=now() returning id into v_id;
 insert into notifications.preferences(user_id) values(auth.uid()) on conflict do nothing;
 return v_id;
exception when unique_violation then raise exception 'notification_device_conflict';
end $$;
create function public.unregister_push_device(p_device_id uuid) returns void language plpgsql security definer set search_path='' as $$
begin
 if auth.uid() is null then raise exception 'notification_auth_required'; end if;
 delete from notifications.devices where id=p_device_id and user_id=auth.uid();
end $$;
create function public.get_notification_preferences() returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb; begin
 if auth.uid() is null then raise exception 'notification_auth_required'; end if;
 insert into notifications.preferences(user_id) values(auth.uid()) on conflict do nothing;
 select to_jsonb(p)-'user_id' into result from notifications.preferences p where user_id=auth.uid(); return result;
end $$;
create function public.set_notification_preferences(p_timezone text,p_earnings_morning boolean,p_macro_morning boolean,p_earnings_midday boolean,p_daily_portfolio boolean,p_countries text[],p_min_importance integer)
returns void language plpgsql security definer set search_path='' as $$
begin
 if auth.uid() is null then raise exception 'notification_auth_required'; end if;
 if p_timezone is null or not exists(select 1 from pg_catalog.pg_timezone_names where name=p_timezone)
 or p_earnings_morning is null or p_macro_morning is null or p_earnings_midday is null or p_daily_portfolio is null
 or p_countries is null or array_position(p_countries,null) is not null or not(p_countries<@array['TR','US']::text[]) or p_min_importance is null or p_min_importance not between 1 and 3 then raise exception 'notification_invalid_preferences'; end if;
 insert into notifications.preferences values(auth.uid(),p_timezone,p_earnings_morning,p_macro_morning,p_earnings_midday,p_daily_portfolio,p_countries,p_min_importance)
 on conflict(user_id) do update set timezone=excluded.timezone,earnings_morning=excluded.earnings_morning,macro_morning=excluded.macro_morning,earnings_midday=excluded.earnings_midday,daily_portfolio=excluded.daily_portfolio,countries=excluded.countries,min_importance=excluded.min_importance;
end $$;
revoke all on function public.register_push_device(uuid,uuid,text,text),public.unregister_push_device(uuid),public.get_notification_preferences(),public.set_notification_preferences(text,boolean,boolean,boolean,boolean,text[],integer) from public,anon;
grant execute on function public.register_push_device(uuid,uuid,text,text),public.unregister_push_device(uuid),public.get_notification_preferences(),public.set_notification_preferences(text,boolean,boolean,boolean,boolean,text[],integer) to authenticated;
alter table public.refresh_runs drop constraint refresh_runs_operation_type_check;
alter table public.refresh_runs add constraint refresh_runs_operation_type_check check(operation_type is null or operation_type=any(array[
 'prices_sync'::text,'calendar_refresh'::text,'portfolio_snapshot'::text,'archive_backfill'::text,'market_summary'::text,'notification_dispatch'::text]));
commit;
