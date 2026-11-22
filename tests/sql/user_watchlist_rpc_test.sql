\set ON_ERROR_STOP on

begin;

insert into auth.users (id) values
    ('00000000-0000-0000-0000-000000000081'),
    ('00000000-0000-0000-0000-000000000082');

insert into public.instruments (
    id, market, symbol, company_name, is_active, asset_type, currency
)
select
    ('20000000-0000-0000-0000-' || lpad(n::text, 12, '0'))::uuid,
    'BIST100',
    'WATCH' || lpad(n::text, 3, '0'),
    'Watchlist Test Instrument ' || n,
    n <> 25,
    'stock',
    'TRY'
from generate_series(1, 25) as n;

insert into public.instrument_latest_prices (
    instrument_id, price, currency, previous_close, change_amount,
    change_percent, price_at, provider_code, is_stale, stale_reason
) values (
    '20000000-0000-0000-0000-000000000001',
    125.25, 'TRY', 120, 5.25, 4.375,
    '2026-08-30 18:00:00+03', 'TEST', false, null
);

insert into public.portfolios (
    id, user_id, name, base_currency, is_default
) values (
    '10000000-0000-0000-0000-000000000081',
    '00000000-0000-0000-0000-000000000081',
    'Watchlist reset test', 'TRY', true
);

-- No session is rejected before any argument-specific behavior.
set local role authenticated;
select set_config('request.jwt.claim.sub', '', true);
do $$
begin
    begin
        perform public.get_my_watchlist(20, 0);
        raise exception 'unauthenticated watchlist read was accepted';
    exception when others then
        if sqlerrm = 'unauthenticated watchlist read was accepted'
           or sqlerrm <> 'watchlist_authentication_required' then
            raise;
        end if;
    end;
end;
$$;
reset role;

-- User A can add an active instrument and receives an idempotent replay.
set local role authenticated;
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000081',
    true
);
do $$
declare
    v_first record;
    v_second record;
begin
    select * into v_first
    from public.set_my_watchlist_item(
        '20000000-0000-0000-0000-000000000001', true
    );
    select * into v_second
    from public.set_my_watchlist_item(
        '20000000-0000-0000-0000-000000000001', true
    );

    if not v_first.is_watched or not v_first.was_changed
       or v_first.watchlist_id is null then
        raise exception 'first watchlist insertion result is wrong: %', v_first;
    end if;
    if not v_second.is_watched or v_second.was_changed
       or v_second.watchlist_id <> v_first.watchlist_id then
        raise exception 'watchlist insertion was not idempotent: %', v_second;
    end if;
end;
$$;

-- Mobile roles cannot bypass the RPC boundary with direct table access.
do $$
begin
    begin
        perform count(*) from public.user_watchlist_items;
        raise exception 'authenticated direct table read was accepted';
    exception when insufficient_privilege then
        null;
    end;
end;
$$;
reset role;

-- User B cannot see or remove User A's row.
set local role authenticated;
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000082',
    true
);
do $$
declare
    v_count bigint;
    v_remove record;
begin
    select count(*) into v_count from public.get_my_watchlist(20, 0);
    if v_count <> 0 then
        raise exception 'User B can read User A watchlist';
    end if;

    select * into v_remove
    from public.set_my_watchlist_item(
        '20000000-0000-0000-0000-000000000001', false
    );
    if v_remove.is_watched or v_remove.was_changed
       or v_remove.watchlist_id is not null then
        raise exception 'User B changed User A watchlist: %', v_remove;
    end if;
end;
$$;
reset role;

do $$
begin
    if not exists (
        select 1
        from public.user_watchlist_items
        where user_id = '00000000-0000-0000-0000-000000000081'
          and instrument_id = '20000000-0000-0000-0000-000000000001'
    ) then
        raise exception 'User B removed User A watchlist row';
    end if;
end;
$$;

-- Add the remaining page fixtures through the public RPC.
set local role authenticated;
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000081',
    true
);
do $$
declare
    n integer;
begin
    for n in 2..23 loop
        perform public.set_my_watchlist_item(
            ('20000000-0000-0000-0000-' || lpad(n::text, 12, '0'))::uuid,
            true
        );
    end loop;
end;
$$;
reset role;

-- Give fixtures deterministic timestamps, including an id-based tie-break.
update public.user_watchlist_items as w
set created_at = timestamp with time zone '2026-08-30 12:00:00+03'
                 + ((substring(i.symbol from 6))::integer * interval '1 minute')
