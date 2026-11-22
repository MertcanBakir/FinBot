begin;

alter table notifications.preferences
 add column watchlist_earnings boolean not null default true,
 add column economic_reminders boolean not null default true,
 add column earnings_today boolean not null default true,
 add column earnings_tomorrow boolean not null default true;
-- Preserve the old economic switch. Old clients can still disable its replacement.
update notifications.preferences set economic_reminders=macro_morning;
alter table notifications.outbox drop constraint outbox_kind_check;
alter table notifications.outbox add constraint outbox_kind_check check(kind in
 ('earnings_morning','macro_morning','earnings_midday','daily_portfolio','analysis_ready',
  'watchlist_earnings','economic_reminder','earnings_today','earnings_tomorrow'));

create or replace function public.get_notification_preferences() returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
 if auth.uid() is null then raise exception 'notification_auth_required'; end if;
 insert into notifications.preferences(user_id) values(auth.uid()) on conflict do nothing;
 select pg_catalog.jsonb_build_object('timezone',p.timezone,'earnings_morning',p.earnings_morning,
  'macro_morning',p.economic_reminders,'earnings_midday',p.earnings_midday,
  'daily_portfolio',p.daily_portfolio,'countries',p.countries,'min_importance',p.min_importance)
 into result from notifications.preferences p where p.user_id=auth.uid();
 return result;
end $$;

create or replace function public.set_notification_preferences(
 p_timezone text,p_earnings_morning boolean,p_macro_morning boolean,
 p_earnings_midday boolean,p_daily_portfolio boolean,p_countries text[],p_min_importance integer)
returns void language plpgsql security definer set search_path='' as $$
begin
 if auth.uid() is null then raise exception 'notification_auth_required'; end if;
 if p_timezone is null or not exists(select 1 from pg_catalog.pg_timezone_names where name=p_timezone)
 or p_earnings_morning is null or p_macro_morning is null or p_earnings_midday is null or p_daily_portfolio is null
 or p_countries is null or pg_catalog.array_position(p_countries,null) is not null
 or not(p_countries<@array['TR','US']::text[]) or p_min_importance is null or p_min_importance not between 1 and 3
 then raise exception 'notification_invalid_preferences'; end if;
 insert into notifications.preferences(user_id,timezone,earnings_morning,macro_morning,earnings_midday,daily_portfolio,countries,min_importance,economic_reminders)
 values(auth.uid(),p_timezone,p_earnings_morning,p_macro_morning,p_earnings_midday,p_daily_portfolio,p_countries,p_min_importance,p_macro_morning)
 on conflict(user_id) do update set timezone=excluded.timezone,earnings_morning=excluded.earnings_morning,
 macro_morning=excluded.macro_morning,economic_reminders=excluded.economic_reminders,
 earnings_midday=excluded.earnings_midday,daily_portfolio=excluded.daily_portfolio,
 countries=excluded.countries,min_importance=excluded.min_importance;
end $$;

create function public.get_notification_preferences_v2() returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
 if auth.uid() is null then raise exception 'notification_auth_required'; end if;
 insert into notifications.preferences(user_id) values(auth.uid()) on conflict do nothing;
 select pg_catalog.to_jsonb(p)-'user_id'-'macro_morning' into result
 from notifications.preferences p where p.user_id=auth.uid();
 return result;
end $$;

create function public.set_notification_preferences_v2(
 p_timezone text,p_earnings_morning boolean,p_earnings_midday boolean,p_daily_portfolio boolean,
 p_watchlist_earnings boolean,p_economic_reminders boolean,p_earnings_today boolean,
 p_earnings_tomorrow boolean,p_countries text[],p_min_importance integer)
returns void language plpgsql security definer set search_path='' as $$
begin
 if auth.uid() is null then raise exception 'notification_auth_required'; end if;
 if p_watchlist_earnings is null or p_economic_reminders is null or p_earnings_today is null
 or p_earnings_tomorrow is null then raise exception 'notification_invalid_preferences'; end if;
 perform public.set_notification_preferences(p_timezone,p_earnings_morning,p_economic_reminders,
  p_earnings_midday,p_daily_portfolio,p_countries,p_min_importance);
 update notifications.preferences set watchlist_earnings=p_watchlist_earnings,
  earnings_today=p_earnings_today,earnings_tomorrow=p_earnings_tomorrow where user_id=auth.uid();
end $$;

-- A failed/latest partial refresh cannot justify saying that no events exist.
create function notifications.earnings_source_fresh(p_day date,p_now timestamptz)
returns boolean language sql stable set search_path='' as $$
 select coalesce((select r.status='success' and r.metrics->>'database_status'='success'
  and r.finished_at>=p_now-interval '36 hours' and r.finished_at<=p_now
  and r.coverage_start<=p_day and r.coverage_end>=p_day
 from public.refresh_runs r where r.job_name='earnings_refresh_job'
 and r.status in ('success','partial','failed')
 order by r.started_at desc,r.created_at desc,r.id desc limit 1),false)
