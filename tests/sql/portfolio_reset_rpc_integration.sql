\set ON_ERROR_STOP on
begin;

insert into auth.users (id) values
    ('00000000-0000-0000-0000-000000000031'),
    ('00000000-0000-0000-0000-000000000032');

insert into public.portfolios (id, user_id, name, base_currency, is_default) values
    ('10000000-0000-0000-0000-000000000031',
     '00000000-0000-0000-0000-000000000031', 'Reset Me', 'TRY', true),
    ('10000000-0000-0000-0000-000000000032',
     '00000000-0000-0000-0000-000000000032', 'Keep Me', 'TRY', true);

insert into public.instruments (
    id, market, symbol, company_name, is_active, asset_type, currency
) values (
    '20000000-0000-0000-0000-000000000031',
    'BIST100', 'RESET1', 'Reset Test Instrument', true, 'stock', 'TRY'
);

select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000031',
    true
);
select public.set_portfolio_cash(
    '10000000-0000-0000-0000-000000000031', 10000, 0
);
select public.record_portfolio_transaction(
    '10000000-0000-0000-0000-000000000031',
    '20000000-0000-0000-0000-000000000031', 'BUY', 10, 100, 'TRY',
    now() - interval '1 hour',
    '30000000-0000-0000-0000-000000000032', null
);

-- User B has investment and manual cash data that must survive A's reset.
select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000032',
    true
);
select public.set_portfolio_cash(
    '10000000-0000-0000-0000-000000000032', 500, 0
);
select public.record_portfolio_transaction(
    '10000000-0000-0000-0000-000000000032',
    '20000000-0000-0000-0000-000000000031', 'BUY', 1, 100, 'TRY',
    now() - interval '1 hour',
    '30000000-0000-0000-0000-000000000033', null
);

select set_config(
    'request.jwt.claim.sub',
    '00000000-0000-0000-0000-000000000031',
    true
);

-- Invalid confirmation is rejected without changing investment state.
do $$
begin
    begin
        perform public.reset_my_investments(
            '30000000-0000-0000-0000-000000000034', 'WRONG_CONFIRMATION'
        );
        raise exception 'invalid reset confirmation was accepted';
    exception when others then
        if sqlerrm = 'invalid reset confirmation was accepted'
           or sqlerrm <> 'portfolio_reset_confirmation_required' then
            raise;
        end if;
    end;

    if not exists (
        select 1 from public.portfolio_transactions
        where portfolio_id = '10000000-0000-0000-0000-000000000031'
    ) then
        raise exception 'invalid confirmation changed transaction data';
    end if;
end;
$$;

-- Valid reset preserves the portfolio, removes its investments, and clears
-- manually entered cash.
do $$
declare
    v_result jsonb;
    v_cash jsonb;
begin
    v_result := public.reset_my_investments(
        '30000000-0000-0000-0000-000000000035',
        'RESET_ALL_INVESTMENTS'
    );

    if (v_result->>'was_replayed')::boolean
       or (v_result->>'portfolio_count')::integer <> 1
       or (v_result->>'deleted_transaction_count')::integer <> 1
       or (v_result->>'deleted_instrument_count')::integer <> 1
       or (v_result->>'deleted_snapshot_count')::integer <> 0
       or (v_result->>'deleted_snapshot_position_count')::integer <> 0
       or (v_result->>'deleted_request_key_count')::integer <> 1 then
        raise exception 'unexpected reset result: %', v_result;
    end if;

    if not exists (
        select 1 from public.portfolios
        where id = '10000000-0000-0000-0000-000000000031'
    ) then
        raise exception 'reset deleted the portfolio container';
    end if;

    if exists (
        select 1 from public.portfolio_transactions
        where portfolio_id = '10000000-0000-0000-0000-000000000031'
        union all
        select 1 from public.portfolio_instruments
        where portfolio_id = '10000000-0000-0000-0000-000000000031'
    ) then
        raise exception 'reset left investment rows behind';
    end if;

    v_cash := public.get_portfolio_cash('10000000-0000-0000-0000-000000000031');
    if (v_cash->>'try_balance')::numeric <> 0
       or (v_cash->>'usd_balance')::numeric <> 0 then
        raise exception 'reset did not clear manual cash: %', v_cash;
    end if;

    if not exists (
        select 1 from public.portfolio_transactions
        where portfolio_id = '10000000-0000-0000-0000-000000000032'
    ) then
        raise exception 'reset changed another user transaction data';
    end if;
    perform set_config(
        'request.jwt.claim.sub',
        '00000000-0000-0000-0000-000000000032',
        true
    );
    v_cash := public.get_portfolio_cash('10000000-0000-0000-0000-000000000032');
    if (v_cash->>'try_balance')::numeric <> 500 then
        raise exception 'reset changed another user cash data: %', v_cash;
    end if;
    perform set_config(
        'request.jwt.claim.sub',
        '00000000-0000-0000-0000-000000000031',
        true
    );
end;
$$;

-- A retry with the same request ID must not clear data created after reset.
select public.set_portfolio_cash(
    '10000000-0000-0000-0000-000000000031', 250, 0
);
select public.record_portfolio_transaction(
    '10000000-0000-0000-0000-000000000031',
    '20000000-0000-0000-0000-000000000031', 'BUY', 2, 80, 'TRY',
    now() - interval '30 minutes',
    '30000000-0000-0000-0000-000000000036', null
);

do $$
declare
    v_result jsonb;
    v_cash jsonb;
begin
    v_result := public.reset_my_investments(
        '30000000-0000-0000-0000-000000000035',
        'RESET_ALL_INVESTMENTS'
    );
    if not (v_result->>'was_replayed')::boolean then
        raise exception 'reset retry was not replayed';
    end if;
    if not exists (
        select 1 from public.portfolio_transactions
        where client_request_id = '30000000-0000-0000-0000-000000000036'
    ) then
        raise exception 'reset retry deleted newer transaction data';
    end if;
    v_cash := public.get_portfolio_cash('10000000-0000-0000-0000-000000000031');
    if (v_cash->>'try_balance')::numeric <> 250 then
        raise exception 'reset retry deleted newer cash data: %', v_cash;
    end if;
end;
$$;

-- A new reset request clears data created after the first reset.
select public.reset_my_investments(
    '30000000-0000-0000-0000-000000000037',
    'RESET_ALL_INVESTMENTS'
);

do $$
begin
    if exists (
        select 1 from public.portfolio_transactions
        where portfolio_id = '10000000-0000-0000-0000-000000000031'
    ) or exists (
        select 1 from public.portfolio_cash_accounts
        where portfolio_id = '10000000-0000-0000-0000-000000000031'
    ) then
        raise exception 'new reset request did not clear newer investment data';
    end if;

    if has_function_privilege(
        'anon',
        'public.reset_my_investments(uuid,text)',
        'EXECUTE'
    ) then
        raise exception 'anon can execute investment reset RPC';
    end if;
    if not has_function_privilege(
        'authenticated',
        'public.reset_my_investments(uuid,text)',
        'EXECUTE'
    ) then
        raise exception 'authenticated cannot execute investment reset RPC';
    end if;
end;
$$;

rollback;
