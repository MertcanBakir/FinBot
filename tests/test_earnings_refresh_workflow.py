from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DAILY_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "earnings-refresh-daily.yml"


def test_daily_workflow_file_structure_and_contracts():
    assert DAILY_WORKFLOW_PATH.exists(), f"Missing {DAILY_WORKFLOW_PATH}"

    content = DAILY_WORKFLOW_PATH.read_text(encoding="utf-8")

    # Name and triggers
    assert "name: Daily Earnings Calendar Refresh" in content
    assert '- cron: "0 5 * * *"' in content
    assert "workflow_dispatch:" in content

    # Permissions and Concurrency
    assert "contents: read" in content
    assert "concurrency:" in content
    assert "group: calendar-refresh-jobs" in content
    assert "cancel-in-progress: false" in content

    # Job settings
    assert "runs-on: ubuntu-latest" in content
    assert "timeout-minutes: 60" in content

    # Step actions
    assert "uses: actions/checkout@v4" in content
    assert "uses: actions/setup-python@v5" in content
    assert 'python-version: "3.11"' in content or "python-version: '3.11'" in content

    # Job execution and environment secrets
    assert "python -m app.jobs.earnings_refresh_job" in content
    assert "EMAIL_USER: ${{ secrets.EMAIL_USER }}" in content
    assert "EMAIL_APP_PASSWORD: ${{ secrets.EMAIL_APP_PASSWORD }}" in content
    assert "EMAIL_TO: ${{ secrets.EMAIL_TO }}" in content
    assert "SUPABASE_DATABASE_URL: ${{ secrets.SUPABASE_DATABASE_URL }}" in content

    # Prohibited env/secret mappings
    assert "TIMEZONE:" not in content
    assert "SMTP_HOST:" not in content
    assert "SMTP_PORT:" not in content

    # The workflow is read-only with respect to the repository.
    assert "git add ." not in content
    assert "git add -A" not in content
    assert "git commit" not in content
    assert "git push" not in content
    assert "git pull" not in content
    assert "contents: write" not in content

    assert "actions/upload-artifact" not in content

    assert "Commit and Push" not in content
