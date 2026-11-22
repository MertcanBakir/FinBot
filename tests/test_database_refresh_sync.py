from datetime import date, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.config import settings
from app.jobs.earnings_refresh_job import exit_code_for_result as earnings_exit_code
from app.jobs.economic_calendar_refresh_job import (
    exit_code_for_result as economic_exit_code,
)
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsEvent,
)
from app.models.earnings_refresh import (
    EarningsEventDiff,
    EarningsRefreshJobResult,
    MarketRefreshOutcome,
    OfficialIrProviderSummary,
    OfficialIrRefreshSummary,
)
from app.models.economic_calendar import (
    RollingEconomicCalendar,
)
from app.models.economic_calendar_refresh import (
    EconomicCalendarRefreshJobResult,
    EconomicEventDiff,
    EconomicProviderRefreshOutcome,
)
from app.models.market import EconomicEvent
from app.repositories.refresh_repository import (
    RefreshRepository,
    RefreshRunData,
)
from app.services.database_refresh_sync import (
    sync_earnings_refresh_to_database,
    sync_economic_refresh_to_database,
)
from app.services.earnings_refresh_report import (
    build_earnings_refresh_email_view,
    collect_earnings_refresh_alerts,
    format_earnings_refresh_email_text,
    format_earnings_refresh_subject,
)
from app.services.economic_calendar_refresh_report import (
    _collect_unique_warnings,
    format_economic_calendar_refresh_email_html,
    format_economic_calendar_refresh_email_text,
    format_economic_calendar_refresh_subject,
)


@pytest.fixture
def mock_db_env(monkeypatch):
    """Setup mock database connection, provider map, and instrument map."""
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    mock_conn = MagicMock()
    mock_conn.closed = False
    cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = cursor
    mock_conn.transaction.return_value.__enter__.return_value = mock_conn

    monkeypatch.setattr("app.services.database_refresh_sync.get_db_connection", MagicMock(return_value=mock_conn))
    mock_conn.__enter__.return_value = mock_conn

    providers = {
        "KAP": uuid4(),
        "OFFICIAL_IR": uuid4(),
        "YAHOO_FINANCE": uuid4(),
        "TUIK": uuid4(),
        "TCMB": uuid4(),
        "BLS": uuid4(),
        "BEA": uuid4(),
        "FED": uuid4(),
    }
    instruments = {
        ("BIST100", "AEFES"): uuid4(),
        ("NASDAQ100", "AAPL"): uuid4(),
    }

    mock_conn.providers = providers
    mock_conn.instruments = instruments

    monkeypatch.setattr("app.repositories.refresh_repository.RefreshRepository.get_provider_id_map", lambda conn: providers)
    monkeypatch.setattr("app.repositories.refresh_repository.RefreshRepository.get_instrument_id_map", lambda conn: instruments)
    monkeypatch.setattr("app.repositories.refresh_repository.RefreshRepository.insert_refresh_run", lambda conn, data: uuid4())
    monkeypatch.setattr("app.repositories.refresh_repository.RefreshRepository.insert_provider_run_results", lambda conn, rid, results: len(results))
    monkeypatch.setattr("app.repositories.refresh_repository.RefreshRepository.upsert_earnings_events", lambda conn, rid, evs, imap, pmap: len(evs))
    monkeypatch.setattr("app.repositories.refresh_repository.RefreshRepository.deactivate_missing_earnings_events", lambda conn, rid, m, s, e, keys: 0)
    monkeypatch.setattr("app.repositories.refresh_repository.RefreshRepository.upsert_economic_events", lambda conn, rid, evs, pmap: len(evs))
    monkeypatch.setattr("app.repositories.refresh_repository.RefreshRepository.deactivate_missing_economic_events", lambda conn, rid, pid, s, e, keys: 0)

    return mock_conn


