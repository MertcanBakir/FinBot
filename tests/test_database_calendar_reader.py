from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.job_names import (
    JOB_NAME_EARNINGS_REFRESH,
    JOB_NAME_ECONOMIC_CALENDAR_REFRESH,
)
from app.jobs.economic_calendar_refresh_job import calculate_economic_diff
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsEvent,
)
from app.models.earnings_refresh import (
    EarningsRefreshJobResult,
    MarketRefreshOutcome,
)
from app.models.economic_calendar import RollingEconomicCalendar
from app.models.economic_calendar_refresh import EconomicCalendarRefreshJobResult
from app.models.market import EconomicEvent
from app.repositories.refresh_repository import RefreshRepository
from app.services.calendar_state_store import SupabaseCalendarStateStore
from app.services.database_calendar_reader import DatabaseCalendarReader
from app.services.earnings_refresh_report import (
    compare_bist_calendars,
    compare_nasdaq_calendars,
    format_earnings_refresh_subject,
)
from app.services.economic_calendar_refresh_report import (
    format_economic_calendar_refresh_subject,
)
from app.services.economic_calendar_state_store import (
    SupabaseEconomicCalendarStateStore,
)


def _cursor_context(*, one=None, all_rows=None, query_tracker=None):
    cursor = MagicMock()
    cursor.fetchone.return_value = one
    cursor.fetchall.return_value = all_rows or []
    if query_tracker is not None:
        def execute_track(query, params=None):
            query_tracker.append((query, params))
        cursor.execute.side_effect = execute_track
    context = MagicMock()
    context.__enter__.return_value = cursor
    context.__exit__.return_value = False
    return context


def test_production_calendar_facades_are_supabase_backed():
    assert SupabaseCalendarStateStore.state_source.startswith("supabase://")
    assert SupabaseEconomicCalendarStateStore().state_source.startswith("supabase://")


def test_canonical_job_names_contract():
    """Verify that reader and job specifications share exact canonical names and no legacy names exist."""
    # 1. Verify canonical values
    assert JOB_NAME_EARNINGS_REFRESH == "earnings_refresh_job"
    assert JOB_NAME_ECONOMIC_CALENDAR_REFRESH == "economic_calendar_refresh_job"

    # 2. Verify reader queries the exact canonical constants
    queries = []
    conn = MagicMock()
    state = (
        datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc),
        date(2026, 8, 30),
        date(2026, 11, 28),
        "success",
    )
    conn.cursor.side_effect = [
        _cursor_context(one=state, query_tracker=queries),
        _cursor_context(all_rows=[], query_tracker=queries),
        _cursor_context(one=state, query_tracker=queries),
        _cursor_context(all_rows=[], query_tracker=queries),
        _cursor_context(one=state, query_tracker=queries),
    ]

    with patch("app.services.database_calendar_reader.settings.SUPABASE_DATABASE_URL", "configured"), \
         patch("app.services.database_calendar_reader.get_db_connection", return_value=nullcontext(conn)), \
         patch.object(RefreshRepository, "get_weekly_economic_events", return_value=[]):
        reader = DatabaseCalendarReader()
        reader.load_bist()
        reader.load_nasdaq()
        reader.load_economic()

    latest_state_queries = [
        (q, p) for q, p in queries
        if "FROM refresh_runs" in q and "WHERE job_name = %s" in q
    ]
    assert len(latest_state_queries) == 3
    # BIST queried earnings_refresh_job
    assert latest_state_queries[0][1] == (JOB_NAME_EARNINGS_REFRESH,)
    # NASDAQ queried earnings_refresh_job
    assert latest_state_queries[1][1] == (JOB_NAME_EARNINGS_REFRESH,)
    # Economic queried economic_calendar_refresh_job
    assert latest_state_queries[2][1] == (JOB_NAME_ECONOMIC_CALENDAR_REFRESH,)

    # 3. Verify no legacy names exist in any Python source under app/
    app_dir = Path(__file__).resolve().parent.parent / "app"
    for py_file in app_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "daily_earnings_calendar_refresh" not in content, f"Legacy name found in {py_file}"
        assert "daily_economic_calendar_refresh" not in content, f"Legacy name found in {py_file}"


