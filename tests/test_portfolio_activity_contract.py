import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260831000000_portfolio_activity_rpc.sql"
SQL_TEST = ROOT / "tests" / "sql" / "portfolio_activity_rpc_test.sql"


SIGNATURE = (
    "public.get_my_portfolio_activity("
    "uuid,integer,text,text,timestamp with time zone,"
    "timestamp with time zone,text,uuid)"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_portfolio_activity_migration_prefix_is_unique_and_after_watchlist():
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    prefixes = [name.split("_", 1)[0] for name in names]

    assert MIGRATION.name in names
    assert len(prefixes) == len(set(prefixes))
    # A historical migration must retain ordering, not permanently be the last
    # migration: unrelated forward migrations may safely be added later.
    assert names.index(MIGRATION.name) > names.index("20260830000000_user_watchlist_rpc.sql")


def test_portfolio_activity_contract_is_exact_and_financially_historical():
    sql = _sql().lower()

    expected_columns = (
        "activity_id uuid",
        "source_kind text",
        "activity_type text",
        "portfolio_id uuid",
        "instrument_id uuid",
        "symbol text",
        "company_name text",
        "market text",
        "quantity numeric",
        "unit_price numeric",
        "gross_amount numeric",
        "currency text",
        "occurred_at timestamp with time zone",
        "created_at timestamp with time zone",
        "notes text",
        "is_reversed boolean",
        "reversed_at timestamp with time zone",
        "reversal_reason text",
        "has_more boolean",
    )
    for column in expected_columns:
        assert column in sql

    assert "t.quantity * t.unit_price" in sql
    assert "c.amount::numeric as gross_amount" in sql
    assert "instrument_latest_prices" not in sql
    assert "earnings_events" not in sql
    assert "economic_events" not in sql
    assert "user_watchlist_items" not in sql
    assert "execute format" not in sql


def test_portfolio_activity_security_and_ownership_are_fail_closed():
    sql = _sql().lower()
    compact_sql = re.sub(r"\s+", "", sql)
    compact_signature = re.sub(r"\s+", "", SIGNATURE)

    assert "security definer" in sql
    assert "set search_path = ''" in sql
    assert "v_user_id uuid := auth.uid()" in sql
    assert "p.user_id = v_user_id" in sql
    assert "p.archived_at is null" in sql
    assert "portfolio_activity_not_found_or_access_denied" in sql
    assert f"revokeallonfunction{compact_signature}" in compact_sql
    assert ")frompublic,anon,authenticated;" in compact_sql
    assert f"grantexecuteonfunction{compact_signature}" in compact_sql
    assert ")toauthenticated;" in compact_sql


def test_portfolio_activity_uses_full_keyset_cursor_and_exact_indexes():
    sql = _sql().lower()

    assert "portfolio_transactions_activity_cursor_idx" in sql
    assert "portfolio_cash_flows_activity_cursor_idx" in sql
    assert re.search(
        r"portfolio_id,\s*transaction_at desc,\s*created_at desc,\s*id desc",
        sql,
    )
    assert re.search(
        r"portfolio_id,\s*flow_at desc,\s*created_at desc,\s*id desc",
        sql,
    )
    assert "portfolio_activity_incomplete_cursor" in sql
    assert "portfolio_activity_invalid_cursor_source" in sql
    assert "limit (p_limit + 1)" in sql
    assert "n.page_row_number <= p_limit" in sql
    assert sql.count("occurred_at desc") >= 3
    assert sql.count("created_at desc") >= 3
    assert sql.count("source_kind desc") >= 3
    assert sql.count("activity_id desc") >= 3


def test_portfolio_activity_search_filter_and_nullable_cash_contract():
    sql = _sql().lower()

    assert "('all', 'buy', 'sell', 'deposit', 'withdrawal')" in sql
    assert "portfolio_activity_invalid_filter" in sql
    assert "portfolio_activity_search_too_long" in sql
    assert "pg_catalog.char_length(v_search) > 100" in sql
    assert "pg_catalog.strpos" in sql
    assert "and v_search is null" in sql
    for value in (
        "null::uuid as instrument_id",
        "null::text as symbol",
        "null::text as company_name",
        "null::text as market",
        "null::numeric as quantity",
        "null::numeric as unit_price",
    ):
        assert value in sql


def test_portfolio_activity_migration_does_not_mutate_existing_contracts():
    sql = _sql().lower()

    assert "create or replace function public.reset_my_investments" not in sql
    assert "drop function public.reset_my_investments" not in sql
    assert "create or replace function public.get_my_watchlist" not in sql
    assert "create or replace function public.get_mobile_system_health" not in sql
    assert "delete from public.portfolio_" not in sql
    assert "update public.portfolio_" not in sql


def test_portfolio_activity_real_sql_acceptance_suite_is_present():
    content = SQL_TEST.read_text(encoding="utf-8").lower()

    required_markers = (
        "set local role anon;",
        "set local role authenticated;",
        "portfolio_activity_authentication_required",
        "portfolio_activity_not_found_or_access_denied",
        "portfolio_activity_incomplete_cursor",
        "portfolio_activity_invalid_cursor_source",
        "collected_activity_ids",
        "inactive instrument history",
        "user b cannot access a's portfolio",
        "has_table_privilege",
        "has_function_privilege",
        "pg_get_functiondef",
        "rollback;",
    )
    for marker in required_markers:
        assert marker in content
