\set ON_ERROR_STOP on
begin;

-- Cash flows, reversals, and FIFO matches were retired by the simplified
-- portfolio model. Manual balances remain available through the cash RPC.
do $$
begin
    if to_regprocedure(
        'public.record_cash_flow(uuid,text,numeric,text,timestamp with time zone,uuid,text)'
    ) is not null then
        raise exception 'retired record_cash_flow RPC still exists';
    end if;
    if to_regprocedure(
        'public.reverse_cash_flow(uuid,uuid,text)'
    ) is not null then
        raise exception 'retired reverse_cash_flow RPC still exists';
    end if;
    if to_regclass('public.portfolio_cash_flows') is not null then
        raise exception 'retired portfolio_cash_flows table still exists';
    end if;
    if to_regclass('public.portfolio_fifo_matches') is not null then
        raise exception 'retired portfolio_fifo_matches table still exists';
    end if;
    if to_regprocedure('public.get_portfolio_cash(uuid)') is null
       or to_regprocedure('public.set_portfolio_cash(uuid,numeric,numeric)') is null then
        raise exception 'manual portfolio cash RPCs are missing';
    end if;
    if not exists (
        select 1 from information_schema.columns
        where table_schema='public'
          and table_name='portfolio_cash_accounts'
          and column_name='current_balance'
    ) then
        raise exception 'manual cash balance column is missing';
    end if;
end;
$$;

rollback;
