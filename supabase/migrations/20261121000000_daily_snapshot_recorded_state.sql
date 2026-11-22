-- Nightly reconciliation reuses the last valuation captured on the requested day.
-- Never rebuild a past day using today's manual cash, latest prices or exchange rate.
-- Apply before deploying the Python caller. Rollback: restore the previous caller,
-- then DROP FUNCTION private.resolve_daily_portfolio_snapshot(uuid,date).
begin;

create function private.resolve_daily_portfolio_snapshot(
    p_portfolio_id uuid, p_snapshot_date date
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_today date := (pg_catalog.now() at time zone 'Europe/Istanbul')::date;
    v_snapshot public.portfolio_snapshots%rowtype;
begin
    if p_snapshot_date is null or p_snapshot_date > v_today then
        raise exception using errcode = 'P0001', message = 'portfolio_invalid_snapshot_date';
    end if;

    perform 1 from public.portfolios
    where id = p_portfolio_id and archived_at is null
    for update;
    if not found then
        raise exception using errcode = 'P0001', message = 'portfolio_snapshot_portfolio_not_found';
    end if;

    if p_snapshot_date = v_today then
        return private.build_portfolio_snapshot(p_portfolio_id, p_snapshot_date)
            || pg_catalog.jsonb_build_object('reused', false);
    end if;

    select * into v_snapshot from public.portfolio_snapshots
    where portfolio_id = p_portfolio_id and snapshot_date = p_snapshot_date
      and calculation_version = 2
      and captured_at >= (p_snapshot_date::timestamp at time zone 'Europe/Istanbul')
      and captured_at < ((p_snapshot_date + 1)::timestamp at time zone 'Europe/Istanbul');
    if not found then
        -- Missing history is observable but must not create invented valuations.
        return pg_catalog.jsonb_build_object(
            'valuation_status', 'missing', 'reused', false,
            'warnings', pg_catalog.jsonb_build_array('portfolio_snapshot_not_captured')
        );
    end if;

    return pg_catalog.jsonb_build_object(
        'snapshot_id', v_snapshot.id,
        'snapshot_date', v_snapshot.snapshot_date,
        'valuation_status', v_snapshot.valuation_status,
        'total_value_base', v_snapshot.total_value_base::text,
        'warnings', v_snapshot.warnings,
        'captured_at', v_snapshot.captured_at,
        'reused', true
    );
end;
$$;

alter function private.resolve_daily_portfolio_snapshot(uuid,date) owner to postgres;
revoke all on function private.resolve_daily_portfolio_snapshot(uuid,date)
    from public, anon, authenticated;
grant execute on function private.resolve_daily_portfolio_snapshot(uuid,date) to service_role;
comment on function private.resolve_daily_portfolio_snapshot(uuid,date) is
    'Backend only: current-day valuation or read-only reuse of a same-day captured historical valuation. No backfill.';
commit;
