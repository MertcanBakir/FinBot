\set ON_ERROR_STOP on
begin;

insert into auth.users (id) values
    ('00000000-0000-0000-0000-000000000091'),
    ('00000000-0000-0000-0000-000000000092');

insert into public.portfolios (
    id, user_id, name, base_currency, is_default, archived_at
) values
    (
        '10000000-0000-0000-0000-000000000091',
        '00000000-0000-0000-0000-000000000091',
        'Activity A', 'TRY', true, null
    ),
    (
        '10000000-0000-0000-0000-000000000092',
        '00000000-0000-0000-0000-000000000092',
        'Activity B', 'TRY', true, null
    ),
    (
        '10000000-0000-0000-0000-000000000093',
        '00000000-0000-0000-0000-000000000091',
        'Archived A', 'TRY', false, now()
    );

insert into public.instruments (
    id, market, symbol, company_name, is_active, asset_type, currency
) values
    (
        '20000000-0000-0000-0000-000000000091',
        'BIST100', 'ACTA', 'Activity Alpha', true, 'stock', 'TRY'
    ),
    (
        '20000000-0000-0000-0000-000000000092',
        'NASDAQ100', 'ACTB', 'Activity Beta', true, 'stock', 'USD'
    ),
    (
        '20000000-0000-0000-0000-000000000093',
        'BIST100', 'OLDX', 'Inactive Historical Company', false, 'stock', 'TRY'
    );

insert into public.portfolio_transactions (
    id, portfolio_id, instrument_id, transaction_type, quantity, unit_price,
    transaction_currency, transaction_at, notes, created_at, updated_at,
    client_request_id, is_reversed, reversed_at, reversal_reason,
    reversal_client_request_id
) values
    (
        '30000000-0000-0000-0000-000000000091',
        '10000000-0000-0000-0000-000000000091',
        '20000000-0000-0000-0000-000000000091',
        'BUY', 2, 100, 'TRY',
        '2026-08-31 11:00:00+03', 'first buy',
        '2026-08-31 11:01:00+03', '2026-08-31 11:01:00+03',
        '40000000-0000-0000-0000-000000000091',
        false, null, null, null
    ),
    (
        '30000000-0000-0000-0000-000000000092',
        '10000000-0000-0000-0000-000000000091',
        '20000000-0000-0000-0000-000000000092',
        'SELL', 1.5, 80, 'USD',
        '2026-08-31 13:00:00+03', 'reversed sell',
        '2026-08-31 13:01:00+03', '2026-08-31 13:02:00+03',
        '40000000-0000-0000-0000-000000000092',
        true, '2026-08-31 13:02:00+03', 'user correction',
        '50000000-0000-0000-0000-000000000092'
    ),
    (
        '30000000-0000-0000-0000-000000000093',
        '10000000-0000-0000-0000-000000000091',
        '20000000-0000-0000-0000-000000000091',
        'BUY', 3, 70, 'TRY',
        '2026-08-31 14:00:00+03', 'tie transaction low id',
        '2026-08-31 14:01:00+03', '2026-08-31 14:01:00+03',
        '40000000-0000-0000-0000-000000000093',
        false, null, null, null
    ),
    (
        '30000000-0000-0000-0000-000000000094',
        '10000000-0000-0000-0000-000000000091',
        '20000000-0000-0000-0000-000000000092',
        'BUY', 4, 60, 'USD',
        '2026-08-31 14:00:00+03', 'tie transaction high id',
        '2026-08-31 14:01:00+03', '2026-08-31 14:01:00+03',
        '40000000-0000-0000-0000-000000000094',
        false, null, null, null
    ),
    (
        '30000000-0000-0000-0000-000000000095',
        '10000000-0000-0000-0000-000000000091',
        '20000000-0000-0000-0000-000000000093',
        'BUY', 5, 50, 'TRY',
        '2026-08-31 15:00:00+03', 'inactive instrument history',
        '2026-08-31 15:01:00+03', '2026-08-31 15:01:00+03',
        '40000000-0000-0000-0000-000000000095',
        false, null, null, null
    ),
    (
        '30000000-0000-0000-0000-000000000096',
        '10000000-0000-0000-0000-000000000092',
        '20000000-0000-0000-0000-000000000091',
        'BUY', 1, 10, 'TRY',
        '2026-08-31 10:00:00+03', 'user B row',
        '2026-08-31 10:01:00+03', '2026-08-31 10:01:00+03',
        '40000000-0000-0000-0000-000000000096',
        false, null, null, null
    );

