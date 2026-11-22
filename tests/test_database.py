import logging
from unittest.mock import MagicMock

import psycopg
import pytest

import app.core.database as db_module
from app.core.config import Settings, settings
from app.core.database import (
    DatabaseConfigError,
    DatabaseConnectionError,
    classify_connection_error,
    get_db_connection,
    validate_pooler_dsn,
)


def test_settings_database_url_none_when_no_env(monkeypatch):
    """Verify Settings._env_file=None produces exactly None for SUPABASE_DATABASE_URL."""
    monkeypatch.delenv("SUPABASE_DATABASE_URL", raising=False)
    test_settings = Settings(_env_file=None)
    assert test_settings.SUPABASE_DATABASE_URL is None


def test_module_import_does_not_connect_to_database(monkeypatch):
    """Verify accessing app.core.database creates zero psycopg connections (lazy connection)."""
    mock_connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", mock_connect)

    assert db_module.EXPECTED_TABLES is not None
    assert db_module.check_database_health is not None
    mock_connect.assert_not_called()


def test_get_db_connection_raises_config_error_when_url_missing(monkeypatch):
    """Verify get_db_connection raises DatabaseConfigError if URL is None or empty."""
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", None)
    with pytest.raises(DatabaseConfigError) as exc_info:
        with get_db_connection():
            pass
    assert exc_info.value.error_code == "configuration_missing"
    assert "SUPABASE_DATABASE_URL is not configured" in str(exc_info.value)

    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "   ")
    with pytest.raises(DatabaseConfigError) as exc_info_empty:
        with get_db_connection():
            pass
    assert exc_info_empty.value.error_code == "configuration_missing"


def test_validate_pooler_dsn_valid_url():
    """Verify validate_pooler_dsn accepts valid port 6543 pooler URLs."""
    valid_url = "postgresql://postgres.myproject:mypassword@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
    validate_pooler_dsn(valid_url)  # Does not raise


@pytest.mark.parametrize(
    "invalid_url",
    [
        "postgresql://postgres.myproject:mypassword@aws-0-eu-west-1.pooler.supabase.com:5432/postgres",  # wrong port 5432
        "postgresql://postgres.myproject:mypassword@aws-0-eu-west-1.pooler.supabase.com/postgres",       # missing port
        "postgresql://:mypassword@aws-0-eu-west-1.pooler.supabase.com:6543/postgres",                    # missing user
        "postgresql://postgres.myproject@aws-0-eu-west-1.pooler.supabase.com:6543/postgres",            # missing password
        "postgresql://postgres.myproject:mypassword@:6543/postgres",                                     # missing host
        "postgresql://postgres.myproject:mypassword@aws-0-eu-west-1.pooler.supabase.com:6543/",         # missing database
        "http://postgres.myproject:mypassword@aws-0-eu-west-1.pooler.supabase.com:6543/postgres",       # wrong scheme
        "://invalid-uri",                                                                                # unparseable
    ],
)
def test_validate_pooler_dsn_rejects_invalid_urls(invalid_url):
    """Verify validate_pooler_dsn rejects non-6543 ports, missing fields, or malformed URIs."""
    with pytest.raises(DatabaseConfigError) as exc_info:
        validate_pooler_dsn(invalid_url)
    assert exc_info.value.error_code == "invalid_configuration"
    assert "SUPABASE_DATABASE_URL configuration is invalid" in str(exc_info.value)
    # Ensure error message does not leak raw url components
    assert "mypassword" not in str(exc_info.value)
    assert "myproject" not in str(exc_info.value)


def test_get_db_connection_rejects_invalid_dsn_without_connecting(monkeypatch):
    """Verify get_db_connection never calls psycopg.connect if DSN validation fails."""
    mock_connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", mock_connect)

    invalid_port_url = "postgresql://user:pass@host:5432/postgres"
    with pytest.raises(DatabaseConfigError) as exc_info:
        with get_db_connection(database_url=invalid_port_url):
            pass

    assert exc_info.value.error_code == "invalid_configuration"
    mock_connect.assert_not_called()


def test_get_db_connection_passes_required_pooler_kwargs(monkeypatch):
    """Verify psycopg.connect is called with prepare_threshold=None, sslmode=require, connect_timeout=10."""
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_connect = MagicMock(return_value=mock_conn)
    monkeypatch.setattr(psycopg, "connect", mock_connect)

    dummy_url = "postgresql://user:pass@host:6543/postgres"
    with get_db_connection(database_url=dummy_url) as conn:
        assert conn is mock_conn

    mock_connect.assert_called_once_with(
        dummy_url,
        prepare_threshold=None,
        sslmode="require",
        connect_timeout=10,
    )
    mock_conn.close.assert_called_once()


