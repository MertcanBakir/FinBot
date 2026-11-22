import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260830000000_user_watchlist_rpc.sql"
SQL_TEST = ROOT / "tests" / "sql" / "user_watchlist_rpc_test.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_watchlist_migration_prefix_is_unique_and_ordered():
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    prefixes = [name.split("_", 1)[0] for name in names]

    assert MIGRATION.name in names
    assert len(prefixes) == len(set(prefixes))
    assert all(
        name < MIGRATION.name
        for name in names
        if name != MIGRATION.name and name.startswith("20260829")
    )


def test_watchlist_table_rls_and_direct_mobile_privileges_are_closed():
    sql = _sql()

    assert "create table public.user_watchlist_items" in sql
    assert "unique (user_id, instrument_id)" in sql
    assert "user_id, created_at desc, id desc" in sql
    assert "alter table public.user_watchlist_items enable row level security" in sql
    assert len(re.findall(r"create policy", sql, re.IGNORECASE)) == 3
    assert "for update" not in sql.lower()
    assert re.search(
        r"revoke all privileges on table public\.user_watchlist_items\s+"
        r"from public, anon, authenticated",
        sql,
        re.IGNORECASE,
    )


def test_watchlist_rpc_contract_and_security_are_explicit():
    sql = _sql()

    assert "create function public.get_my_watchlist(" in sql
    assert "create function public.set_my_watchlist_item(" in sql
    assert sql.lower().count("security definer") == 2
    assert sql.lower().count("set search_path = ''") == 2
    assert sql.count("auth.uid()") >= 2
    assert "earnings_events" not in sql
    assert "left join public.instrument_latest_prices" in sql
    assert "count(*) over () as total_count" in sql
    assert "order by w.created_at desc, w.id desc" in sql
    assert "limit p_limit" in sql
    assert "offset p_offset" in sql
    assert "when lp.instrument_id is null then null::boolean" in sql
    assert (
        "on conflict on constraint user_watchlist_items_user_instrument_key"
        in sql
    )

    for signature in (
        "public.get_my_watchlist(integer, integer)",
        "public.set_my_watchlist_item(uuid, boolean)",
    ):
        assert f"revoke all on function {signature}" in sql
        assert f"grant execute on function {signature}" in sql


def test_watchlist_migration_does_not_change_reset_or_python_jobs():
    sql = _sql()

    assert "create or replace function public.reset_my_investments" not in sql
    assert "drop function public.reset_my_investments" not in sql
    assert "delete from public.portfolio_" not in sql
    assert "update public.portfolio_" not in sql


def test_watchlist_real_sql_acceptance_suite_is_present():
    content = SQL_TEST.read_text(encoding="utf-8")
    normalized = content.lower()

    required_markers = (
        "set local role anon;",
        "set local role authenticated;",
        "watchlist_authentication_required",
        "watchlist_instrument_inactive",
        "public.get_my_watchlist(20, 0)",
        "public.get_my_watchlist(20, 20)",
        "public.reset_my_investments(",
        "has_table_privilege",
        "has_function_privilege",
        "rollback;",
    )
    for marker in required_markers:
        assert marker.lower() in normalized

    assert "earnings_events" in content
    assert "pg_get_functiondef" in content