def test_database_reader_reconstructs_bist_calendar():
    conn = MagicMock()
    state = (
        datetime(2026, 8, 25, 5, 30, tzinfo=timezone.utc),
        date(2026, 8, 25),
        date(2026, 11, 23),
        "success",
    )
    event = (
        "THYAO", "Türk Hava Yolları", 2026, "Q2", date(2026, 8, 26),
        date(2026, 1, 1), date(2026, 6, 30), date(2026, 8, 1),
        "company_announced", "KAP", "KAP Financial Calendar",
        "financial_calendar", "kap-1", 123, "https://example.test/kap-1",
    )
    conn.cursor.side_effect = [
        _cursor_context(one=state),
        _cursor_context(all_rows=[event]),
    ]

    with patch("app.services.database_calendar_reader.settings.SUPABASE_DATABASE_URL", "configured"), \
         patch("app.services.database_calendar_reader.get_db_connection", return_value=nullcontext(conn)):
        calendar = DatabaseCalendarReader().load_bist()

    assert calendar is not None
    assert calendar.coverage_start == "2026-08-25"
    assert calendar.events[0].symbol == "THYAO"
    assert calendar.events[0].source == "KAP"


def test_database_reader_reconstructs_nasdaq_calendar():
    conn = MagicMock()
    state = (
        datetime(2026, 8, 25, 5, 30, tzinfo=timezone.utc),
        date(2026, 8, 25),
        date(2026, 11, 23),
        "success",
    )
    event = (
        "AAPL", "Apple Inc.", date(2026, 10, 25), "after_hours",
    )
    conn.cursor.side_effect = [
        _cursor_context(one=state),
        _cursor_context(all_rows=[event]),
    ]

    with patch("app.services.database_calendar_reader.settings.SUPABASE_DATABASE_URL", "configured"), \
         patch("app.services.database_calendar_reader.get_db_connection", return_value=nullcontext(conn)):
        calendar = DatabaseCalendarReader().load_nasdaq()

    assert calendar is not None
    assert calendar.status == "success"
    assert calendar.provider_status == "success"
    assert calendar.coverage_start == "2026-08-25"
    assert calendar.coverage_end == "2026-11-23"
    assert len(calendar.events) == 1
    assert calendar.events[0].symbol == "AAPL"
    assert calendar.events[0].company_name == "Apple Inc."
    assert calendar.events[0].earnings_date == "2026-10-25"
    assert calendar.events[0].session == "after_hours"
    assert calendar.events[0].session_label == "After Hours"


def test_database_reader_reconstructs_economic_calendar():
    conn = MagicMock()
    state = (
        datetime(2026, 8, 25, 4, 45, tzinfo=timezone.utc),
        date(2026, 8, 25),
        date(2026, 11, 23),
        "partial",
    )
    conn.cursor.return_value = _cursor_context(one=state)
    events = [
        EconomicEvent(
            name="TCMB PPK",
            country="TR",
            scheduled_at="2026-08-27T11:00:00+00:00",
            scheduled_at_istanbul="2026-08-27T14:00:00+03:00",
            source="TCMB",
        )
    ]

    with patch("app.services.database_calendar_reader.settings.SUPABASE_DATABASE_URL", "configured"), \
         patch("app.services.database_calendar_reader.get_db_connection", return_value=nullcontext(conn)), \
         patch.object(RefreshRepository, "get_weekly_economic_events", return_value=events):
        calendar = DatabaseCalendarReader().load_economic()

    assert calendar is not None
    assert calendar.status == "partial"
    assert calendar.events[0].name == "TCMB PPK"
    assert calendar.provider_results == []


def test_database_reader_does_not_fall_back_without_supabase_config():
    with patch("app.services.database_calendar_reader.settings.SUPABASE_DATABASE_URL", ""):
        reader = DatabaseCalendarReader()
        assert reader.load_bist() is None
        assert reader.load_nasdaq() is None
        assert reader.load_economic() is None