from public.instruments as i
where i.id = w.instrument_id
  and w.user_id = '00000000-0000-0000-0000-000000000081';

update public.user_watchlist_items
set created_at = '2026-08-30 13:00:00+03'
where user_id = '00000000-0000-0000-0000-000000000081'
  and instrument_id in (
      '20000000-0000-0000-0000-000000000022',
      '20000000-0000-0000-0000-000000000023'
  );

update public.user_watchlist_items
set id = case instrument_id
    when '20000000-0000-0000-0000-000000000022'::uuid
        then '90000000-0000-0000-0000-000000000022'::uuid
    when '20000000-0000-0000-0000-000000000023'::uuid
        then '90000000-0000-0000-0000-000000000023'::uuid
end
where user_id = '00000000-0000-0000-0000-000000000081'
  and instrument_id in (
      '20000000-0000-0000-0000-000000000022',
      '20000000-0000-0000-0000-000000000023'
  );

set local role authenticated;
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000081',
    true
);
do $$
declare
    v_page_one_count bigint;
    v_page_two_count bigint;
    v_total_min bigint;
    v_total_max bigint;
    v_first_instrument uuid;
    v_price_row record;
    v_no_price_row record;
begin
    select count(*), min(total_count), max(total_count)
    into v_page_one_count, v_total_min, v_total_max
    from public.get_my_watchlist(20, 0);
    if v_page_one_count <> 20 or v_total_min <> 23 or v_total_max <> 23 then
        raise exception 'first page or total_count is wrong: %, %, %',
            v_page_one_count, v_total_min, v_total_max;
    end if;

    select count(*), min(total_count), max(total_count)
    into v_page_two_count, v_total_min, v_total_max
    from public.get_my_watchlist(20, 20);
    if v_page_two_count <> 3 or v_total_min <> 23 or v_total_max <> 23 then
        raise exception 'second page or total_count is wrong: %, %, %',
            v_page_two_count, v_total_min, v_total_max;
    end if;

    select instrument_id into v_first_instrument
    from public.get_my_watchlist(1, 0);
    if v_first_instrument <> '20000000-0000-0000-0000-000000000023' then
        raise exception 'deterministic created_at/id ordering failed: %',
            v_first_instrument;
    end if;

    select * into v_price_row
    from public.get_my_watchlist(100, 0)
    where instrument_id = '20000000-0000-0000-0000-000000000001';
    if not v_price_row.has_price
       or v_price_row.price <> 125.25
       or v_price_row.change_amount <> 5.25
       or v_price_row.change_percent <> 4.375
       or v_price_row.price_is_stale is distinct from false then
        raise exception 'priced watchlist row is wrong: %', v_price_row;
    end if;

    select * into v_no_price_row
    from public.get_my_watchlist(100, 0)
    where instrument_id = '20000000-0000-0000-0000-000000000002';
    if v_no_price_row.has_price
       or v_no_price_row.price is not null
       or v_no_price_row.change_amount is not null
       or v_no_price_row.change_percent is not null
       or v_no_price_row.price_at is not null
       or v_no_price_row.price_is_stale is not null
       or v_no_price_row.stale_reason is not null then
        raise exception 'missing-price watchlist row uses fake values: %',
            v_no_price_row;
    end if;
end;
$$;

