from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20261019000000_analysis_backend.sql"


def test_analysis_migration_has_unique_forward_prefix():
    files = sorted(MIGRATION.parent.glob("*.sql"))
    prefixes = [path.name.split("_", 1)[0] for path in files]
    assert len(prefixes) == len(set(prefixes))
    assert MIGRATION.name > "20260831000000_portfolio_activity_rpc.sql"


def test_analysis_schema_is_private_and_each_table_has_rls():
    sql = MIGRATION.read_text().lower()
    assert "revoke all on schema analysis_backend from public, anon, authenticated" in sql
    assert "revoke all on all tables in schema analysis_backend from public, anon, authenticated" in sql
    for table in ("connections", "jobs", "memory", "rate_limits"):
        assert f"alter table analysis_backend.{table} enable row level security" in sql
    assert "create role lucrari_analysis_backend nologin" in sql
    assert "analysis_backend_role_has_unsafe_attributes" in sql
    assert "revoke lucrari_analysis_backend from anon, authenticated" in sql


def test_analysis_does_not_change_investment_or_job_behavior():
    sql = MIGRATION.read_text().lower()
    for forbidden in ("public.portfolio", "reset_my_investments", "earnings_events", "refresh_runs", "provider_run_results"):
        assert forbidden not in sql
    assert "update public.instruments" not in sql
    assert "delete from public.instruments" not in sql
    assert "insert into public.instruments" not in sql
    assert "analysis-reports', 'analysis-reports', false" in sql
    assert (ROOT / "tests/sql/analysis_backend_test.sql").exists()


def test_personal_auth_is_forward_private_and_never_embeds_owner_or_tokens():
    path = MIGRATION.parent / "20261022000000_personal_analysis_auth.sql"
    sql = path.read_text().lower()
    assert path.name > MIGRATION.name
    assert "add column auth_type text not null default 'api_key'" in sql
    assert "analysis_jobs_auth_contract" in sql
    assert "connection_id is null" in sql and "connection_id is not null" in sql
    for forbidden in ("grant ", "create policy", "public.portfolio", "refresh_runs", "03f9261a", "access_token", "refresh_token"):
        assert forbidden not in sql
    assert (ROOT / "tests/sql/personal_analysis_auth_test.sql").exists()


def test_dynamic_model_catalog_is_private_and_contains_no_credentials():
    path = MIGRATION.parent / "20261024000000_dynamic_chatgpt_model_catalog.sql"
    sql = path.read_text().lower()
    assert path.name > "20261022000000_personal_analysis_auth.sql"
    assert "create table analysis_backend.provider_model_catalog" in sql
    assert "enable row level security" in sql
    assert "to lucrari_analysis_backend" in sql
    assert "revoke all" in sql
    assert "jsonb_typeof(models) = 'array'" in sql
    for forbidden in (
        "access_token",
        "refresh_token",
        "client_secret",
        "grant select on analysis_backend.provider_model_catalog to authenticated",
    ):
        assert forbidden not in sql