def test_state_restoration_and_diff_rerun_identical_earnings_calendar():
    """Verify that restoring the previous state from Supabase results in 0 added/changed/removed and 'Güncel' subject."""
    cov_start = date(2026, 8, 30)
    cov_end = date(2026, 11, 28)

    # 4 BIST events
    bist_events = [
        BistEarningsEvent(
            symbol=f"SYM{i}",
            company_name=f"Company {i}",
            fiscal_year=2026,
            fiscal_period="Q2",
            earnings_date=f"2026-09-{10+i:02d}",
            period_start="2026-01-01",
            period_end="2026-06-30",
            source="KAP",
            source_provider="KAP",
            source_type="financial_calendar",
            date_type="company_announced",
        )
        for i in range(1, 5)
    ]
    # 108 NASDAQ events
    nasdaq_events = [
        NasdaqEarningsEvent(
            symbol=f"NDX{i}",
            company_name=f"Nasdaq Co {i}",
            earnings_date=f"2026-10-{(i % 20) + 1:02d}",
            session="after_hours",
            session_label="After Hours",
        )
        for i in range(1, 109)
    ]

    # Reconstructed previous calendar from Supabase
    previous_bist = Bist100EarningsCalendar(
        generated_at="2026-08-29T05:00:00",
        coverage_start="2026-08-30",
        coverage_end="2026-11-28",
        status="success",
        provider_status="success",
        events=bist_events,
    )
    previous_nasdaq = Nasdaq100EarningsCalendar(
        generated_at="2026-08-29T05:00:00",
        coverage_start="2026-08-30",
        coverage_end="2026-11-28",
        status="success",
        provider_status="success",
        events=nasdaq_events,
    )

    # Identical incoming current fetch and final calendars
    current_bist = previous_bist.model_copy(deep=True)
    current_nasdaq = previous_nasdaq.model_copy(deep=True)

    bist_diff = compare_bist_calendars(previous_bist, current_bist, current_bist, cov_start, cov_end)
    nasdaq_diff = compare_nasdaq_calendars(previous_nasdaq, current_nasdaq, current_nasdaq, cov_start, cov_end)

    # BIST diff must be clean
    assert bist_diff.added_count == 0
    assert bist_diff.changed_count == 0
    assert bist_diff.removed_count == 0
    assert bist_diff.unchanged_count == 4
    assert bist_diff.aged_out_count == 0

    # NASDAQ diff must be clean
    assert nasdaq_diff.added_count == 0
    assert nasdaq_diff.changed_count == 0
    assert nasdaq_diff.removed_count == 0
    assert nasdaq_diff.unchanged_count == 108
    assert nasdaq_diff.aged_out_count == 0

    # Subject must be "Güncel", NOT "112 değişiklik"
    job_result = EarningsRefreshJobResult(
        started_at="2026-08-30T05:00:00+00:00",
        finished_at="2026-08-30T05:01:00+00:00",
        duration_seconds=60.0,
        coverage_start="2026-08-30",
        coverage_end="2026-11-28",
        overall_status="success",
        bist=MarketRefreshOutcome(
            market="BIST100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-30",
            coverage_end="2026-11-28",
            diff=bist_diff,
        ),
        nasdaq=MarketRefreshOutcome(
            market="NASDAQ100",
            outcome_status="success",
            fetch_status="success",
            sync_action="replaced",
            coverage_start="2026-08-30",
            coverage_end="2026-11-28",
            diff=nasdaq_diff,
        ),
    )

    subject = format_earnings_refresh_subject(job_result)
    assert "Güncel" in subject
    assert "112 değişiklik" not in subject
    assert "değişiklik" not in subject


