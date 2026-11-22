\set ON_ERROR_STOP on

begin;

insert into auth.users (id) values
    ('00000000-0000-0000-0000-000000000001'),
    ('00000000-0000-0000-0000-000000000002');

insert into public.portfolios (
    id, user_id, name, base_currency
) values (
    '10000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-000000000001',
    'Integration Portfolio',
    'TRY'
);

insert into public.instruments (
    id, market, symbol, company_name, is_active, asset_type, currency
) values
    (
        '20000000-0000-0000-0000-000000000001',
        'BIST100', 'TEST1', 'Test Instrument 1', true, 'stock', 'TRY'
    ),
    (
        '20000000-0000-0000-0000-000000000002',
        'BIST100', 'TEST2', 'Test Instrument 2', true, 'stock', 'TRY'
    ),
    (
        '20000000-0000-0000-0000-000000000003',
        'BIST100', 'TEST3', 'Inactive Instrument', false, 'stock', 'TRY'
    );

select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000001',
    true
);

do $$
declare
    v_result jsonb;
begin
    v_result := public.record_portfolio_transaction(
        '10000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000001',
        'buy', 10, 100, 'try',
        '2026-08-01 10:00:00+03',
        '30000000-0000-0000-0000-000000000001',
        'First lot'
    );

    if (v_result->>'was_replayed')::boolean then
        raise exception 'first request was incorrectly treated as replay';
    end if;
    if (v_result->>'current_quantity')::numeric <> 10 then
        raise exception 'unexpected first BUY quantity: %', v_result;
    end if;
    if (v_result->>'remaining_cost_basis')::numeric <> 1000 then
        raise exception 'unexpected first BUY cost: %', v_result;
    end if;
end;
$$;

-- Invalid numeric, currency, future timestamp, and inactive BUY inputs are
-- rejected before a transaction row can be created.
do $$
declare
    v_transaction_count bigint;
begin
    select count(*) into v_transaction_count
    from public.portfolio_transactions;

    begin
        perform public.record_portfolio_transaction(
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000001',
            'BUY', 'NaN'::numeric, 100, 'TRY',
            '2026-08-02 10:00:00+03',
            '30000000-0000-0000-0000-000000000021', null
        );
        raise exception 'expected invalid quantity error was not raised';
    exception when others then
        if sqlerrm = 'expected invalid quantity error was not raised'
           or sqlerrm <> 'portfolio_invalid_quantity' then
            raise;
        end if;
    end;

    begin
        perform public.record_portfolio_transaction(
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000001',
            'BUY', 1, 100, 'USD',
            '2026-08-02 10:00:00+03',
            '30000000-0000-0000-0000-000000000022', null
        );
        raise exception 'expected currency mismatch was not raised';
    exception when others then
        if sqlerrm = 'expected currency mismatch was not raised'
           or sqlerrm <> 'portfolio_instrument_currency_mismatch' then
            raise;
        end if;
    end;

    begin
        perform public.record_portfolio_transaction(
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000001',
            'BUY', 1, 100, 'TRY',
            '2099-01-01 10:00:00+03',
            '30000000-0000-0000-0000-000000000023', null
        );
        raise exception 'expected future transaction error was not raised';
    exception when others then
        if sqlerrm = 'expected future transaction error was not raised'
           or sqlerrm <> 'portfolio_future_transaction_not_allowed' then
            raise;
        end if;
    end;

    begin
        perform public.record_portfolio_transaction(
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000003',
            'BUY', 1, 100, 'TRY',
            '2026-08-02 10:00:00+03',
            '30000000-0000-0000-0000-000000000024', null
        );
        raise exception 'expected inactive instrument error was not raised';
    exception when others then
        if sqlerrm = 'expected inactive instrument error was not raised'
           or sqlerrm <> 'portfolio_instrument_inactive' then
            raise;
        end if;
    end;

    if (select count(*) from public.portfolio_transactions) <> v_transaction_count then
        raise exception 'invalid input validation mutated transactions';
    end if;
end;
$$;

-- Identical replay returns the existing transaction and creates no duplicate.
do $$
declare
    v_first_id uuid;
    v_result jsonb;
begin
    select id into v_first_id
    from public.portfolio_transactions
    where client_request_id = '30000000-0000-0000-0000-000000000001';

    v_result := public.record_portfolio_transaction(
        '10000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000001',
        'BUY', 10, 100, 'TRY',
        '2026-08-01 10:00:00+03',
        '30000000-0000-0000-0000-000000000001',
        'First lot'
    );

    if not (v_result->>'was_replayed')::boolean then
        raise exception 'identical retry was not treated as replay';
    end if;
    if (v_result->>'transaction_id')::uuid <> v_first_id then
        raise exception 'replay returned a different transaction';
    end if;
    if (select count(*) from public.portfolio_transactions) <> 1 then
        raise exception 'replay created a duplicate transaction';
    end if;
end;
$$;

-- Reusing a request UUID with a different payload must fail without mutation.
do $$
begin
    begin
        perform public.record_portfolio_transaction(
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000001',
            'BUY', 11, 100, 'TRY',
            '2026-08-01 10:00:00+03',
            '30000000-0000-0000-0000-000000000001',
            'First lot'
        );
        raise exception 'expected idempotency conflict was not raised';
    exception when others then
        if sqlerrm = 'expected idempotency conflict was not raised'
           or sqlerrm <> 'portfolio_idempotency_conflict' then
            raise;
        end if;
    end;

    if (select count(*) from public.portfolio_transactions) <> 1 then
        raise exception 'idempotency conflict mutated transactions';
    end if;