do $$
begin
    if has_function_privilege(
        'anon',
        'public.get_my_portfolio_activity(uuid,integer,text,text,timestamp with time zone,timestamp with time zone,text,uuid)',
        'EXECUTE'
    ) then
        raise exception 'anon can execute portfolio activity RPC';
    end if;
    if not has_function_privilege(
        'authenticated',
        'public.get_my_portfolio_activity(uuid,integer,text,text,timestamp with time zone,timestamp with time zone,text,uuid)',
        'EXECUTE'
    ) then
        raise exception 'authenticated cannot execute portfolio activity RPC';
    end if;
end;
$$;

set local role anon;
do $$
begin
    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000091'
        );
        raise exception 'anon portfolio activity call was accepted';
    exception when insufficient_privilege then
        null;
    end;
end;
$$;
reset role;

set local role authenticated;
select set_config('request.jwt.claim.sub', '', true);
do $$
begin
    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000091'
        );
        raise exception 'unauthenticated activity call was accepted';
    exception when others then
        if sqlerrm = 'unauthenticated activity call was accepted'
           or sqlerrm <> 'portfolio_activity_authentication_required' then
            raise;
        end if;
    end;
end;
$$;

select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000091',
    true
);

do $$
declare
    v_ids uuid[];
    v_sources text[];
    v_activity record;
begin
    select array_agg(a.activity_id order by a.occurred_at desc, a.created_at desc,
                     a.source_kind desc, a.activity_id desc),
           array_agg(a.source_kind order by a.occurred_at desc, a.created_at desc,
                     a.source_kind desc, a.activity_id desc)
    into v_ids, v_sources
    from public.get_my_portfolio_activity(
        '10000000-0000-0000-0000-000000000091', 100
    ) as a;

    if cardinality(v_ids) <> 5 then
        raise exception 'activity row count is wrong: %', v_ids;
    end if;
    if v_ids <> array[
        '30000000-0000-0000-0000-000000000095'::uuid,
        '30000000-0000-0000-0000-000000000094'::uuid,
        '30000000-0000-0000-0000-000000000093'::uuid,
        '30000000-0000-0000-0000-000000000092'::uuid,
        '30000000-0000-0000-0000-000000000091'::uuid
    ] then
        raise exception 'deterministic transaction ordering failed: %', v_ids;
    end if;
    if v_sources <> array_fill('TRANSACTION'::text, array[5]) then
        raise exception 'unexpected activity source kinds: %', v_sources;
    end if;

    select * into v_activity
    from public.get_my_portfolio_activity(
        '10000000-0000-0000-0000-000000000091', 100, 'SELL'
    );
    if v_activity.activity_id <> '30000000-0000-0000-0000-000000000092'
       or not v_activity.is_reversed or v_activity.reversed_at is null
       or v_activity.reversal_reason <> 'user correction'
       or v_activity.gross_amount <> 120 or v_activity.currency <> 'USD' then
        raise exception 'reversed SELL mapping is wrong: %', v_activity;
    end if;

    if (select count(*) from public.get_my_portfolio_activity(
        '10000000-0000-0000-0000-000000000091', 100, 'ALL'
    )) <> 5 then
        raise exception 'ALL filter count is wrong';
    end if;
    if (select count(*) from public.get_my_portfolio_activity(
        '10000000-0000-0000-0000-000000000091', 100, 'BUY'
    )) <> 4 then
        raise exception 'BUY filter count is wrong';
    end if;
    if (select count(*) from public.get_my_portfolio_activity(
        '10000000-0000-0000-0000-000000000091', 100, 'SELL'
    )) <> 1 then
        raise exception 'SELL filter count is wrong';
    end if;
    if (select count(*) from public.get_my_portfolio_activity(
        '10000000-0000-0000-0000-000000000091', 100, 'ALL', ' beta '
    )) <> 2 then
        raise exception 'company-name search count is wrong';
    end if;
    if (select count(*) from public.get_my_portfolio_activity(
        '10000000-0000-0000-0000-000000000091', 100, 'ALL', ' historical '
    )) <> 1 then
        raise exception 'inactive instrument history/search was lost';
    end if;
end;
$$;

create temporary table collected_activity_ids (
    activity_id uuid primary key
) on commit drop;

do $$
declare
    v_before_occurred_at timestamp with time zone := null;
    v_before_created_at timestamp with time zone := null;
    v_before_source_kind text := null;
    v_before_activity_id uuid := null;
    v_row record;
    v_page_count integer;
    v_page_has_more boolean;
    v_page_no integer := 0;