$$;

create function notifications.kind_enabled(p_user uuid,p_kind text) returns boolean
language sql stable set search_path='' as $$
 select coalesce((select case when p_kind='macro_morning' then false
 when p_kind='economic_reminder' then p.economic_reminders
 else (pg_catalog.to_jsonb(p)->>p_kind)::boolean end
 from notifications.preferences p where p.user_id=p_user),false)
$$;

create function notifications.notice_eligible(p_id uuid,p_now timestamptz)
returns boolean language plpgsql stable set search_path='' as $$
declare o notifications.outbox; ids text[]; n bigint; content text;
begin
 select * into o from notifications.outbox where id=p_id;
 if not found then return false; end if;
 if o.kind='earnings_morning' and not exists(select 1 from public.portfolios pf
  where pf.user_id=o.user_id and pf.archived_at is null) then return false; end if;
 if o.kind='daily_portfolio' then
  return exists(select 1 from public.portfolio_snapshots s join public.portfolios pf on pf.id=s.portfolio_id
   where s.id::text=o.data->>'snapshot_id' and pf.user_id=o.user_id and pf.archived_at is null
   and s.valuation_status='success' and s.calculation_version=2)
   and (select count(*) from public.portfolios pf where pf.user_id=o.user_id and pf.archived_at is null)=1;
 end if;
 if o.kind='economic_reminder' then
  return exists(select 1 from public.economic_events e
   join notifications.event_importance m on m.event_code=e.event_code
   join notifications.preferences p on p.user_id=o.user_id
   where e.id::text=o.data->>'event_id' and e.scheduled_at=(o.data->>'scheduled_at')::timestamptz
   and e.is_active and not e.is_stale and e.last_seen_at>=p_now-interval '36 hours'
   and e.scheduled_at>p_now and e.country=any(p.countries) and m.importance>=p.min_importance);
 end if;
 if o.kind not in ('earnings_morning','earnings_midday','watchlist_earnings','earnings_today','earnings_tomorrow')
 then return true; end if;
 select count(distinct e.instrument_id),array_agg(distinct e.instrument_id::text),
  pg_catalog.string_agg(distinct e.instrument_id::text||':'||i.symbol,',' order by e.instrument_id::text||':'||i.symbol)
 into n,ids,content
 from public.earnings_events e join public.instruments i on i.id=e.instrument_id
 where e.earnings_date=o.event_date and e.is_active and not e.is_stale
 and e.last_seen_at>=p_now-interval '36 hours' and i.is_active
 and i.asset_type='stock' and i.market in ('BIST100','NASDAQ100')
 and (o.kind in ('earnings_today','earnings_tomorrow')
  or (o.kind in ('earnings_morning','earnings_midday') and exists(
   select 1 from public.portfolios pf
   cross join lateral private.get_portfolio_position_summary(pf.id,e.instrument_id) s
   where pf.user_id=o.user_id and pf.archived_at is null and s.current_quantity>0))
  or (o.kind='watchlist_earnings' and exists(select 1 from public.user_watchlist_items w
   where w.user_id=o.user_id and w.instrument_id=e.instrument_id)
   and not exists(select 1 from public.portfolios pf
    cross join lateral private.get_portfolio_position_summary(pf.id,e.instrument_id) s
    join notifications.preferences p on p.user_id=o.user_id
    where pf.user_id=o.user_id and pf.archived_at is null and s.current_quantity>0 and p.earnings_midday)));
 if o.kind='earnings_midday' then return o.event_key=any(coalesce(ids,array[]::text[])); end if;
 if n=0 then
  return o.data->>'count'='0' and o.kind in ('earnings_morning','earnings_today')
   and notifications.earnings_source_fresh(o.event_date,p_now);
 end if;
 return n::text=o.data->>'count'
 and pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(content,'UTF8')),'hex')=o.data->>'content_hash'
 and not exists(
  select 1 from pg_catalog.jsonb_array_elements_text(o.data->'instrument_ids') j(value)
  where not(j.value=any(ids)));
end $$;

revoke all on function public.get_notification_preferences_v2(),
 public.set_notification_preferences_v2(text,boolean,boolean,boolean,boolean,boolean,boolean,boolean,text[],integer)
 from public,anon;
grant execute on function public.get_notification_preferences_v2(),
 public.set_notification_preferences_v2(text,boolean,boolean,boolean,boolean,boolean,boolean,boolean,text[],integer)
 to authenticated;
revoke all on function notifications.earnings_source_fresh(date,timestamptz),
 notifications.kind_enabled(uuid,text),notifications.notice_eligible(uuid,timestamptz) from public,anon,authenticated;
-- Rollout: pause old worker, migrate, deploy v2 worker, resume. Do not run both planners.
-- No old pending macro digest is dispatched by v2; ticket receipts remain processable.
commit;