def test_real_changes_distinction():
    """Verify that real additions, date changes, removals, and aged_out events are properly categorized."""
    cov_start = date(2026, 8, 30)
    cov_end = date(2026, 11, 28)

    # Previous calendar has 4 events:
    # A (unchanged), B (will change date), C (will be removed), D (aged out before cov_start)
    ev_a = BistEarningsEvent(symbol="A", company_name="Co A", fiscal_year=2026, fiscal_period="Q2", earnings_date="2026-09-15", period_start="2026-01-01", period_end="2026-06-30", source="KAP", source_provider="KAP", source_type="fc", date_type="ca")
    ev_b = BistEarningsEvent(symbol="B", company_name="Co B", fiscal_year=2026, fiscal_period="Q2", earnings_date="2026-09-20", period_start="2026-01-01", period_end="2026-06-30", source="KAP", source_provider="KAP", source_type="fc", date_type="ca")
    ev_c = BistEarningsEvent(symbol="C", company_name="Co C", fiscal_year=2026, fiscal_period="Q2", earnings_date="2026-09-25", period_start="2026-01-01", period_end="2026-06-30", source="KAP", source_provider="KAP", source_type="fc", date_type="ca")
    ev_d = BistEarningsEvent(symbol="D", company_name="Co D", fiscal_year=2026, fiscal_period="Q2", earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30", source="KAP", source_provider="KAP", source_type="fc", date_type="ca")  # Aged out

    previous_cal = Bist100EarningsCalendar(
        generated_at="2026-08-20T05:00:00", coverage_start="2026-08-20", coverage_end="2026-11-18",
        status="success", provider_status="success", events=[ev_a, ev_b, ev_c, ev_d]
    )

    # Current calendar has:
    # A (same), B (date moved to 2026-09-22), E (newly added)
    ev_b_changed = ev_b.model_copy(update={"earnings_date": "2026-09-22"})
    ev_e_new = BistEarningsEvent(symbol="E", company_name="Co E", fiscal_year=2026, fiscal_period="Q2", earnings_date="2026-10-01", period_start="2026-01-01", period_end="2026-06-30", source="KAP", source_provider="KAP", source_type="fc", date_type="ca")

    final_cal = Bist100EarningsCalendar(
        generated_at="2026-08-30T05:00:00", coverage_start="2026-08-30", coverage_end="2026-11-28",
        status="success", provider_status="success", events=[ev_a, ev_b_changed, ev_e_new]
    )

    diff = compare_bist_calendars(previous_cal, final_cal, final_cal, cov_start, cov_end)

    assert diff.unchanged_count == 1
    assert diff.changed_count == 1
    assert diff.removed_count == 1
    assert diff.added_count == 1
    assert diff.aged_out_count == 1

    assert any(d.change_type == "changed" and d.symbol == "B" for d in diff.details)
    assert any(d.change_type == "removed" and d.symbol == "C" for d in diff.details)
    assert any(d.change_type == "added" and d.symbol == "E" for d in diff.details)
    assert any(d.change_type == "aged_out" and d.symbol == "D" for d in diff.details)


def test_new_process_state_store_restores_from_database():
    """Verify that a fresh process without pending memory state loads correctly from database using canonical job names."""
    state_bist = (
        datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc),
        date(2026, 8, 30),
        date(2026, 11, 28),
        "success",
    )
    event_bist = (
        "AKBNK", "Akbank", 2026, "Q2", date(2026, 9, 1),
        date(2026, 1, 1), date(2026, 6, 30), None,
        "company_announced", "KAP", "KAP",
        "financial_calendar", "ev-1", None, None,
    )
    state_nasdaq = (
        datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc),
        date(2026, 8, 30),
        date(2026, 11, 28),
        "success",
    )
    event_nasdaq = (
        "NVDA", "NVIDIA Corporation", date(2026, 10, 15), "pre_market",
    )
    state_econ = (
        datetime(2026, 8, 30, 4, 30, tzinfo=timezone.utc),
        date(2026, 8, 30),
        date(2026, 11, 28),
        "success",
    )
    event_econ = EconomicEvent(
        name="Sanayi Üretimi",
        country="TR",
        scheduled_at="2026-09-10T07:00:00+00:00",
        scheduled_at_istanbul="2026-09-10T10:00:00+03:00",
        source="TUIK",
    )

    queries = []
    conn = MagicMock()
    conn.cursor.side_effect = [
        _cursor_context(one=state_bist, query_tracker=queries),
        _cursor_context(all_rows=[event_bist], query_tracker=queries),
        _cursor_context(one=state_nasdaq, query_tracker=queries),
        _cursor_context(all_rows=[event_nasdaq], query_tracker=queries),
        _cursor_context(one=state_econ, query_tracker=queries),
    ]

    with patch("app.services.database_calendar_reader.settings.SUPABASE_DATABASE_URL", "configured"), \
         patch("app.services.database_calendar_reader.get_db_connection", return_value=nullcontext(conn)), \
         patch.object(RefreshRepository, "get_weekly_economic_events", return_value=[event_econ]):

        # 1. Fresh earnings state store (new process with zero in-memory cache)
        earnings_store = SupabaseCalendarStateStore()
        assert earnings_store._pending_bist is None
        assert earnings_store._pending_nasdaq is None

        loaded_bist = earnings_store.load_bist()
        assert loaded_bist is not None
        assert len(loaded_bist.events) == 1
        assert loaded_bist.events[0].symbol == "AKBNK"
        assert loaded_bist.events[0].earnings_date == "2026-09-01"

        loaded_nasdaq = earnings_store.load_nasdaq()
        assert loaded_nasdaq is not None
        assert len(loaded_nasdaq.events) == 1
        assert loaded_nasdaq.events[0].symbol == "NVDA"
        assert loaded_nasdaq.events[0].company_name == "NVIDIA Corporation"
        assert loaded_nasdaq.events[0].earnings_date == "2026-10-15"
        assert loaded_nasdaq.events[0].session == "pre_market"
        assert loaded_nasdaq.events[0].session_label == "Pre-Market"

        # 2. Fresh economic state store (new process with zero in-memory cache)
        econ_store = SupabaseEconomicCalendarStateStore()
        assert econ_store._pending is None

        loaded_econ = econ_store.load()
        assert loaded_econ is not None
        assert len(loaded_econ.events) == 1
        assert loaded_econ.events[0].name == "Sanayi Üretimi"

    # Verify that both BIST and NASDAQ used JOB_NAME_EARNINGS_REFRESH and Economic used JOB_NAME_ECONOMIC_CALENDAR_REFRESH
    state_queries = [
        (q, p) for q, p in queries
        if "FROM refresh_runs" in q and "WHERE job_name = %s" in q
    ]
    assert len(state_queries) == 3
    assert state_queries[0][1] == (JOB_NAME_EARNINGS_REFRESH,)
    assert state_queries[1][1] == (JOB_NAME_EARNINGS_REFRESH,)
    assert state_queries[2][1] == (JOB_NAME_ECONOMIC_CALENDAR_REFRESH,)


