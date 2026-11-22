from unittest.mock import MagicMock

import psycopg
import pytest

from app.core.config import settings
from app.core.database import (
    EXPECTED_TABLES,
    DatabaseHealthResult,
    check_database_health,
)
from app.jobs.database_health_check import run_health_check


def test_expected_tables_contains_all_7_tables():
    """Verify EXPECTED_TABLES contains exactly the 7 required tables."""
    assert len(EXPECTED_TABLES) == 7
    expected = {
        "instruments",
        "kap_member_mappings",
        "data_providers",
        "refresh_runs",
        "provider_run_results",
        "earnings_events",
        "economic_events",
    }
    assert set(EXPECTED_TABLES) == expected


def test_check_database_health_all_tables_found(monkeypatch):
    """Verify successful health check when all 7 tables are present."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = [(table_name,) for table_name in EXPECTED_TABLES] + [("extra_table",)]

    executed_queries = []

    def track_execute(query, *args, **kwargs):
        executed_queries.append(query)

    mock_cursor.execute.side_effect = track_execute

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=mock_conn))
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    result = check_database_health()

    assert result.status == "success"
    assert result.connected is True
    assert result.expected_table_count == 7
    assert result.found_table_count == 7
    assert result.missing_tables == ()
    assert result.error_code is None
    assert result.error is None
    assert set(result.found_tables) == set(EXPECTED_TABLES)

    # Verify only read-only SELECT queries were executed
    for q in executed_queries:
        upper_q = q.strip().upper()
        assert upper_q.startswith("SELECT")
        for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "UPSERT"):
            assert forbidden not in upper_q


def test_check_database_health_missing_tables(monkeypatch):
    """Verify failed health check when some required tables are missing."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (1,)
    partial_tables = [t for t in EXPECTED_TABLES if t != "economic_events"]
    mock_cursor.fetchall.return_value = [(t,) for t in partial_tables]

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=mock_conn))
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    result = check_database_health()

    assert result.status == "failed"
    assert result.connected is True
    assert result.expected_table_count == 7
    assert result.found_table_count == 6
    assert result.missing_tables == ("economic_events",)
    assert result.error_code == "missing_tables"
    assert "Missing required tables in public schema: economic_events" in result.error


def test_check_database_health_select_1_failure(monkeypatch):
    """Verify select 1 query failure results in connected=False and query_failed."""
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = psycopg.OperationalError("disk error")

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=mock_conn))
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    result = check_database_health()

    assert result.status == "failed"
    assert result.connected is False
    assert result.error_code == "query_failed"
    assert "schema query failed" in result.error


def test_check_database_health_schema_query_failure(monkeypatch):
    """Verify information_schema failure results in connected=True, status=failed, error_code=query_failed."""
    mock_cursor = MagicMock()
    # First call (SELECT 1) succeeds
    # Second call (information_schema) fails
    def cursor_execute(query, *args, **kwargs):
        if "information_schema" in query:
            raise psycopg.ProgrammingError("permission denied for information_schema")
        return None

    mock_cursor.execute.side_effect = cursor_execute
    mock_cursor.fetchone.return_value = (1,)

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=mock_conn))
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    result = check_database_health()

    assert result.status == "failed"
    assert result.connected is True
    assert result.error_code == "query_failed"
    assert "schema query failed" in result.error


def test_check_database_health_connection_authentication_error(monkeypatch):
    """Verify health check returns authentication_failed safely on auth failure."""
    def fail_connect(*args, **kwargs):
        raise psycopg.OperationalError('FATAL: password authentication failed for user "postgres.myproject"')

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:secret@host:6543/db")

    result = check_database_health()

    assert result.status == "failed"
    assert result.connected is False
    assert result.expected_table_count == 7
    assert result.found_table_count == 0
    assert result.missing_tables == EXPECTED_TABLES
    assert result.error_code == "authentication_failed"
    assert "Database authentication failed" in result.error
    assert "secret" not in result.error
    assert "myproject" not in result.error


def test_check_database_health_missing_config(monkeypatch):
    """Verify health check fails with configuration_missing when SUPABASE_DATABASE_URL is not set."""
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", None)

    result = check_database_health()

    assert result.status == "failed"
    assert result.connected is False
    assert result.expected_table_count == 7
    assert result.found_table_count == 0
    assert result.error_code == "configuration_missing"
    assert "SUPABASE_DATABASE_URL is not configured" in result.error