end;
$$;

select public.record_portfolio_transaction(
    '10000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000001',
    'BUY', 5, 120, 'TRY',
    '2026-08-05 10:00:00+03',
    '30000000-0000-0000-0000-000000000002',
    'Second lot'
);

-- SELL uses the weighted-average cost of the 10@100 and 5@120 purchases.
do $$
declare
    v_result jsonb;
begin
    v_result := public.record_portfolio_transaction(
        '10000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000001',
        'SELL', 12, 150, 'TRY',
        '2026-08-20 10:00:00+03',
        '30000000-0000-0000-0000-000000000003',
        'Weighted-average sale'
    );

    if (v_result->>'current_quantity')::numeric <> 3
       or (v_result->>'remaining_cost_basis')::numeric <> 320
       or (v_result->>'realized_pnl')::numeric <> 520 then
        raise exception 'unexpected weighted-average summary: %', v_result;
    end if;

 end;
$$;

-- Oversell must roll back the SELL without changing transaction history.
do $$
declare
    v_transaction_count bigint;
begin
    select count(*) into v_transaction_count
    from public.portfolio_transactions;

    begin
        perform public.record_portfolio_transaction(
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000001',
            'SELL', 4, 155, 'TRY',
            '2026-08-21 10:00:00+03',
            '30000000-0000-0000-0000-000000000004',
            'Oversell'
        );
        raise exception 'expected oversell error was not raised';
    exception when others then
        if sqlerrm = 'expected oversell error was not raised'
           or sqlerrm <> 'portfolio_insufficient_position' then
            raise;
        end if;
    end;

    if (select count(*) from public.portfolio_transactions) <> v_transaction_count then
        raise exception 'oversell did not roll back atomically';
    end if;
end;
$$;

-- A backdated purchase is included in the weighted-average history.
do $$
declare
    v_result jsonb;
begin
    perform public.record_portfolio_transaction(
        '10000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000002',
        'BUY', 10, 100, 'TRY',
        '2026-08-10 10:00:00+03',
        '30000000-0000-0000-0000-000000000005',
        null
    );
    perform public.record_portfolio_transaction(
        '10000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000002',
        'SELL', 8, 150, 'TRY',
        '2026-08-20 10:00:00+03',
        '30000000-0000-0000-0000-000000000006',
        null
    );
    v_result := public.record_portfolio_transaction(
        '10000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000002',
        'BUY', 5, 80, 'TRY',
        '2026-08-01 10:00:00+03',
        '30000000-0000-0000-0000-000000000007',
        'Backdated earlier lot'
    );

    if (v_result->>'current_quantity')::numeric <> 7
       or abs((v_result->>'remaining_cost_basis')::numeric - (1960::numeric / 3)) > 0.000001
       or abs((v_result->>'realized_pnl')::numeric - (1360::numeric / 3)) > 0.000001 then
        raise exception 'backdated weighted-average summary is incorrect: %', v_result;
    end if;
end;
$$;

-- A different authenticated user cannot mutate the first user's portfolio.
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000002',
    true
);

do $$
begin
    begin
        perform public.record_portfolio_transaction(
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000001',
            'BUY', 1, 100, 'TRY',
            '2026-08-22 10:00:00+03',
            '30000000-0000-0000-0000-000000000008',
            null
        );
        raise exception 'expected ownership error was not raised';
    exception when others then
        if sqlerrm = 'expected ownership error was not raised'
           or sqlerrm <> 'portfolio_not_found_or_forbidden' then
            raise;
        end if;
    end;
end;
$$;

do $$
begin
    if has_table_privilege(
        'authenticated',
        'public.portfolio_transactions',
        'INSERT'
    ) then
        raise exception 'authenticated still has direct transaction INSERT';
    end if;
    if has_table_privilege(
        'authenticated',
        'public.portfolio_transactions',
        'TRUNCATE'
    ) then
        raise exception 'authenticated still has direct transaction TRUNCATE';
    end if;
    if not has_table_privilege(
        'authenticated',
        'public.portfolio_transactions',
        'SELECT'
    ) then
        raise exception 'authenticated cannot read owned transactions';
    end if;
    if not has_function_privilege(
        'authenticated',
        'public.record_portfolio_transaction(uuid,uuid,text,numeric,numeric,text,timestamp with time zone,uuid,text)',
        'EXECUTE'
    ) then
        raise exception 'authenticated cannot execute transaction RPC';
    end if;
    if has_schema_privilege('authenticated', 'private', 'USAGE') then
        raise exception 'authenticated can access private helper schema';
    end if;
end;
$$;

-- Execute the public function through the same database role used by
-- Supabase Anonymous Sign-In sessions, not only through the migration owner.
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000001',
    true
);
set local role authenticated;
select public.record_portfolio_transaction(
    '10000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000001',
    'BUY', 10, 100, 'TRY',
    '2026-08-01 10:00:00+03',
    '30000000-0000-0000-0000-000000000001',
    'First lot'
);
reset role;

rollback;