-- Null and range errors are stable fail-closed domain codes.
do $$
begin
    begin
        perform public.get_my_watchlist(null, 0);
        raise exception 'null limit was accepted';
    exception when others then
        if sqlerrm = 'null limit was accepted'
           or sqlerrm <> 'watchlist_limit_out_of_range' then raise; end if;
    end;
    begin
        perform public.get_my_watchlist(0, 0);
        raise exception 'zero limit was accepted';
    exception when others then
        if sqlerrm = 'zero limit was accepted'
           or sqlerrm <> 'watchlist_limit_out_of_range' then raise; end if;
    end;
    begin
        perform public.get_my_watchlist(101, 0);
        raise exception 'oversized limit was accepted';
    exception when others then
        if sqlerrm = 'oversized limit was accepted'
           or sqlerrm <> 'watchlist_limit_out_of_range' then raise; end if;
    end;
    begin
        perform public.get_my_watchlist(20, null);
        raise exception 'null offset was accepted';
    exception when others then
        if sqlerrm = 'null offset was accepted'
           or sqlerrm <> 'watchlist_offset_out_of_range' then raise; end if;
    end;
    begin
        perform public.get_my_watchlist(20, -1);
        raise exception 'negative offset was accepted';
    exception when others then
        if sqlerrm = 'negative offset was accepted'
           or sqlerrm <> 'watchlist_offset_out_of_range' then raise; end if;
    end;
    begin
        perform public.set_my_watchlist_item(null, true);
        raise exception 'null instrument was accepted';
    exception when others then
        if sqlerrm = 'null instrument was accepted'
           or sqlerrm <> 'watchlist_instrument_id_required' then raise; end if;
    end;
    begin
        perform public.set_my_watchlist_item(
            '20000000-0000-0000-0000-000000000001', null
        );
        raise exception 'null watched state was accepted';
    exception when others then
        if sqlerrm = 'null watched state was accepted'
           or sqlerrm <> 'watchlist_state_required' then raise; end if;
    end;
    begin
        perform public.set_my_watchlist_item(
            '29999999-9999-9999-9999-999999999999', true
        );
        raise exception 'missing instrument was accepted';
    exception when others then
        if sqlerrm = 'missing instrument was accepted'
           or sqlerrm <> 'watchlist_instrument_not_found' then raise; end if;
    end;
    begin
        perform public.set_my_watchlist_item(
            '20000000-0000-0000-0000-000000000025', true
        );
        raise exception 'inactive instrument was accepted';
    exception when others then
        if sqlerrm = 'inactive instrument was accepted'
           or sqlerrm <> 'watchlist_instrument_inactive' then raise; end if;
    end;
end;
$$;

-- A watched instrument remains visible after deactivation and can be removed.
select public.set_my_watchlist_item(
    '20000000-0000-0000-0000-000000000024', true
);
reset role;
update public.instruments
set is_active = false
where id = '20000000-0000-0000-0000-000000000024';
set local role authenticated;
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000081',
    true
);
do $$
declare
    v_item record;
    v_replay record;
    v_remove record;
begin
    select * into v_item
    from public.get_my_watchlist(100, 0)
    where instrument_id = '20000000-0000-0000-0000-000000000024';
    if v_item.watchlist_id is null or v_item.is_active then
        raise exception 'deactivated watched instrument is not visible: %', v_item;
    end if;

    select * into v_replay
    from public.set_my_watchlist_item(
        '20000000-0000-0000-0000-000000000024', true
    );
    if not v_replay.is_watched or v_replay.was_changed
       or v_replay.watchlist_id <> v_item.watchlist_id then
        raise exception 'existing inactive watchlist replay failed: %', v_replay;
    end if;

    select * into v_remove
    from public.set_my_watchlist_item(
        '20000000-0000-0000-0000-000000000024', false
    );
    if v_remove.is_watched or not v_remove.was_changed
       or v_remove.watchlist_id is not null then
        raise exception 'deactivated watched instrument was not removable: %',
            v_remove;
    end if;
end;
$$;

-- Reset clears investments and manual cash but leaves the watchlist intact.
select public.set_portfolio_cash(
    '10000000-0000-0000-0000-000000000081', 500, 0
);
select public.record_portfolio_transaction(
    '10000000-0000-0000-0000-000000000081',
    '20000000-0000-0000-0000-000000000001',
    'BUY', 1, 100, 'TRY', now(),
    '30000000-0000-0000-0000-000000000081', null
);
select public.reset_my_investments(
    '30000000-0000-0000-0000-000000000082',
    'RESET_ALL_INVESTMENTS'
);
reset role;

do $$
declare
    v_cash jsonb;
begin
    if exists (
        select 1 from public.portfolio_transactions
        where portfolio_id = '10000000-0000-0000-0000-000000000081'
    ) or exists (
        select 1 from public.portfolio_cash_accounts
        where portfolio_id = '10000000-0000-0000-0000-000000000081'
    ) then
        raise exception 'reset did not clear portfolio investment data';
    end if;
    if not exists (
        select 1 from public.portfolios
        where id = '10000000-0000-0000-0000-000000000081'
    ) then
        raise exception 'reset removed portfolio container';
    end if;
    if (select count(*) from public.user_watchlist_items
        where user_id = '00000000-0000-0000-0000-000000000081') <> 23 then
        raise exception 'reset changed watchlist rows';
    end if;
    v_cash := public.get_portfolio_cash('10000000-0000-0000-0000-000000000081');
    if (v_cash->>'try_balance')::numeric <> 0 then
        raise exception 'reset did not clear manual cash: %', v_cash;
    end if;
