from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_quality_gates_and_database_contract():
    assert WORKFLOW_PATH.exists(), f"Missing {WORKFLOW_PATH}"

    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = yaml.safe_load(content)
    triggers = workflow.get("on") or workflow.get(True)
    jobs = workflow["jobs"]

    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}

    assert {"lint", "tests", "database-integration"} <= set(jobs)

    database_job = jobs["database-integration"]
    assert database_job["name"] == "Database integration"
    assert database_job["needs"] == ["lint", "tests"]
    assert database_job["runs-on"] == "ubuntu-latest"
    assert database_job["timeout-minutes"] == 30
    assert database_job["env"]["SUPABASE_TELEMETRY_DISABLED"] == "1"

    steps = database_job["steps"]
    setup_supabase = next(
        step for step in steps if step.get("name") == "Set up Supabase CLI"
    )
    run_database_tests = next(
        step for step in steps if step.get("name") == "Run database integration suite"
    )

    assert setup_supabase["uses"] == "supabase/setup-cli@v3"
    assert setup_supabase["with"]["version"] == "2.109.1"
    assert run_database_tests["run"] == "bash ./scripts/test_db_integration.sh"

    assert "secrets." not in content
    assert "SUPABASE_DATABASE_URL" not in content
    assert "contents: write" not in content
