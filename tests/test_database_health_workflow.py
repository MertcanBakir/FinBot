from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "database-health-check.yml"


def test_database_health_workflow_file_structure_and_contracts():
    """Verify database-health-check.yml adheres to strict workflow contracts."""
    assert WORKFLOW_PATH.exists(), f"Missing {WORKFLOW_PATH}"

    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Name and triggers
    assert "name: Database Health Check" in content
    assert "workflow_dispatch:" in content
    assert "schedule:" not in content
    assert "cron:" not in content

    # Permissions
    assert "contents: read" in content
    assert "contents: write" not in content

    # Runner and setup
    assert "runs-on: ubuntu-latest" in content
    assert "timeout-minutes: 10" in content
    assert "uses: actions/checkout@v4" in content
    assert "uses: actions/setup-python@v5" in content
    assert "python-version: '3.11'" in content or 'python-version: "3.11"' in content
    assert "pip install -r requirements.txt" in content

    # Secret environment variable and command
    assert "SUPABASE_DATABASE_URL: ${{ secrets.SUPABASE_DATABASE_URL }}" in content
    assert "python -m app.jobs.database_health_check" in content

    # No git push, commit, or state mutations
    assert "git push" not in content
    assert "git commit" not in content
    assert "git add" not in content