def test_get_db_connection_closes_connection_on_exception(monkeypatch):
    """Verify connection is closed even if exception is raised within context block."""
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_connect = MagicMock(return_value=mock_conn)
    monkeypatch.setattr(psycopg, "connect", mock_connect)

    with pytest.raises(ValueError, match="Inside context error"):
        with get_db_connection(database_url="postgresql://user:pass@host:6543/postgres"):
            raise ValueError("Inside context error")

    mock_conn.close.assert_called_once()


def test_get_db_connection_does_not_rewrap_query_exceptions_from_context_block(monkeypatch):
    """Verify query exceptions inside the context manager are NOT caught as DatabaseConnectionError."""
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_connect = MagicMock(return_value=mock_conn)
    monkeypatch.setattr(psycopg, "connect", mock_connect)

    with pytest.raises(psycopg.ProgrammingError, match="syntax error"):
        with get_db_connection(database_url="postgresql://user:pass@host:6543/postgres"):
            raise psycopg.ProgrammingError("syntax error")

    mock_conn.close.assert_called_once()


def test_get_db_connection_reraises_assertion_error(monkeypatch):
    """Verify safety fixture AssertionError is never swallowed during connection."""
    def fail_with_assertion(*args, **kwargs):
        raise AssertionError("Real PostgreSQL access is forbidden during tests.")

    monkeypatch.setattr(psycopg, "connect", fail_with_assertion)

    with pytest.raises(AssertionError, match="Real PostgreSQL access is forbidden"):
        with get_db_connection(database_url="postgresql://user:pass@host:6543/postgres"):
            pass


def test_classify_connection_error_authentication_failure():
    """Verify authentication errors map to authentication_failed without leaking credentials."""
    exc_with_msg = psycopg.OperationalError(
        'connection failed: FATAL: password authentication failed for user "postgres.myproject"'
    )
    code, msg = classify_connection_error(exc_with_msg)
    assert code == "authentication_failed"
    assert "postgres.myproject" not in msg
    assert "password authentication failed" not in msg
    assert "Verify the Supabase Transaction Pooler database password" in msg

    # SQLSTATE 28P01
    exc_sqlstate = psycopg.OperationalError("Some internal message")
    exc_sqlstate.sqlstate = "28P01"
    code_sql, _ = classify_connection_error(exc_sqlstate)
    assert code_sql == "authentication_failed"


def test_classify_connection_error_timeout():
    """Verify timeout errors map to connection_timeout."""
    exc = psycopg.OperationalError("connection to server at 1.2.3.4, port 6543 timed out")
    code, msg = classify_connection_error(exc)
    assert code == "connection_timeout"
    assert "1.2.3.4" not in msg
    assert "6543" not in msg


def test_classify_connection_error_host_unreachable():
    """Verify DNS/host errors map to host_unreachable."""
    exc = psycopg.OperationalError("could not translate host name db.example.com to address")
    code, msg = classify_connection_error(exc)
    assert code == "host_unreachable"
    assert "db.example.com" not in msg


def test_classify_connection_error_ssl():
    """Verify SSL errors map to ssl_error."""
    exc = psycopg.OperationalError("SSL connection error: certificate verify failed")
    code, msg = classify_connection_error(exc)
    assert code == "ssl_error"


def test_database_connection_error_and_logs_never_leak_sensitive_info(monkeypatch, caplog):
    """Verify neither DatabaseConnectionError nor log records contain raw connection details."""
    raw_error_message = (
        'connection to server at "pooler.example.invalid" (192.0.2.10), port 6543 failed: '
        'FATAL: password authentication failed for user "test-user" '
        'postgresql://test-user:example-only@pooler.example.invalid:6543/postgres'
    )

    def fail_connect(*args, **kwargs):
        raise psycopg.OperationalError(raw_error_message)

    monkeypatch.setattr(psycopg, "connect", fail_connect)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(DatabaseConnectionError) as exc_info:
            with get_db_connection(
                database_url="postgresql://test-user:example-only@pooler.example.invalid:6543/postgres"
            ):
                pass

    err_str = str(exc_info.value)
    # Check exception message
    assert "example-only" not in err_str
    assert "test-user" not in err_str
    assert "192.0.2.10" not in err_str
    assert "pooler.example.invalid" not in err_str
    assert "6543" not in err_str
    assert exc_info.value.error_code == "authentication_failed"

    # Check caplog
    log_text = caplog.text
    assert "example-only" not in log_text
    assert "test-user" not in log_text
    assert "192.0.2.10" not in log_text
    assert "pooler.example.invalid" not in log_text
    assert "6543" not in log_text


def test_central_safety_fixture_blocks_unmocked_connect():
    """Verify the autouse fixture blocks raw psycopg.connect calls."""
    with pytest.raises(AssertionError, match="Real PostgreSQL access is forbidden during tests"):
        psycopg.connect("postgresql://dummy:dummy@localhost:6543/dummy")
