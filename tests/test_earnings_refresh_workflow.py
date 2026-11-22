from pathlib import Path

from app.services.calendar_state_store import (
    BIST_ROLLING_CACHE_FILE,
    NASDAQ_ROLLING_CACHE_FILE,
)

PROJECT_ROOT = Path(__file__).parent.parent
DAILY_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "earnings-refresh-daily.yml"
BACKFILL_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "bist-earnings-backfill.yml"
PARTITION_DIR = PROJECT_ROOT / "data" / "generated" / "earnings" / "bist100"


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

    # Repository is read-only: generated state must never create commits.
    assert "git add ." not in content
    assert "git add -A" not in content
    assert "git commit" not in content
    assert "git push" not in content
    assert "git pull" not in content
    assert "contents: write" not in content

    # BIST fiscal period files remain available as a workflow artifact, not a commit.
    assert "uses: actions/upload-artifact@v4" in content
    assert "data/generated/earnings/bist100/bist100_*_earnings_calendar.json" in content
    assert "data/generated/bist100_earnings_calendar.json" not in content
    assert "data/generated/nasdaq100_earnings_calendar.json" not in content

    # Prohibited files in daily commit
    assert "bist100_earnings_week.json" not in content
    assert "nasdaq100_earnings_week.json" not in content

    assert "Commit and Push" not in content


def test_backfill_workflow_file_structure_and_contracts():
    assert BACKFILL_WORKFLOW_PATH.exists(), f"Missing {BACKFILL_WORKFLOW_PATH}"

    content = BACKFILL_WORKFLOW_PATH.read_text(encoding="utf-8")

    # Triggers: workflow_dispatch ONLY, NO schedule/cron
    assert "workflow_dispatch:" in content
    assert "schedule:" not in content
    assert "cron:" not in content

    # Input year parameter
    assert "year:" in content
    assert "default: '2026'" in content or 'default: "2026"' in content

    # Concurrency and permissions
    assert "concurrency:" in content
    assert "group: calendar-refresh-jobs" in content
    assert "cancel-in-progress: false" in content

    # Python & Job execution
    assert "uses: actions/checkout@v4" in content
    assert "python -m app.jobs.bist_earnings_backfill_job --year" in content

    # Prohibited env/secret mappings
    assert "TIMEZONE:" not in content

    # Archive is preserved as a downloadable artifact without repository writes.
    assert "data/generated/earnings/bist100/bist100_*_earnings_calendar.json" in content
    assert "uses: actions/upload-artifact@v4" in content
    assert "contents: read" in content
    assert "contents: write" not in content
    assert "git add ." not in content
    assert "git add -A" not in content
    assert "git commit" not in content
    assert "git push" not in content
    assert "git pull" not in content

    # Backfill must NOT stage rolling or weekly cache files
    assert "bist100_earnings_calendar.json" not in content
    assert "nasdaq100_earnings_calendar.json" not in content
    assert "bist100_earnings_week.json" not in content
    assert "nasdaq100_earnings_week.json" not in content


def test_workflow_tests_do_not_modify_production_cache():
    def snapshot_file(path_str: str):
        p = Path(path_str)
        return p.read_bytes() if p.exists() else None

    def snapshot_dir(dir_path: Path):
        if not dir_path.exists():
            return {}
        return {f.name: f.read_bytes() for f in dir_path.glob("*.json")}

    bist_before = snapshot_file(BIST_ROLLING_CACHE_FILE)
    nasdaq_before = snapshot_file(NASDAQ_ROLLING_CACHE_FILE)
    part_before = snapshot_dir(PARTITION_DIR)

    # Read workflow files without running live jobs
    assert DAILY_WORKFLOW_PATH.exists()
    assert BACKFILL_WORKFLOW_PATH.exists()

    bist_after = snapshot_file(BIST_ROLLING_CACHE_FILE)
    nasdaq_after = snapshot_file(NASDAQ_ROLLING_CACHE_FILE)
    part_after = snapshot_dir(PARTITION_DIR)

    assert bist_before == bist_after
    assert nasdaq_before == nasdaq_after
    assert part_before == part_after
