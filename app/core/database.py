import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator, Literal, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import psycopg
from psycopg import Connection

from app.core.config import settings

logger = logging.getLogger(__name__)

EXPECTED_TABLES: Tuple[str, ...] = (
    "instruments",
    "kap_member_mappings",
    "data_providers",
    "refresh_runs",
    "provider_run_results",
    "earnings_events",
    "economic_events",
)

DatabaseErrorCode = Literal[
    "configuration_missing",
    "invalid_configuration",
    "authentication_failed",
    "connection_timeout",
    "host_unreachable",
    "ssl_error",
    "connection_failed",
    "query_failed",
    "missing_tables",
]

SAFE_ERROR_MESSAGES: dict[DatabaseErrorCode, str] = {
    "configuration_missing": "SUPABASE_DATABASE_URL is not configured. Please set the environment variable.",
    "invalid_configuration": "SUPABASE_DATABASE_URL configuration is invalid.",
    "authentication_failed": "Database authentication failed. Verify the Supabase Transaction Pooler database password.",
    "connection_timeout": "Database connection timed out.",
    "host_unreachable": "Database host could not be reached.",
    "ssl_error": "Secure database connection could not be established.",
    "connection_failed": "Could not connect to the database.",
    "query_failed": "Database connection succeeded, but the schema query failed.",
    "missing_tables": "Missing required tables in public schema.",
}


class DatabaseConfigError(Exception):
    """Raised when database configuration is missing or invalid."""

    def __init__(
        self,
        error_code: DatabaseErrorCode = "configuration_missing",
        message: Optional[str] = None,
    ):
        self.error_code: DatabaseErrorCode = error_code
        self.message: str = message or SAFE_ERROR_MESSAGES.get(
            error_code, "Database configuration error."
        )
        super().__init__(self.message)


class DatabaseConnectionError(Exception):
    """Raised when establishing a database connection fails."""

    def __init__(
        self,
        error_code: DatabaseErrorCode = "connection_failed",
        message: Optional[str] = None,
    ):
        self.error_code: DatabaseErrorCode = error_code
        self.message: str = message or SAFE_ERROR_MESSAGES.get(
            error_code, "Database connection failed."
        )
        super().__init__(self.message)


@dataclass(frozen=True)
class DatabaseHealthResult:
    """Structured result of a database schema health check."""

    status: Literal["success", "failed"]
    connected: bool
    expected_table_count: int
    found_table_count: int
    found_tables: Tuple[str, ...]
    missing_tables: Tuple[str, ...]
    error_code: Optional[DatabaseErrorCode] = None
    error: Optional[str] = None


def validate_pooler_dsn(url: str) -> None:
    """Validate Supabase Transaction Pooler DSN contract.

    Contract:
    - Parseable URI with scheme postgres or postgresql.
    - Non-empty host.
    - Non-empty username.
    - Non-empty password.
    - Non-empty database path (e.g. /postgres).
    - Port must be strictly 6543 (Supabase Transaction Pooler port).

    Raises DatabaseConfigError with error_code='invalid_configuration' on any failure.
    Never exposes host, credentials, or DSN in error messages or logs.
    """
    try:
        parsed = urlsplit(url)
    except Exception:
        logger.error("Database DSN validation failed: unparseable URI")
        raise DatabaseConfigError(
            error_code="invalid_configuration",
            message=SAFE_ERROR_MESSAGES["invalid_configuration"],
        ) from None

    if parsed.scheme not in ("postgres", "postgresql"):
        logger.error("Database DSN validation failed: invalid scheme")
        raise DatabaseConfigError(
            error_code="invalid_configuration",
            message=SAFE_ERROR_MESSAGES["invalid_configuration"],
        )

    if not parsed.hostname or not parsed.hostname.strip():
        logger.error("Database DSN validation failed: missing hostname")
        raise DatabaseConfigError(
            error_code="invalid_configuration",
            message=SAFE_ERROR_MESSAGES["invalid_configuration"],
        )

    if not parsed.username or not parsed.username.strip():
        logger.error("Database DSN validation failed: missing username")
        raise DatabaseConfigError(
            error_code="invalid_configuration",
            message=SAFE_ERROR_MESSAGES["invalid_configuration"],
        )

    if not parsed.password or not parsed.password.strip():
        logger.error("Database DSN validation failed: missing password")
        raise DatabaseConfigError(
            error_code="invalid_configuration",
            message=SAFE_ERROR_MESSAGES["invalid_configuration"],
        )

    db_name = parsed.path.lstrip("/")
    if not db_name or not db_name.strip():
        logger.error("Database DSN validation failed: missing database name")
        raise DatabaseConfigError(
            error_code="invalid_configuration",
            message=SAFE_ERROR_MESSAGES["invalid_configuration"],
        )

    if parsed.port != 6543:
        logger.error(
            "Database DSN validation failed: invalid port (expected 6543 for pooler)"
        )
        raise DatabaseConfigError(
            error_code="invalid_configuration",
            message=SAFE_ERROR_MESSAGES["invalid_configuration"],
        )