def test_economic_calendar_rerun_identical_state():
    """Verify that identical rerun of economic calendar produces 0 added/changed/removed and 'Güncel' subject."""
    events = [
        EconomicEvent(
            name=f"Event {i}",
            country="TR",
            scheduled_at=f"2026-09-{10+i:02d}T07:00:00+00:00",
            scheduled_at_istanbul=f"2026-09-{10+i:02d}T10:00:00+03:00",
            source="TUIK",
        )
        for i in range(1, 6)
    ]

    previous_cal = RollingEconomicCalendar(
        generated_at="2026-08-30T04:30:00+00:00",
        coverage_start="2026-08-30",
        coverage_end="2026-11-28",
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[],
        events=events,
    )
    current_cal = previous_cal.model_copy(deep=True)

    diff = calculate_economic_diff(previous_cal, current_cal, current_cal)

    assert diff.added_count == 0
    assert diff.changed_count == 0
    assert diff.removed_count == 0
    assert diff.carried_forward_count == 0
    assert diff.unchanged_count == 5

    job_result = EconomicCalendarRefreshJobResult(
        generated_at="2026-08-30T04:30:00+03:00",
        coverage_start="2026-08-30",
        coverage_end="2026-11-28",
        status="success",
        sync_action="replaced",
        state_source="supabase://economic_events",
        diff=diff,
    )

    subject = format_economic_calendar_refresh_subject(job_result)
    assert "Güncel" in subject
    assert "değişiklik" not in subject