def test_sync_earnings_refresh_success(mock_db_env):
    """Verify earnings sync completes with status success when URL is configured."""
    result = EarningsRefreshJobResult(
        started_at="2026-08-18T05:00:00+00:00",
        finished_at="2026-08-18T05:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-18",
            coverage_end="2026-11-17",
            diff=EarningsEventDiff(fetched_event_count=1, final_event_count=1),
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-18",
            coverage_end="2026-11-17",
            diff=EarningsEventDiff(fetched_event_count=1, final_event_count=1),
        ),
    )

    final_bist = Bist100EarningsCalendar(
        schema_version="2.0",
        generated_at="2026-08-18T05:00:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        market="BIST100",
        status="success",
        provider_status="success",
        events=[
            BistEarningsEvent(
                symbol="AEFES",
                company_name="Anadolu Efes",
                earnings_date="2026-08-20",
                period_start="2026-01-01",
                period_end="2026-03-31",
                source="KAP",
            )
        ],
    )

    final_nasdaq = Nasdaq100EarningsCalendar(
        schema_version="2.0",
        generated_at="2026-08-18T05:00:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        market="NASDAQ100",
        status="success",
        provider_status="success",
        events=[
            NasdaqEarningsEvent(
                symbol="AAPL",
                company_name="Apple Inc.",
                earnings_date="2026-10-29",
                session="after_hours",
                session_label="After Hours",
            )
        ],
    )

    status, error = sync_earnings_refresh_to_database(
        result, final_bist, final_nasdaq, refresh_run_id=uuid4()
    )

    assert status == "success"
    assert error is None


def test_sync_earnings_refresh_skipped_when_not_configured(monkeypatch):
    """Verify earnings sync is skipped without DB connection when URL is unset."""
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", None)

    mock_connect = MagicMock()
    monkeypatch.setattr("app.services.database_refresh_sync.get_db_connection", mock_connect)

    result = EarningsRefreshJobResult(
        started_at="2026-08-18T05:00:00+00:00",
        finished_at="2026-08-18T05:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100", outcome_status="success", fetch_status="success",
            sync_action="preserved_previous", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100", outcome_status="success", fetch_status="success",
            sync_action="preserved_previous", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
    )

    status, error = sync_earnings_refresh_to_database(result, None, None, refresh_run_id=uuid4())

    assert status == "skipped_not_configured"
    assert error is None
    mock_connect.assert_not_called()


def test_sync_earnings_refresh_no_event_upsert_on_preserved_cache(mock_db_env):
    """Verify earnings sync records refresh run but skips event upserts when cache action is preserved_previous."""
    result = EarningsRefreshJobResult(
        started_at="2026-08-18T05:00:00+00:00",
        finished_at="2026-08-18T05:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        overall_status="partial",
        bist=MarketRefreshOutcome(
            market="BIST100", outcome_status="partial", fetch_status="partial",
            sync_action="preserved_previous", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100", outcome_status="partial", fetch_status="partial",
            sync_action="preserved_previous", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
    )

    with patch("app.repositories.refresh_repository.RefreshRepository.upsert_earnings_events") as mock_upsert:
        status, error = sync_earnings_refresh_to_database(result, None, None, refresh_run_id=uuid4())
        assert status == "success"
        mock_upsert.assert_not_called()


def test_sync_economic_refresh_success(mock_db_env):
    """Verify economic sync completes with status success."""
    result = EconomicCalendarRefreshJobResult(
        generated_at="2026-08-18T04:30:00+03:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-16",
        status="success",
        sync_action="replaced",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=1, final_event_count=1),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(
                provider_id="TUIK",
                provider_name="TÜİK",
                status="success",
                structure_validated=True,
                event_count=1,
            )
        ],
    )

    final_calendar = RollingEconomicCalendar(
        schema_version="2.0",
        generated_at="2026-08-18T04:30:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-16",
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[],
        events=[
            EconomicEvent(
                name="TÜFE",
                country="TR",
                scheduled_at="2026-09-03T10:00:00+03:00",
                scheduled_at_istanbul="2026-09-03T10:00:00+03:00",
                source="TUIK",
                provider_id="TUIK",
                event_code="TR_CPI",
                reference_period="2026-08",
            )
        ],
    )

    status, error = sync_economic_refresh_to_database(
        result, final_calendar, refresh_run_id=uuid4(), dry_run=False
    )

    assert status == "success"
    assert error is None


def test_sync_economic_refresh_skipped_on_dry_run(monkeypatch):
    """Verify economic sync is skipped without DB access in dry-run mode."""
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    mock_connect = MagicMock()
    monkeypatch.setattr("app.services.database_refresh_sync.get_db_connection", mock_connect)

    result = EconomicCalendarRefreshJobResult(
        generated_at="2026-08-18T04:30:00+03:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        status="success",
        sync_action="dry_run",
        state_source="",
        diff=EconomicEventDiff(),
    )

    status, error = sync_economic_refresh_to_database(result, None, dry_run=True)

    assert status == "skipped_dry_run"
    assert error is None
    mock_connect.assert_not_called()