def classify_connection_error(exc: Exception) -> Tuple[DatabaseErrorCode, str]:
    """Classify a connection exception into a safe DatabaseErrorCode and safe message.

    NEVER leaks raw error text, host, port, user, DSN, or password.
    """
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate in ("28P01", "28000"):
        return "authentication_failed", SAFE_ERROR_MESSAGES["authentication_failed"]

    raw = str(exc).lower()
    if (
        "password authentication failed" in raw
        or "authentication failed" in raw
        or ("auth" in raw and "failed" in raw)
    ):
        return "authentication_failed", SAFE_ERROR_MESSAGES["authentication_failed"]
    if "timeout" in raw or "timed out" in raw or "connection timed out" in raw:
        return "connection_timeout", SAFE_ERROR_MESSAGES["connection_timeout"]
    if (
        "could not translate host name" in raw
        or "name or service not known" in raw
        or "nodename nor servname provided" in raw
        or "host unreachable" in raw
        or "network is unreachable" in raw
        or "connection refused" in raw
    ):
        return "host_unreachable", SAFE_ERROR_MESSAGES["host_unreachable"]
    if "ssl" in raw or "certificate" in raw or "tls" in raw:
        return "ssl_error", SAFE_ERROR_MESSAGES["ssl_error"]

    return "connection_failed", SAFE_ERROR_MESSAGES["connection_failed"]


@contextmanager
def get_db_connection(
    database_url: Optional[str] = None,
) -> Generator[Connection, None, None]:
    """Context manager for lazy PostgreSQL connection via Psycopg 3.

    Configured specifically for Supabase Transaction Pooler compatibility:
    - Validates port 6543 and required connection URL components
    - prepare_threshold=None (disables prepared statements for pooler mode)
    - sslmode='require'
    - connect_timeout=10
    """
    url = database_url or settings.SUPABASE_DATABASE_URL
    if not url or not url.strip():
        logger.error(
            "Database connection failed: DatabaseConfigError - configuration_missing"
        )
        raise DatabaseConfigError(
            error_code="configuration_missing",
            message=SAFE_ERROR_MESSAGES["configuration_missing"],
        )

    # Validate Transaction Pooler DSN contract
    validate_pooler_dsn(url)

    conn: Optional[Connection] = None
    try:
        conn = psycopg.connect(
            url,
            prepare_threshold=None,
            sslmode="require",
            connect_timeout=10,
        )
    except AssertionError:
        raise
    except psycopg.Error as exc:
        err_code, safe_msg = classify_connection_error(exc)
        logger.error(
            "Database connection failed: %s - %s", type(exc).__name__, err_code
        )
        raise DatabaseConnectionError(
            error_code=err_code, message=safe_msg
        ) from None

    try:
        yield conn
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def check_database_health(
    database_url: Optional[str] = None,
    expected_tables: Sequence[str] = EXPECTED_TABLES,
) -> DatabaseHealthResult:
    """Perform a strictly read-only database schema health-check.

    Verifies:
    1. PostgreSQL connectivity (SELECT 1).
    2. Existence of all expected tables in the 'public' schema.
    """
    expected_tuple = tuple(expected_tables)
    total_expected = len(expected_tuple)

    try:
        with get_db_connection(database_url=database_url) as conn:
            # 1. Connectivity check (Read-only)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    cur.fetchone()
            except AssertionError:
                raise
            except Exception:
                logger.error("Database query failed: SELECT 1")
                return DatabaseHealthResult(
                    status="failed",
                    connected=False,
                    expected_table_count=total_expected,
                    found_table_count=0,
                    found_tables=(),
                    missing_tables=expected_tuple,
                    error_code="query_failed",
                    error=SAFE_ERROR_MESSAGES["query_failed"],
                )

            # 2. Schema check (Read-only)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_type = 'BASE TABLE';
                        """
                    )
                    rows = cur.fetchall()
                    public_tables = {row[0] for row in rows if row and row[0]}
            except AssertionError:
                raise
            except Exception:
                logger.error("Database query failed: information_schema query")
                return DatabaseHealthResult(
                    status="failed",
                    connected=True,
                    expected_table_count=total_expected,
                    found_table_count=0,
                    found_tables=(),
                    missing_tables=expected_tuple,
                    error_code="query_failed",
                    error=SAFE_ERROR_MESSAGES["query_failed"],
                )

            found = tuple(t for t in expected_tuple if t in public_tables)
            missing = tuple(t for t in expected_tuple if t not in public_tables)
            found_count = len(found)

            if missing:
                missing_str = ", ".join(missing)
                return DatabaseHealthResult(
                    status="failed",
                    connected=True,
                    expected_table_count=total_expected,
                    found_table_count=found_count,
                    found_tables=found,
                    missing_tables=missing,
                    error_code="missing_tables",
                    error=f"Missing required tables in public schema: {missing_str}",
                )

            return DatabaseHealthResult(
                status="success",
                connected=True,
                expected_table_count=total_expected,
                found_table_count=found_count,
                found_tables=found,
                missing_tables=(),
                error_code=None,
                error=None,
            )

    except DatabaseConfigError as exc:
        return DatabaseHealthResult(
            status="failed",
            connected=False,
            expected_table_count=total_expected,
            found_table_count=0,
            found_tables=(),
            missing_tables=expected_tuple,
            error_code=exc.error_code,
            error=exc.message,
        )
    except DatabaseConnectionError as exc:
        return DatabaseHealthResult(
            status="failed",
            connected=False,
            expected_table_count=total_expected,
            found_table_count=0,
            found_tables=(),
            missing_tables=expected_tuple,
            error_code=exc.error_code,
            error=exc.message,
        )
    except AssertionError:
        raise
    except Exception:
        return DatabaseHealthResult(
            status="failed",
            connected=False,
            expected_table_count=total_expected,
            found_table_count=0,
            found_tables=(),
            missing_tables=expected_tuple,
            error_code="connection_failed",
            error=SAFE_ERROR_MESSAGES["connection_failed"],
        )