end;
$$;

-- Privileges, RLS, overload count and earnings independence are catalog-tested.
do $$
declare
    v_get_definition text;
    v_set_definition text;
begin
    if has_table_privilege('anon', 'public.user_watchlist_items', 'SELECT')
       or has_table_privilege('authenticated', 'public.user_watchlist_items', 'SELECT')
       or has_table_privilege('authenticated', 'public.user_watchlist_items', 'INSERT')
       or has_table_privilege('authenticated', 'public.user_watchlist_items', 'DELETE') then
        raise exception 'mobile role has direct watchlist table privilege';
    end if;

    if has_function_privilege(
        'anon', 'public.get_my_watchlist(integer,integer)', 'EXECUTE'
    ) or has_function_privilege(
        'anon', 'public.set_my_watchlist_item(uuid,boolean)', 'EXECUTE'
    ) then
        raise exception 'anon can execute watchlist RPC';
    end if;

    if not has_function_privilege(
        'authenticated', 'public.get_my_watchlist(integer,integer)', 'EXECUTE'
    ) or not has_function_privilege(
        'authenticated', 'public.set_my_watchlist_item(uuid,boolean)', 'EXECUTE'
    ) then
        raise exception 'authenticated cannot execute watchlist RPC';
    end if;

    if (select count(*) from pg_catalog.pg_proc as p
        join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
        where n.nspname = 'public'
          and p.proname in ('get_my_watchlist', 'set_my_watchlist_item')) <> 2 then
        raise exception 'unexpected watchlist RPC overload exists';
    end if;

    if (select count(*)
        from pg_catalog.pg_policies
        where schemaname = 'public'
          and tablename = 'user_watchlist_items'
          and roles = array['authenticated']::name[]
          and cmd in ('SELECT', 'INSERT', 'DELETE')) <> 3
       or exists (
           select 1
           from pg_catalog.pg_policies
           where schemaname = 'public'
             and tablename = 'user_watchlist_items'
             and cmd = 'UPDATE'
       ) then
        raise exception 'watchlist RLS policy matrix is wrong';
    end if;

    if exists (
        select 1
        from pg_catalog.pg_proc as p
        join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
        cross join lateral pg_catalog.aclexplode(
            coalesce(
                p.proacl,
                pg_catalog.acldefault('f', p.proowner)
            )
        ) as acl
        where n.nspname = 'public'
          and p.proname in ('get_my_watchlist', 'set_my_watchlist_item')
          and acl.grantee = 0
          and acl.privilege_type = 'EXECUTE'
    ) then
        raise exception 'PUBLIC retains watchlist RPC execute privilege';
    end if;

    if (select count(*)
        from pg_catalog.pg_proc as p
        join pg_catalog.pg_namespace as n on n.oid = p.pronamespace
        join pg_catalog.pg_roles as owner on owner.oid = p.proowner
        where n.nspname = 'public'
          and p.proname in ('get_my_watchlist', 'set_my_watchlist_item')
          and p.prosecdef
          and owner.rolname = 'postgres'
          and 'search_path=""' = any(p.proconfig)) <> 2 then
        raise exception 'watchlist RPC owner/security/search_path contract is wrong';
    end if;

    select pg_catalog.pg_get_functiondef(
        'public.get_my_watchlist(integer,integer)'::regprocedure
    ) into v_get_definition;
    select pg_catalog.pg_get_functiondef(
        'public.set_my_watchlist_item(uuid,boolean)'::regprocedure
    ) into v_set_definition;

    if position('earnings_events' in v_get_definition) > 0
       or position('earnings_events' in v_set_definition) > 0 then
        raise exception 'watchlist RPC depends on earnings_events';
    end if;

    if not (select c.relrowsecurity
            from pg_catalog.pg_class as c
            join pg_catalog.pg_namespace as n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname = 'user_watchlist_items') then
        raise exception 'watchlist RLS is disabled';
    end if;
end;
$$;

set local role anon;
do $$
begin
    begin
        perform public.get_my_watchlist(20, 0);
        raise exception 'anon RPC call was accepted';
    exception when insufficient_privilege then
        null;
    end;
end;
$$;
reset role;

ROLLBACK;