def test_exit_code_fails_on_database_error():
    """Verify CLI exit code returns 1 when database_status is failed."""
    # Earnings
    earn_res = EarningsRefreshJobResult(
        started_at="2026-08-18T05:00:00+00:00",
        finished_at="2026-08-18T05:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        overall_status="success",  # Business status is success
        bist=MarketRefreshOutcome(
            market="BIST100", outcome_status="success", fetch_status="success",
            sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100", outcome_status="success", fetch_status="success",
            sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
        database_status="failed",  # Database status failed
        database_error="Connection timed out",
    )
    assert earnings_exit_code(earn_res) == 1

    # Economic
    econ_res = EconomicCalendarRefreshJobResult(
        generated_at="2026-08-18T04:30:00+03:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        status="success",  # Business status is success
        sync_action="replaced",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(),
        database_status="failed",  # Database status failed
        database_error="Query failed",
    )
    assert economic_exit_code(econ_res) == 1


def test_email_alerts_include_database_failure():
    """Verify operational email warnings include database sync failure."""
    earn_res = EarningsRefreshJobResult(
        started_at="2026-08-18T05:00:00+00:00",
        finished_at="2026-08-18T05:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100", outcome_status="success", fetch_status="success",
            sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100", outcome_status="success", fetch_status="success",
            sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
        database_status="failed",
        database_error="Connection timed out",
    )
    alerts = collect_earnings_refresh_alerts(earn_res)
    assert any("Supabase veritabanı senkronizasyonu başarısız" in a for a in alerts)

    econ_res = EconomicCalendarRefreshJobResult(
        generated_at="2026-08-18T04:30:00+03:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        status="success",
        sync_action="replaced",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(),
        database_status="failed",
        database_error="Connection timed out",
    )
    econ_warnings = _collect_unique_warnings(econ_res)
    assert any("Supabase veritabanı senkronizasyonu başarısız" in w for w in econ_warnings)


def test_scoped_inactive_rules_earnings(mock_db_env):
    """Verify earnings scoped inactive is called only on fetch_status=success and sync_action=replaced."""
    with patch("app.repositories.refresh_repository.RefreshRepository.deactivate_missing_earnings_events") as mock_deact:
        # Case 1: success & replaced -> calls deactivation
        res1 = EarningsRefreshJobResult(
            started_at="2026-08-18T05:00:00+00:00", finished_at="2026-08-18T05:01:00+00:00",
            duration_seconds=60.0, coverage_start="2026-08-18", coverage_end="2026-11-17",
            overall_status="success",
            bist=MarketRefreshOutcome(market="BIST100", outcome_status="success", fetch_status="success", sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"),
            nasdaq=MarketRefreshOutcome(market="NASDAQ100", outcome_status="success", fetch_status="success", sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"),
        )
        sync_earnings_refresh_to_database(
            res1,
            Bist100EarningsCalendar(
                generated_at="2026-08-18T05:00:00",
                coverage_start="2026-08-18",
                coverage_end="2026-11-17",
                status="success",
                provider_status="success",
                events=[],
            ),
            None,
            refresh_run_id=uuid4(),
        )
        assert mock_deact.call_count == 1

        mock_deact.reset_mock()

        # Case 2: partial & merged_and_updated -> does NOT call deactivation
        res2 = EarningsRefreshJobResult(
            started_at="2026-08-18T05:00:00+00:00", finished_at="2026-08-18T05:01:00+00:00",
            duration_seconds=60.0, coverage_start="2026-08-18", coverage_end="2026-11-17",
            overall_status="partial",
            bist=MarketRefreshOutcome(market="BIST100", outcome_status="partial", fetch_status="partial", sync_action="merged_and_updated", coverage_start="2026-08-18", coverage_end="2026-11-17"),
            nasdaq=MarketRefreshOutcome(market="NASDAQ100", outcome_status="success", fetch_status="success", sync_action="preserved_previous", coverage_start="2026-08-18", coverage_end="2026-11-17"),
        )
        sync_earnings_refresh_to_database(
            res2,
            Bist100EarningsCalendar(
                generated_at="2026-08-18T05:00:00",
                coverage_start="2026-08-18",
                coverage_end="2026-11-17",
                status="partial",
                provider_status="partial",
                events=[],
            ),
            None,
            refresh_run_id=uuid4(),
        )
        mock_deact.assert_not_called()


def test_scoped_inactive_rules_economic(mock_db_env):
    """Verify economic scoped inactive is called only for providers with status=success and structure_validated=True."""
    with patch("app.repositories.refresh_repository.RefreshRepository.deactivate_missing_economic_events") as mock_deact:
        res = EconomicCalendarRefreshJobResult(
            generated_at="2026-08-18T04:30:00+03:00", coverage_start="2026-08-18", coverage_end="2026-11-16",
            status="partial", sync_action="merged_and_updated", state_source="data/generated/economic_calendar.json",
            diff=EconomicEventDiff(),
            provider_outcomes=[
                # Provider 1: success & validated -> SHOULD be deactivated
                EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True),
                # Provider 2: partial -> should NOT be deactivated
                EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="partial", structure_validated=False),
            ],
        )
        final_cal = RollingEconomicCalendar(
            generated_at="2026-08-18T04:30:00", coverage_start="2026-08-18", coverage_end="2026-11-16",
            timezone="Europe/Istanbul", status="partial", provider_results=[], events=[]
        )
        sync_economic_refresh_to_database(res, final_cal, refresh_run_id=uuid4(), dry_run=False)

        # Only 1 provider (TUIK) gets deactivated
        assert mock_deact.call_count == 1


def test_repository_parameterized_queries_and_no_destructive_sql(mock_db_env):
    """Verify repository queries use parameter placeholders (%s) and never use DELETE or TRUNCATE."""
    conn = mock_db_env
    cursor = conn.cursor.return_value.__enter__.return_value

    executed_sql = []
    def track_exec(query, *args, **kwargs):
        executed_sql.append(query)
    cursor.execute.side_effect = track_exec
    cursor.executemany.side_effect = track_exec
    cursor.fetchone.return_value = (str(uuid4()),)

    run_id = RefreshRepository.insert_refresh_run(
        conn,
        RefreshRunData(
            job_name="test_job", started_at=datetime.now(), status="success",
            fresh_event_count=0, final_event_count=0
        )
    )
    assert run_id is not None

    for sql in executed_sql:
        upper = sql.upper()
        assert "DELETE" not in upper
        assert "TRUNCATE" not in upper
        assert "DROP" not in upper
        assert "%s" in sql  # Parameterized


def test_earnings_and_economic_upsert_observation_tracking_sql():
    """Verify upsert SQL contains all business fields in last_changed_at CASE and observation-only distinct check in WHERE."""
    from app.repositories.refresh_repository import (
        SyncEarningsEventData,
        SyncEconomicEventData,
    )

    mock_conn = MagicMock()
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    executed = []
    cursor.executemany.side_effect = lambda q, p: executed.append(q)

    # Earnings upsert query check
    RefreshRepository.upsert_earnings_events(
        mock_conn,
        uuid4(),
        [
            SyncEarningsEventData(
                market="BIST100", symbol="AEFES", identity_key="KAP:AEFES:2026-01-01:2026-03-31",
                earnings_date=date(2026, 8, 20), provider_code="KAP"
            )
        ],
        {("BIST100", "AEFES"): uuid4()},
        {"KAP": uuid4()},
    )
    earnings_sql = executed[-1]
    assert "OR earnings_events.last_refresh_run_id IS DISTINCT FROM EXCLUDED.last_refresh_run_id;" in earnings_sql
    assert "last_seen_at = now()" in earnings_sql
    # All 18 business/state fields must be in CASE condition
    for field in [
        "instrument_id", "fiscal_year", "fiscal_period", "period_start", "period_end",
        "earnings_date", "date_type", "session", "provider_id", "source_provider",
        "source_event_id", "source_url", "source_type", "publish_date", "disclosure_index",
        "is_active", "is_stale", "carried_forward"
    ]:
        assert f"earnings_events.{field} IS DISTINCT FROM EXCLUDED.{field}" in earnings_sql
    # Observation-only last_refresh_run_id must NOT trigger last_changed_at update
    case_clause = earnings_sql[earnings_sql.find("last_changed_at = CASE"):earnings_sql.find("END,")]
    assert "last_refresh_run_id" not in case_clause

    # Economic upsert query check
    RefreshRepository.upsert_economic_events(
        mock_conn,
        uuid4(),
        [
            SyncEconomicEventData(
                identity_key="TUIK:TR_CPI:2026-09-03", provider_code="TUIK",
                name="TÜFE", country="TR", scheduled_at=datetime.now()
            )
        ],
        {"TUIK": uuid4()},
    )
    econ_sql = executed[-1]
    assert "OR economic_events.last_refresh_run_id IS DISTINCT FROM EXCLUDED.last_refresh_run_id;" in econ_sql
    assert "last_seen_at = now()" in econ_sql
    # All 11 business/state fields must be in CASE condition
    for field in [
        "provider_id", "event_code", "name", "country", "reference_period",
        "scheduled_at", "source_event_id", "source_url", "is_active", "is_stale", "carried_forward"
    ]:
        assert f"economic_events.{field} IS DISTINCT FROM EXCLUDED.{field}" in econ_sql
    # Observation-only last_refresh_run_id must NOT trigger last_changed_at update
    case_clause_econ = econ_sql[econ_sql.find("last_changed_at = CASE"):econ_sql.find("END,")]
    assert "last_refresh_run_id" not in case_clause_econ


def test_assertion_error_is_reraised_during_db_sync(monkeypatch):
    """Verify test security gate AssertionError is never swallowed by sync handlers."""
    monkeypatch.setattr(settings, "SUPABASE_DATABASE_URL", "postgresql://user:pass@host:6543/db")

    def raise_assertion(*args, **kwargs):
        raise AssertionError("Real PostgreSQL access is forbidden during tests.")

    monkeypatch.setattr("app.services.database_refresh_sync.get_db_connection", raise_assertion)

    earn_res = EarningsRefreshJobResult(
        started_at="2026-08-18T05:00:00+00:00", finished_at="2026-08-18T05:01:00+00:00",
        duration_seconds=60.0, coverage_start="2026-08-18", coverage_end="2026-11-17",
        overall_status="success",
        bist=MarketRefreshOutcome(market="BIST100", outcome_status="success", fetch_status="success", sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"),
        nasdaq=MarketRefreshOutcome(market="NASDAQ100", outcome_status="success", fetch_status="success", sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"),
    )

    with pytest.raises(AssertionError, match="Real PostgreSQL access is forbidden"):
        sync_earnings_refresh_to_database(earn_res, None, None, refresh_run_id=uuid4())

    econ_res = EconomicCalendarRefreshJobResult(
        generated_at="2026-08-18T04:30:00+03:00", coverage_start="2026-08-18", coverage_end="2026-11-16",
        status="success", sync_action="replaced", state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(),
    )

    with pytest.raises(AssertionError, match="Real PostgreSQL access is forbidden"):
        sync_economic_refresh_to_database(econ_res, None, refresh_run_id=uuid4(), dry_run=False)


def test_deactivation_audit_fields_sql_for_repository_calls():
    """Verify deactivation SQL sets last_refresh_run_id and last_changed_at without updating last_seen_at."""
    mock_conn = MagicMock()
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    executed = []
    cursor.execute.side_effect = lambda q, p: executed.append((q, p))

    run_id = uuid4()

    # Earnings deactivation
    RefreshRepository.deactivate_missing_earnings_events(
        mock_conn, run_id, "BIST100", date(2026, 8, 18), date(2026, 11, 17), {"active_key"}
    )
    earn_sql, earn_params = executed[-1]
    assert "is_active = false" in earn_sql
    assert "last_refresh_run_id = %s" in earn_sql
    assert "last_changed_at = now()" in earn_sql
    assert "last_seen_at" not in earn_sql
    assert earn_params[0] == run_id

    # Economic deactivation
    prov_id = uuid4()
    RefreshRepository.deactivate_missing_economic_events(
        mock_conn, run_id, prov_id, datetime.now(), datetime.now(), {"active_key"}
    )
    econ_sql, econ_params = executed[-1]
    assert "is_active = false" in econ_sql
    assert "last_refresh_run_id = %s" in econ_sql
    assert "last_changed_at = now()" in econ_sql
    assert "last_seen_at" not in econ_sql
    assert econ_params[0] == run_id


def test_earnings_email_effective_status_when_db_failed():
    """Verify earnings email uses failed effective status when overall_status is success but database_status is failed."""
    earn_res = EarningsRefreshJobResult(
        started_at="2026-08-18T05:00:00+00:00",
        finished_at="2026-08-18T05:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        overall_status="success",  # Business status is success
        bist=MarketRefreshOutcome(
            market="BIST100", outcome_status="success", fetch_status="success",
            sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100", outcome_status="success", fetch_status="success",
            sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"
        ),
        database_status="failed",  # Database status is failed
        database_error="Connection failed",
    )

    # Subject line must show failure icon and text
    subj = format_earnings_refresh_subject(earn_res)
    assert "❌" in subj
    assert "Güncelleme başarısız" in subj

    # View model must reflect failed status and red styling
    view = build_earnings_refresh_email_view(earn_res)
    assert view["status_conf"]["primary"] == "#B91C1C"

    # Plain text title must indicate GÜNCELLEME BAŞARISIZ
    text_rep = format_earnings_refresh_email_text(earn_res)
    assert "FİNBOT BİLANÇO TAKVİMİ — GÜNCELLEME BAŞARISIZ" in text_rep


def test_economic_email_effective_status_when_db_failed():
    """Verify economic email uses failed effective status when status is success but database_status is failed."""
    econ_res = EconomicCalendarRefreshJobResult(
        generated_at="2026-08-18T04:30:00+03:00",
        coverage_start="2026-08-18",
        coverage_end="2026-11-16",
        status="success",  # Business status is success
        sync_action="replaced",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(),
        database_status="failed",  # Database status is failed
        database_error="Connection failed",
    )

    # Subject line must show failure icon and text
    subj = format_economic_calendar_refresh_subject(econ_res)
    assert "❌" in subj
    assert "Güncelleme başarısız" in subj

    # Plain text representation must show Hata and suppress "Tüm ekonomik takvim kaynakları güncel"
    text_rep = format_economic_calendar_refresh_email_text(econ_res)
    assert "FinBot Günlük Ekonomik Takvim Raporu — Hata" in text_rep
    assert "Tüm ekonomik takvim kaynakları başarıyla kontrol edildi." not in text_rep

    # HTML representation must show Hata badge style
    html_rep = format_economic_calendar_refresh_email_html(econ_res)
    assert "#B91C1C" in html_rep
    assert "Hata" in html_rep


def test_email_view_retained_on_db_success_or_skipped():
    """Verify emails retain normal success appearance when database_status is success or skipped_not_configured."""
    for db_st in ["success", "skipped_not_configured"]:
        earn_res = EarningsRefreshJobResult(
            started_at="2026-08-18T05:00:00+00:00",
            finished_at="2026-08-18T05:01:00+00:00",
            duration_seconds=60.0,
            coverage_start="2026-08-18",
            coverage_end="2026-11-17",
            overall_status="success",
            bist=MarketRefreshOutcome(
                market="BIST100", outcome_status="success", fetch_status="success",
                sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"
            ),
            nasdaq=MarketRefreshOutcome(
                market="NASDAQ100", outcome_status="success", fetch_status="success",
                sync_action="replaced", coverage_start="2026-08-18", coverage_end="2026-11-17"
            ),
            database_status=db_st,
        )
        assert "✅" in format_earnings_refresh_subject(earn_res)
        assert build_earnings_refresh_email_view(earn_res)["status_conf"]["primary"] == "#166534"

        econ_res = EconomicCalendarRefreshJobResult(
            generated_at="2026-08-18T04:30:00+03:00",
            coverage_start="2026-08-18",
            coverage_end="2026-11-16",
            status="success",
            sync_action="replaced",
            state_source="data/generated/economic_calendar.json",
            diff=EconomicEventDiff(),
            database_status=db_st,
        )
        assert "✅" in format_economic_calendar_refresh_subject(econ_res)
        assert "FinBot Günlük Ekonomik Takvim Raporu — Başarılı" in format_economic_calendar_refresh_email_text(econ_res)


@pytest.mark.parametrize(
    "env_val,expected",
    [
        ("schedule", "scheduled"),
        ("workflow_dispatch", "manual"),
        (None, "manual"),
    ],
)
def test_resolve_refresh_trigger_type(monkeypatch, env_val, expected):
    """Verify resolve_refresh_trigger_type correctly identifies schedule vs manual triggers."""
    from app.services.database_refresh_sync import resolve_refresh_trigger_type

    if env_val is None:
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    else:
        monkeypatch.setenv("GITHUB_EVENT_NAME", env_val)

    assert resolve_refresh_trigger_type() == expected


def test_resolve_official_ir_aggregate_status_decision_matrix():
    """Verify resolve_official_ir_aggregate_status covers all required aggregate decision states."""
    from app.services.database_refresh_sync import resolve_official_ir_aggregate_status

    # 1. Empty provider list -> failed
    assert resolve_official_ir_aggregate_status([]) == "failed"

    # 2. 7 success -> success
    all_success = [
        OfficialIrProviderSummary(symbol=f"SYM{i}", source_provider=f"P{i}", status="success", parser_failed=False)
        for i in range(7)
    ]
    assert resolve_official_ir_aggregate_status(all_success) == "success"

    # 3. 7 partial -> partial
    all_partial = [
        OfficialIrProviderSummary(symbol=f"SYM{i}", source_provider=f"P{i}", status="partial", parser_failed=False)
        for i in range(7)
    ]
    assert resolve_official_ir_aggregate_status(all_partial) == "partial"

    # 4. 6 success + 1 partial -> partial
    six_succ_one_part = [
        OfficialIrProviderSummary(symbol=f"SYM{i}", source_provider=f"P{i}", status="success", parser_failed=False)
        for i in range(6)
    ] + [
        OfficialIrProviderSummary(symbol="PART", source_provider="PART", status="partial", parser_failed=False)
    ]
    assert resolve_official_ir_aggregate_status(six_succ_one_part) == "partial"

    # 5. 2 partial + 5 failed -> partial
    two_part_five_fail = [
        OfficialIrProviderSummary(symbol=f"PART{i}", source_provider=f"P{i}", status="partial", parser_failed=False)
        for i in range(2)
    ] + [
        OfficialIrProviderSummary(symbol=f"FAIL{i}", source_provider=f"P{i}", status="failed", parser_failed=False)
        for i in range(5)
    ]
    assert resolve_official_ir_aggregate_status(two_part_five_fail) == "partial"

    # 6. 7 failed -> failed
    all_failed = [
        OfficialIrProviderSummary(symbol=f"SYM{i}", source_provider=f"P{i}", status="failed", parser_failed=False)
        for i in range(7)
    ]
    assert resolve_official_ir_aggregate_status(all_failed) == "failed"

    # 7. status='partial', parser_failed=True evaluated in unavailable group -> 7 of them -> failed
    all_partial_parser_failed = [
        OfficialIrProviderSummary(symbol=f"SYM{i}", source_provider=f"P{i}", status="partial", parser_failed=True)
        for i in range(7)
    ]
    assert resolve_official_ir_aggregate_status(all_partial_parser_failed) == "failed"


def test_deactivation_audit_fields_sql():
    """Verify deactivation SQL sets last_refresh_run_id and last_changed_at without updating last_seen_at."""
    mock_conn = MagicMock()
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    executed = []
    cursor.execute.side_effect = lambda q, p: executed.append((q, p))

    run_id = uuid4()
    RefreshRepository.deactivate_missing_earnings_events(
        mock_conn,
        run_id,
        "BIST100",
        date(2026, 8, 18),
        date(2026, 11, 17),
        ["KAP:AEFES:2026-10-29"],
    )
    assert len(executed) == 1
    query, params = executed[0]
    assert "last_refresh_run_id = %s" in query
    assert "last_changed_at = now()" in query
    assert "last_seen_at" not in query
    assert params[0] == run_id


def test_kap_and_official_ir_warning_separation(mock_db_env):
    """Verify KAP provider result only contains KAP warnings and does not inherit MAVI Official IR errors."""
    recorded_runs = []
    recorded_prov_results = []

    with patch("app.repositories.refresh_repository.RefreshRepository.insert_refresh_run", side_effect=lambda c, d: (recorded_runs.append(d), uuid4())[1]), \
         patch("app.repositories.refresh_repository.RefreshRepository.insert_provider_run_results", side_effect=lambda c, r, res: (recorded_prov_results.extend(res), len(res))[1]):

        earn_res = EarningsRefreshJobResult(
            started_at="2026-08-18T05:00:00+00:00",
            finished_at="2026-08-18T05:01:00+00:00",
            duration_seconds=60.0,
            coverage_start="2026-08-18",
            coverage_end="2026-11-17",
            overall_status="partial",
            bist=MarketRefreshOutcome(
                market="BIST100",
                outcome_status="partial",
                fetch_status="partial",
                sync_action="merged_and_updated",
                coverage_start="2026-08-18",
                coverage_end="2026-11-17",
                warnings=["MAVI: HTTP 404 Client Error: Not Found"],
                official_ir_summary=OfficialIrRefreshSummary(
                    kap_fetch_status="success",
                    kap_fetched_event_count=4,
                    ir_fetched_event_count=0,
                    kap_warnings=[],  # KAP had zero warnings
                    providers=[
                        OfficialIrProviderSummary(
                            symbol="MAVI",
                            source_provider="MAVI Investor Relations",
                            status="failed",
                            warnings=["MAVI: HTTP 404 Client Error: Not Found", "MAVI: HTTP 404 Client Error: Not Found"],  # Duplicate warning
                        )
                    ],
                ),
            ),
            nasdaq=MarketRefreshOutcome(
                market="NASDAQ100",
                outcome_status="success",
                fetch_status="success",
                sync_action="replaced",
                coverage_start="2026-08-18",
                coverage_end="2026-11-17",
            ),
        )

        final_bist = Bist100EarningsCalendar(
            generated_at="2026-08-18T05:00:00",
            coverage_start="2026-08-18",
            coverage_end="2026-11-17",
            status="partial",
            provider_status="partial",
            events=[],
        )

        status, error = sync_earnings_refresh_to_database(earn_res, final_bist, None, refresh_run_id=uuid4())
        assert status == "success"
        assert error is None

        # Verify provider outcome generation via EarningsRefreshResultAdapter
        from app.services.job_tracking.adapters.earnings_adapter import (
            EarningsRefreshResultAdapter,
        )
        adapter = EarningsRefreshResultAdapter()
        outcome = adapter.to_outcome(earn_res)

        # 1. Top-level outcome must retain the operational warning
        assert "MAVI: HTTP 404 Client Error: Not Found" in outcome.warnings

        # 2. Exactly 3 provider results must be produced
        assert len(outcome.provider_outcomes) == 3

        # 3. Provider results verified with exact provider codes
        kap_prov = next(p for p in outcome.provider_outcomes if p.provider_code == "KAP")
        assert kap_prov.scope == "BIST100"
        assert kap_prov.fresh_event_count == 4
        assert kap_prov.warnings == []  # KAP has zero warnings

        ir_prov = next(p for p in outcome.provider_outcomes if p.provider_code == "OFFICIAL_IR")
        assert ir_prov.scope == "BIST100"
        assert ir_prov.fresh_event_count == 0
        assert ir_prov.status == "failed"
        assert ir_prov.warnings == ["MAVI: HTTP 404 Client Error: Not Found"]  # Deduplicated

        yahoo_prov = next(p for p in outcome.provider_outcomes if p.provider_code == "YAHOO_FINANCE")
        assert yahoo_prov.scope == "NASDAQ100"
        assert yahoo_prov.warnings == []


def test_no_official_ir_summary_fallback_does_not_attribute_bist_warnings_to_kap(mock_db_env):
    """Verify that when official_ir_summary is absent, only verified YAHOO_FINANCE provider outcome is generated and KAP/IR are not created."""
    earn_res = EarningsRefreshJobResult(
        started_at="2026-08-18T05:00:00+00:00",
        finished_at="2026-08-18T05:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-18",
        coverage_end="2026-11-17",
        overall_status="partial",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="partial",
            fetch_status="partial",
            sync_action="merged_and_updated",
            coverage_start="2026-08-18",
            coverage_end="2026-11-17",
            warnings=["Generic BIST combined warning"],
            official_ir_summary=None,  # No structured summary
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-18",
            coverage_end="2026-11-17",
        ),
    )

    status, error = sync_earnings_refresh_to_database(earn_res, None, None, refresh_run_id=uuid4())
    assert status == "success"
    assert error is None

    from app.services.job_tracking.adapters.earnings_adapter import (
        EarningsRefreshResultAdapter,
    )
    adapter = EarningsRefreshResultAdapter()
    outcome = adapter.to_outcome(earn_res)

    # Top-level outcome retains the warning
    assert "Generic BIST combined warning" in outcome.warnings

    # When no official_ir_summary exists, KAP and OFFICIAL_IR are omitted, only YAHOO_FINANCE is present
    assert len(outcome.provider_outcomes) == 1
    assert outcome.provider_outcomes[0].provider_code == "YAHOO_FINANCE"
    assert outcome.provider_outcomes[0].scope == "NASDAQ100"
