import re
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.job_tracking import (
    JobRunContext,
    JobRunOutcome,
    JobRunSpecification,
    JobStatus,
    NotificationStatus,
    OperationType,
    ProviderRunOutcome,
    ProviderScope,
    ProviderTerminalStatus,
)
from app.repositories.job_tracking_repository import (
    JobTrackingRepository,
    sanitize_recursive,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "supabase" / "migrations"


def test_schema_contract_refresh_runs_columns():
    """Verify refresh_runs database schema columns against repository queries."""
    canonical_refresh_runs_columns = {
        "id",
        "job_name",
        "operation_type",
        "provider_id",
        "scope",
        "trigger_type",
        "target_year",
        "started_at",
        "finished_at",
        "coverage_start",
        "coverage_end",
        "status",
        "sync_action",
        "skip_reason",
        "fresh_event_count",
        "final_event_count",
        "warnings",
        "metrics",
        "error",
        "github_run_id",
        "github_run_attempt",
        "notification_status",
        "notification_error",
        "notified_at",
        "created_at",
        "updated_at",
    }

    forbidden_columns = {
        "duration_seconds",
        "notification_sent_at",
        "cache_action",  # renamed to sync_action
    }

    # Verify migration files do not introduce forbidden columns in CREATE/ALTER TABLE public.refresh_runs
    for sql_file in MIGRATIONS_DIR.glob("*.sql"):
        content = sql_file.read_text(encoding="utf-8")
        for col in forbidden_columns:
            pattern = rf"\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?{col}\b"
            assert not re.search(pattern, content, re.IGNORECASE), (
                f"Forbidden column '{col}' found in migration {sql_file.name}"
            )

    # Verify start_run INSERT columns are a strict subset of canonical columns
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = (uuid4(),)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    spec = JobRunSpecification(
        job_name="test_job",
        operation_type="prices_sync",
        scope="BIST100",
        provider_code=None,
    )
    ctx = JobRunContext(
        started_at=datetime.now(timezone.utc),
        trigger_type="scheduled",
    )

    JobTrackingRepository.start_run(mock_conn, spec, ctx)
    start_sql = mock_cur.execute.call_args[0][0]

    # Extract columns from INSERT INTO public.refresh_runs (...)
    insert_match = re.search(r"INSERT\s+INTO\s+public\.refresh_runs\s*\((.*?)\)", start_sql, re.DOTALL | re.IGNORECASE)
    assert insert_match is not None, "Could not match INSERT columns in start_run SQL"
    start_cols = {c.strip() for c in insert_match.group(1).split(",")}
    assert start_cols.issubset(canonical_refresh_runs_columns), (
        f"start_run contains invalid columns: {start_cols - canonical_refresh_runs_columns}"
    )

    for col in forbidden_columns:
        assert col not in start_cols, f"Forbidden column '{col}' found in start_run SQL"

    # Verify finalize_run UPDATE columns are a strict subset of canonical columns
    mock_cur.reset_mock()
    mock_cur.rowcount = 1
    outcome = JobRunOutcome(
        status="success",
        sync_action="updated",
        skip_reason=None,
        finished_at=datetime.now(timezone.utc),
        coverage_start="2026-08-01",
        coverage_end="2026-08-28",
        fresh_event_count=10,
        final_event_count=10,
        warnings=["Non-critical warning"],
        metrics={"count": 10},
    )

    JobTrackingRepository.finalize_run(
        mock_conn,
        run_id=uuid4(),
        outcome=outcome,
        notification_status="sent",
        notification_error=None,
        notified_at=datetime.now(timezone.utc),
    )
    finalize_sql = mock_cur.execute.call_args[0][0]

    update_set_match = re.search(r"UPDATE\s+public\.refresh_runs\s+SET\s+(.*?)\s+WHERE", finalize_sql, re.DOTALL | re.IGNORECASE)
    assert update_set_match is not None, "Could not match UPDATE SET columns in finalize_run SQL"
    finalize_cols = {item.split("=")[0].strip() for item in update_set_match.group(1).split(",")}
    assert finalize_cols.issubset(canonical_refresh_runs_columns), (
        f"finalize_run contains invalid columns: {finalize_cols - canonical_refresh_runs_columns}"
    )

    for col in forbidden_columns:
        assert col not in finalize_cols, f"Forbidden column '{col}' found in finalize_run SQL"

    assert "notified_at" in finalize_cols
    assert "skip_reason" in finalize_cols
    assert "WHERE id = %s AND status = 'running'" in finalize_sql

    # Verify update_notification_status UPDATE columns
    mock_cur.reset_mock()
    mock_cur.rowcount = 1
    JobTrackingRepository.update_notification_status(
        mock_conn,
        run_id=uuid4(),
        status="sent",
        error=None,
        notified_at=datetime.now(timezone.utc),
    )
    notif_sql = mock_cur.execute.call_args[0][0]
    notif_set_match = re.search(r"UPDATE\s+public\.refresh_runs\s+SET\s+(.*?)\s+WHERE", notif_sql, re.DOTALL | re.IGNORECASE)
    assert notif_set_match is not None, "Could not match UPDATE SET columns in update_notification_status SQL"
    notif_cols = {item.split("=")[0].strip() for item in notif_set_match.group(1).split(",")}
    assert notif_cols.issubset(canonical_refresh_runs_columns), (
        f"update_notification_status contains invalid columns: {notif_cols - canonical_refresh_runs_columns}"
    )
    assert "WHERE id = %s AND status != 'running'" in notif_sql


def test_schema_contract_provider_run_results_columns():
    """Verify provider_run_results schema columns and upsert query."""
    canonical_provider_columns = {
        "id",
        "refresh_run_id",
        "provider_id",
        "scope",
        "status",
        "structure_validated",
        "observed_count",
        "candidate_count",
        "fresh_event_count",
        "carried_forward_count",
        "malformed_count",
        "retry_count",
        "warnings",
        "metrics",
        "error",
        "created_at",
    }

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [
        ("KAP", uuid4()),
        ("YAHOO_FINANCE", uuid4()),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    results = [
        ProviderRunOutcome(
            provider_code="KAP",
            scope="BIST100",
            status="success",
            fresh_event_count=5,
            warnings=["Warn"],
            metrics={"item_count": 5},
        )
    ]

    JobTrackingRepository.upsert_provider_results(mock_conn, uuid4(), results)
    upsert_sql = mock_cur.execute.call_args[0][0]

    insert_match = re.search(r"INSERT\s+INTO\s+public\.provider_run_results\s*\((.*?)\)", upsert_sql, re.DOTALL | re.IGNORECASE)
    assert insert_match is not None, "Could not match INSERT columns in upsert_provider_results SQL"
    provider_cols = {c.strip() for c in insert_match.group(1).split(",")}
    assert provider_cols.issubset(canonical_provider_columns), (
        f"upsert_provider_results contains invalid columns: {provider_cols - canonical_provider_columns}"
    )

    assert "ON CONFLICT (refresh_run_id, provider_id, scope)" in upsert_sql
    assert "DO UPDATE SET" in upsert_sql
    assert "metrics = EXCLUDED.metrics" in upsert_sql


def extract_constraint_values(sql_text: str, constraint_name: str) -> set[str]:
    """Parse exact array values from a named CHECK constraint in SQL migration."""
    pattern = rf"(?:CONSTRAINT|ADD\s+CONSTRAINT)\s+{constraint_name}\s+CHECK\s*\([^;]*?ARRAY\s*\[(.*?)\]"
    match = re.search(pattern, sql_text, re.DOTALL | re.IGNORECASE)
    assert match is not None, f"Constraint '{constraint_name}' not found in SQL"
    raw_items = match.group(1)
    values = set(re.findall(r"'([^']+)'::text", raw_items))
    assert len(values) > 0, f"No values extracted for constraint '{constraint_name}'"
    return values


def test_schema_literal_constraints_alignment():
    """Verify python Literal definitions align with exact SQL migration named check constraints."""
    py_job_statuses = set(get_args(JobStatus))
    py_provider_statuses = set(get_args(ProviderTerminalStatus))
    py_notif_statuses = set(get_args(NotificationStatus))
    py_operation_types = set(get_args(OperationType))
    py_scopes = set(get_args(ProviderScope))

    migration_path = MIGRATIONS_DIR / "20260826004000_central_job_tracking_engine.sql"
    migration_sql = migration_path.read_text(encoding="utf-8")

    latest_op_type_migration_path = MIGRATIONS_DIR / "20261120000000_push_notifications.sql"
    latest_op_type_migration_sql = latest_op_type_migration_path.read_text(encoding="utf-8")

    baseline_path = MIGRATIONS_DIR / "20260825000000_baseline_schema.sql"
    baseline_sql = baseline_path.read_text(encoding="utf-8")

    # Exact equality against named constraints in migration files
    assert py_job_statuses == extract_constraint_values(migration_sql, "refresh_runs_status_check")
    assert py_notif_statuses == extract_constraint_values(migration_sql, "refresh_runs_notification_status_check")
    assert py_operation_types == extract_constraint_values(latest_op_type_migration_sql, "refresh_runs_operation_type_check")
    assert py_provider_statuses == extract_constraint_values(baseline_sql, "provider_run_results_status_check")
    assert py_scopes == extract_constraint_values(migration_sql, "provider_run_results_scope_check")


def test_secret_sanitization_recursive():
    """Verify sanitize_recursive thoroughly masks tokens, passwords, and sensitive keys."""
    raw_data = {
        "api_key": "SecretApiKey123456",
        "nested": {
            "token": "ghp_" + "VerySecretGitHubToken999",
            "url": "https://user:MyPassword@supabase.co:6543/db",
            "list_items": [
                "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                "normal_item",
            ],
        },
        "safe_counter": 42,
    }

    cleaned = sanitize_recursive(raw_data)

    assert cleaned["api_key"] == "***MASKED***"
    assert cleaned["nested"]["token"] == "***MASKED***"
    assert "MyPassword" not in cleaned["nested"]["url"]
    assert "***MASKED***" in cleaned["nested"]["url"]
    assert "eyJhbGciOi" not in cleaned["nested"]["list_items"][0]
    assert "[REDACTED]" in cleaned["nested"]["list_items"][0]
    assert cleaned["nested"]["list_items"][1] == "normal_item"
    assert cleaned["safe_counter"] == 42