def test_check_database_health_invalid_config(monkeypatch):
    """Verify health check fails with invalid_configuration when URL has wrong port/format."""
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:5432/db")

    result = check_database_health()

    assert result.status == "failed"
    assert result.connected is False
    assert result.error_code == "invalid_configuration"
    assert "SUPABASE_DATABASE_URL configuration is invalid" in result.error


def test_check_database_health_reraises_safety_assertion_during_connect(monkeypatch):
    """Verify safety fixture AssertionError during connect is NOT swallowed by check_database_health."""
    def fail_connect(*args, **kwargs):
        raise AssertionError("Real PostgreSQL access is forbidden during tests. Mock connect.")

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    with pytest.raises(AssertionError, match="Real PostgreSQL access is forbidden"):
        check_database_health()


def test_check_database_health_reraises_safety_assertion_during_select_1(monkeypatch):
    """Verify safety fixture AssertionError during SELECT 1 is NOT swallowed by check_database_health."""
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = AssertionError("Safety check failed during SELECT 1.")

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=mock_conn))
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    with pytest.raises(AssertionError, match="Safety check failed during SELECT 1"):
        check_database_health()


def test_check_database_health_reraises_safety_assertion_during_schema_query(monkeypatch):
    """Verify safety fixture AssertionError during schema query is NOT swallowed by check_database_health."""
    mock_cursor = MagicMock()

    def cursor_execute(query, *args, **kwargs):
        if "information_schema" in query:
            raise AssertionError("Safety check failed during schema query.")
        return None

    mock_cursor.execute.side_effect = cursor_execute
    mock_cursor.fetchone.return_value = (1,)

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=mock_conn))
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    with pytest.raises(AssertionError, match="Safety check failed during schema query"):
        check_database_health()


def test_cli_job_success_output(monkeypatch, capsys):
    """Verify CLI job prints expected success output and returns 0."""
    success_result = DatabaseHealthResult(
        status="success",
        connected=True,
        expected_table_count=7,
        found_table_count=7,
        found_tables=EXPECTED_TABLES,
        missing_tables=(),
        error_code=None,
        error=None,
    )
    monkeypatch.setattr("app.jobs.database_health_check.check_database_health", lambda: success_result)

    exit_code = run_health_check()
    captured = capsys.readouterr().out

    assert exit_code == 0
    assert "Database connection: OK" in captured
    assert "Required tables: 7/7 found" in captured
    assert "Schema validation: PASSED" in captured


def test_cli_job_missing_tables_output(monkeypatch, capsys):
    """Verify CLI job prints missing tables and returns 1."""
    missing_result = DatabaseHealthResult(
        status="failed",
        connected=True,
        expected_table_count=7,
        found_table_count=6,
        found_tables=tuple(t for t in EXPECTED_TABLES if t != "economic_events"),
        missing_tables=("economic_events",),
        error_code="missing_tables",
        error="Missing required tables in public schema: economic_events",
    )
    monkeypatch.setattr("app.jobs.database_health_check.check_database_health", lambda: missing_result)

    exit_code = run_health_check()
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "Database connection: OK" in captured
    assert "Required tables: 6/7 found" in captured
    assert "Missing tables: economic_events" in captured
    assert "Error code: missing_tables" in captured
    assert "Error: Missing required tables in public schema: economic_events" in captured
    assert "Schema validation: FAILED" in captured


def test_cli_job_authentication_failure_output(monkeypatch, capsys):
    """Verify CLI job prints authentication_failed and returns 1."""
    failed_result = DatabaseHealthResult(
        status="failed",
        connected=False,
        expected_table_count=7,
        found_table_count=0,
        found_tables=(),
        missing_tables=EXPECTED_TABLES,
        error_code="authentication_failed",
        error="Database authentication failed. Verify the Supabase Transaction Pooler database password.",
    )
    monkeypatch.setattr("app.jobs.database_health_check.check_database_health", lambda: failed_result)

    exit_code = run_health_check()
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "Database connection: FAILED" in captured
    assert "Error code: authentication_failed" in captured
    assert "Error: Database authentication failed. Verify the Supabase Transaction Pooler database password." in captured
    assert "Schema validation: FAILED" in captured