begin
    loop
        v_page_count := 0;
        v_page_has_more := false;
        v_page_no := v_page_no + 1;

        for v_row in
            select *
            from public.get_my_portfolio_activity(
                '10000000-0000-0000-0000-000000000091',
                2, 'ALL', null,
                v_before_occurred_at, v_before_created_at,
                v_before_source_kind, v_before_activity_id
            )
        loop
            insert into collected_activity_ids values (v_row.activity_id);
            v_page_count := v_page_count + 1;
            if v_page_count = 1 then
                v_page_has_more := v_row.has_more;
            elsif v_page_has_more is distinct from v_row.has_more then
                raise exception 'has_more differs within page %', v_page_no;
            end if;

            v_before_occurred_at := v_row.occurred_at;
            v_before_created_at := v_row.created_at;
            v_before_source_kind := v_row.source_kind;
            v_before_activity_id := v_row.activity_id;
        end loop;

        if v_page_count = 0 or not v_page_has_more then
            exit;
        end if;
        if v_page_no > 10 then
            raise exception 'pagination did not terminate';
        end if;
    end loop;

    if (select count(*) from collected_activity_ids) <> 5 then
        raise exception 'pagination skipped rows';
    end if;
    if v_page_no <> 3 or v_page_has_more then
        raise exception 'last page metadata is wrong: page %, has_more %',
            v_page_no, v_page_has_more;
    end if;
end;
$$;

do $$
declare
    v_message_one text;
    v_message_two text;
begin
    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000091', 0
        );
        raise exception 'zero limit was accepted';
    exception when others then
        if sqlerrm = 'zero limit was accepted'
           or sqlerrm <> 'portfolio_activity_limit_out_of_range' then raise; end if;
    end;

    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000091', 20, 'BROKEN'
        );
        raise exception 'invalid filter was accepted';
    exception when others then
        if sqlerrm = 'invalid filter was accepted'
           or sqlerrm <> 'portfolio_activity_invalid_filter' then raise; end if;
    end;

    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000091', 20, 'ALL',
            repeat('x', 101)
        );
        raise exception 'oversized search was accepted';
    exception when others then
        if sqlerrm = 'oversized search was accepted'
           or sqlerrm <> 'portfolio_activity_search_too_long' then raise; end if;
    end;

    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000091', 20, 'ALL', null,
            '2026-08-31 12:00:00+03', null, null, null
        );
        raise exception 'partial cursor was accepted';
    exception when others then
        if sqlerrm = 'partial cursor was accepted'
           or sqlerrm <> 'portfolio_activity_incomplete_cursor' then raise; end if;
    end;

    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000091', 20, 'ALL', null,
            '2026-08-31 12:00:00+03', '2026-08-31 12:01:00+03',
            'CASH_FLOW', '30000000-0000-0000-0000-000000000091'
        );
        raise exception 'invalid cursor source was accepted';
    exception when others then
        if sqlerrm = 'invalid cursor source was accepted'
           or sqlerrm <> 'portfolio_activity_invalid_cursor_source' then raise; end if;
    end;

    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000092'
        );
    exception when others then v_message_one := sqlerrm; end;
    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000093'
        );
    exception when others then v_message_two := sqlerrm; end;

    if v_message_one <> 'portfolio_activity_not_found_or_access_denied'
       or v_message_two <> v_message_one then
        raise exception 'ownership/archive errors leak existence: %, %',
            v_message_one, v_message_two;
    end if;
end;
$$;

-- User B cannot access A's portfolio through the activity RPC.
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000092',
    true
);
do $$
begin
    begin
        perform public.get_my_portfolio_activity(
            '10000000-0000-0000-0000-000000000091'
        );
        raise exception 'user b cannot access a''s portfolio';
    exception when others then
        if sqlerrm = 'user b cannot access a''s portfolio'
           or sqlerrm <> 'portfolio_activity_not_found_or_access_denied' then
            raise;
        end if;
    end;
end;
$$;

do $$
declare
    v_definition text;
begin
    if has_table_privilege('anon', 'public.portfolio_transactions', 'SELECT') then
        raise exception 'anon can read portfolio transactions directly';
    end if;
    if not has_table_privilege(
        'authenticated', 'public.portfolio_transactions', 'SELECT'
    ) then
        raise exception 'authenticated lost portfolio transaction read access';
    end if;

    select pg_get_functiondef(
        'public.get_my_portfolio_activity(uuid,integer,text,text,timestamp with time zone,timestamp with time zone,text,uuid)'::regprocedure
    ) into v_definition;
    if position('SECURITY DEFINER' in upper(v_definition)) = 0 then
        raise exception 'activity RPC is no longer security definer';
    end if;
end;
$$;

reset role;
rollback;
