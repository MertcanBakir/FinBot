\set ON_ERROR_STOP on
begin;

insert into auth.users (id) values
    ('10000000-0000-0000-0000-000000000001');

select set_config(
    'request.jwt.claim.sub',
    '10000000-0000-0000-0000-000000000001',
    true
);

insert into public.instruments (
    id, market, symbol, company_name, asset_type, currency, is_active
) values (
    '20000000-0000-0000-0000-000000000001',
    'BIST100', 'TEST', 'Test Instrument', 'stock', 'TRY', true
);

insert into public.portfolios (
    id, user_id, name, base_currency, is_default
) values (
    '30000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    'Test portfolio', 'TRY', true
);

do $$
declare
    v_cash jsonb;
    v_result jsonb;
begin
    v_cash := public.set_portfolio_cash(
        '30000000-0000-0000-0000-000000000001', 500, 100
    );
    if (v_cash->>'try_balance')::numeric <> 500
       or (v_cash->>'usd_balance')::numeric <> 100 then
        raise exception 'manual cash set returned wrong balances: %', v_cash;
    end if;

    v_cash := public.get_portfolio_cash(
        '30000000-0000-0000-0000-000000000001'
    );
    if (v_cash->>'try_balance')::numeric <> 500
       or (v_cash->>'usd_balance')::numeric <> 100 then
        raise exception 'manual cash read returned wrong balances: %', v_cash;
    end if;

    v_result := public.record_portfolio_transaction(
        '30000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000001',
        'BUY', 1, 100, 'TRY', now(),
        '40000000-0000-0000-0000-000000000002', null
    );
    if (v_result->>'current_quantity')::numeric <> 1 then
        raise exception 'transaction position assertion failed: %', v_result;
    end if;

    v_cash := public.get_portfolio_cash(
        '30000000-0000-0000-0000-000000000001'
    );
    if (v_cash->>'try_balance')::numeric <> 500
       or (v_cash->>'usd_balance')::numeric <> 100 then
        raise exception 'trade unexpectedly changed manual cash: %', v_cash;
    end if;

    begin
        perform public.set_portfolio_cash(
            '30000000-0000-0000-0000-000000000001', -1, 100
        );
        raise exception 'negative manual cash was accepted';
    exception when others then
        if sqlerrm = 'negative manual cash was accepted'
           or sqlerrm <> 'portfolio_invalid_cash_amount' then
            raise;
        end if;
    end;

    if has_function_privilege(
        'anon', 'public.get_portfolio_cash(uuid)', 'EXECUTE'
    ) or has_function_privilege(
        'anon', 'public.set_portfolio_cash(uuid,numeric,numeric)', 'EXECUTE'
    ) then
        raise exception 'anon can execute manual cash RPC';
    end if;
    if not has_function_privilege(
        'authenticated', 'public.get_portfolio_cash(uuid)', 'EXECUTE'
    ) or not has_function_privilege(
        'authenticated', 'public.set_portfolio_cash(uuid,numeric,numeric)', 'EXECUTE'
    ) then
        raise exception 'authenticated cannot execute manual cash RPC';
    end if;
end;
$$;

rollback;
