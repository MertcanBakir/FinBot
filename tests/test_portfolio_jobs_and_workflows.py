from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_fx_workflow_uses_isolated_requirements_and_expected_schedule():
    workflow = yaml.safe_load((ROOT / ".github/workflows/fx-latest-price.yml").read_text())
    trigger = workflow.get("on") or workflow.get(True)
    assert trigger["schedule"][0]["cron"] == "27 5,7,9,11,13,15,17,19,21 * * 1-5"
    content = (ROOT / ".github/workflows/fx-latest-price.yml").read_text()
    assert "requirements/jobs/fx.txt" in content
    assert "app.jobs.fx_latest_price_job --apply-db" in content
    assert "SUPABASE_DATABASE_URL" in content


def test_snapshot_workflow_runs_daily_at_0200_turkey_time():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/portfolio-snapshots-daily.yml").read_text()
    )
    trigger = workflow.get("on") or workflow.get(True)
    assert trigger["schedule"][0]["cron"] == "0 23 * * *"
    content = (ROOT / ".github/workflows/portfolio-snapshots-daily.yml").read_text()
    assert "requirements/jobs/portfolio-snapshots.txt" in content
    assert "app.jobs.portfolio_snapshots_job --apply-db" in content


def test_snapshot_migration_documents_modified_dietz_and_snapshot_fx_convention():
    migration = (
        ROOT / "supabase/migrations/20260825008000_portfolio_snapshot_engine.sql"
    ).read_text()
    assert "Daily Modified Dietz return" in migration
    assert "translated at snapshot FX" in migration
    assert "get_portfolio_position_summary_at" in migration
    assert "price_is_stale" in migration
