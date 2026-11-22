import smtplib
from unittest.mock import MagicMock
from uuid import UUID

import psycopg
import pytest

from app.core.config import settings

DEFAULT_MOCK_PROVIDERS = [
    ("YAHOO_FINANCE", UUID("00000000-0000-0000-0000-000000000001")),
    ("BINANCE", UUID("00000000-0000-0000-0000-000000000002")),
    ("TEFAS", UUID("00000000-0000-0000-0000-000000000003")),
    ("KAP", UUID("00000000-0000-0000-0000-000000000004")),
    ("OFFICIAL_IR", UUID("00000000-0000-0000-0000-000000000005")),
    ("TUIK", UUID("00000000-0000-0000-0000-000000000006")),
    ("TCMB", UUID("00000000-0000-0000-0000-000000000007")),
    ("BLS", UUID("00000000-0000-0000-0000-000000000008")),
    ("BEA", UUID("00000000-0000-0000-0000-000000000009")),
    ("FED", UUID("00000000-0000-0000-0000-000000000010")),
]


def configure_mock_db_cursor(cursor: MagicMock) -> MagicMock:
    """Helper to configure standard query responses for mock database cursors."""
    def _mock_execute(query, params=None):
        q = str(query).lower()
        if "data_providers" in q:
            cursor.fetchall.return_value = DEFAULT_MOCK_PROVIDERS
        elif "returning id" in q or "returning" in q:
            if not isinstance(cursor.fetchone.return_value, (tuple, list)):
                cursor.fetchone.return_value = (UUID("00000000-0000-0000-0000-000000000099"),)
        else:
            if cursor.fetchall.return_value == DEFAULT_MOCK_PROVIDERS:
                cursor.fetchall.return_value = []
        cursor.rowcount = 1

    cursor.execute.side_effect = _mock_execute
    cursor.rowcount = 1
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = (UUID("00000000-0000-0000-0000-000000000099"),)
    cursor.__enter__.return_value = cursor
    return cursor


def create_mock_db_connection() -> MagicMock:
    """Create a fully-configured mock database connection for tests."""
    conn = MagicMock()
    conn.closed = False
    cursor = configure_mock_db_cursor(MagicMock())
    conn.cursor.return_value = cursor
    conn.transaction.return_value.__enter__.return_value = conn
    conn.__enter__.return_value = conn
    return conn


@pytest.fixture(autouse=True)
def default_mock_db_cursor_behavior(monkeypatch):
    """Ensure any plain MagicMock used as a DB connection in tests provides realistic cursor tracking responses unless overridden."""
    orig_getattr = MagicMock.__getattr__

    def custom_getattr(self, name):
        if name == "cursor":
            cur = configure_mock_db_cursor(MagicMock())
            mock_method = MagicMock(return_value=cur)
            mock_method.__enter__.return_value = cur
            object.__setattr__(self, "cursor", mock_method)
            return mock_method
        return orig_getattr(self, name)

    monkeypatch.setattr(MagicMock, "__getattr__", custom_getattr)


@pytest.fixture(autouse=True)
def block_real_smtp(monkeypatch):
    """Central test suite SMTP safety gate.

    Blocks all real SMTP socket/network connections across the entire test suite.
    Any test attempting to send a real email via smtplib.SMTP will immediately fail.
    """
    def fail_on_real_smtp(*args, **kwargs):
        raise AssertionError(
            "Real SMTP access is forbidden during tests. "
            "Inject or mock EmailService explicitly."
        )

    monkeypatch.setattr(smtplib, "SMTP", fail_on_real_smtp)


@pytest.fixture(autouse=True)
def block_real_database(monkeypatch):
    """Central test suite PostgreSQL safety gate.

    Blocks all real PostgreSQL connections across the entire test suite.
    Any test attempting to connect via psycopg.connect without explicit mocking will fail.
    Default database URL is None unless explicitly set in a test.
    """
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", None)

    def fail_on_real_connect(*args, **kwargs):
        raise AssertionError(
            "Real PostgreSQL access is forbidden during tests. "
            "Mock psycopg.connect explicitly."
        )

    monkeypatch.setattr(psycopg, "connect", fail_on_real_connect)
