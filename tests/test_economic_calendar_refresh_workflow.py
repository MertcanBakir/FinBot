import os

import yaml


def test_daily_economic_calendar_workflow():
    wf_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".github",
        "workflows",
        "economic-calendar-refresh-daily.yml"
    )
    assert os.path.exists(wf_path)

    with open(wf_path, encoding="utf-8") as f:
        wf = yaml.safe_load(f)

    # Schedule validation
    on_section = wf.get("on") or wf.get(True)
    cron_val = on_section["schedule"][0]["cron"]
    assert cron_val == "30 4 * * *"

    # Concurrency group
    assert wf["concurrency"]["group"] == "calendar-refresh-jobs"
    assert wf["concurrency"]["cancel-in-progress"] is False

    # Steps inspection
    steps = wf["jobs"]["refresh-economic-calendar"]["steps"]
    refresh_step = next(s for s in steps if s.get("id") == "economic_refresh")
    
    assert "EMAIL_USER" in refresh_step["env"]
    assert "EMAIL_APP_PASSWORD" in refresh_step["env"]
    assert "EMAIL_TO" in refresh_step["env"]
    assert "SUPABASE_DATABASE_URL" in refresh_step["env"]
    assert "TIMEZONE" not in refresh_step["env"]

    # Supabase is durable storage; workflow must not write rolling JSON to Git.
    assert wf["permissions"]["contents"] == "read"
    workflow_text = open(wf_path, encoding="utf-8").read()
    assert "git add" not in workflow_text
    assert "git commit" not in workflow_text
    assert "git push" not in workflow_text
    assert "contents: write" not in workflow_text

def test_weekly_workflow_is_removed():
    wf_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".github",
        "workflows",
        "economic-calendar-weekly.yml"
    )
    assert not os.path.exists(wf_path)
