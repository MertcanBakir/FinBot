import pytest

from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    KapSymbolEarningsStatus,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsEvent,
)
from app.services.calendar_state_store import SupabaseCalendarStateStore


@pytest.fixture
def temp_cache(monkeypatch):
    store = SupabaseCalendarStateStore()
    monkeypatch.setattr("app.services.calendar_state_store.database_calendar_reader.load_bist", lambda: None)
    monkeypatch.setattr("app.services.calendar_state_store.database_calendar_reader.load_nasdaq", lambda: None)
    return store


# --- D. BIST Success Replace & Healthy Empty Tests ---

def test_bist_success_replace_and_healthy_empty(temp_cache):
    old_event = BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-20")
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        status="success",
        events=[old_event]
    )
    assert temp_cache.stage_bist(prev_cal) is not None

    # New success calendar with empty events (healthy empty)
    new_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="success",
        events=[]
    )
    assert temp_cache.stage_bist(new_cal) is not None

    loaded = temp_cache.load_bist()
    assert loaded is not None
    assert loaded.status == "success"
    assert len(loaded.events) == 0  # Old event completely replaced!


# --- E. BIST Partial Merge Tests ---

def test_bist_partial_merge_3_symbols(temp_cache):
    # Previous Cache
    prev_events = [
        BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30"),
        BistEarningsEvent(symbol="AKBNK", company_name="AKBNK", earnings_date="2026-08-20", period_start="2026-01-01", period_end="2026-06-30"),
        BistEarningsEvent(symbol="GARAN", company_name="GARAN", earnings_date="2026-09-10", period_start="2026-01-01", period_end="2026-06-30")
    ]
    prev_statuses = [
        KapSymbolEarningsStatus(symbol="CCOLA", is_complete=True, calendar_status="scheduled"),
        KapSymbolEarningsStatus(symbol="AKBNK", is_complete=True, calendar_status="scheduled"),
        KapSymbolEarningsStatus(symbol="GARAN", is_complete=True, calendar_status="scheduled")
    ]
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        status="success",
        events=prev_events,
        symbol_results=prev_statuses
    )
    assert temp_cache.stage_bist(prev_cal) is not None

    # Current Partial Refresh:
    # CCOLA -> complete with new date 2026-08-22
    # AKBNK -> complete with 0 events (healthy negative)
    # GARAN -> incomplete (is_complete = False)
    curr_events = [
        BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-22", period_start="2026-01-01", period_end="2026-06-30")
    ]
    curr_statuses = [
        KapSymbolEarningsStatus(symbol="CCOLA", is_complete=True, calendar_status="scheduled"),
        KapSymbolEarningsStatus(symbol="AKBNK", is_complete=True, calendar_status="no_announced_date_in_coverage"),
        KapSymbolEarningsStatus(symbol="GARAN", is_complete=False, calendar_status="unavailable", refresh_status="failed")
    ]
    curr_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        provider_status="partial",
        events=curr_events,
        symbol_results=curr_statuses
    )

    assert temp_cache.stage_bist(curr_cal) is not None
    merged = temp_cache.load_bist()

    assert merged is not None
    assert merged.status == "partial"
    assert merged.provider_status == "partial"

    events_map = {e.symbol: e for e in merged.events}
    # CCOLA has ONLY new date 2026-08-22
    assert events_map["CCOLA"].earnings_date == "2026-08-22"
    # AKBNK has NO events (healthy negative!)
    assert "AKBNK" not in events_map
    # GARAN previous event 2026-09-10 carried forward!
    assert events_map["GARAN"].earnings_date == "2026-09-10"

    sym_map = {s.symbol: s for s in merged.symbol_results}
    assert sym_map["GARAN"].calendar_status == "unavailable"
    assert not sym_map["GARAN"].is_complete
    assert any("Carried forward previously stored BIST events" in w for w in merged.warnings)


# --- F. Coverage Cleanup Test ---

def test_bist_partial_coverage_cleanup(temp_cache):
    # Previous event outside current coverage window (e.g. 2026-08-01)
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-01T12:00:00",
        coverage_start="2026-08-01",
        coverage_end="2026-10-31",
        status="success",
        events=[BistEarningsEvent(symbol="GARAN", company_name="GARAN", earnings_date="2026-08-01")],
        symbol_results=[KapSymbolEarningsStatus(symbol="GARAN", is_complete=True)]
    )
    assert temp_cache.stage_bist(prev_cal) is not None

    curr_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=[],
        symbol_results=[KapSymbolEarningsStatus(symbol="GARAN", is_complete=False, calendar_status="unavailable")]
    )
    assert temp_cache.stage_bist(curr_cal) is not None
    merged = temp_cache.load_bist()

    # GARAN's 2026-08-01 event is BEFORE 2026-08-13 -> EXCLUDED!
    assert len(merged.events) == 0


# --- G. Missing Current Symbol Status Test ---

def test_bist_missing_current_symbol_status(temp_cache):
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        events=[BistEarningsEvent(symbol="MISSINGSYM", company_name="MISSINGSYM", earnings_date="2026-09-01")],
        symbol_results=[KapSymbolEarningsStatus(symbol="MISSINGSYM", is_complete=True)]
    )
    assert temp_cache.stage_bist(prev_cal) is not None

    # Current partial refresh completely omits MISSINGSYM from symbol_results
    curr_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=[],
        symbol_results=[]
    )
    assert temp_cache.stage_bist(curr_cal) is not None
    merged = temp_cache.load_bist()

    assert len(merged.events) == 1
    assert merged.events[0].symbol == "MISSINGSYM"
    sym_map = {s.symbol: s for s in merged.symbol_results}
    assert sym_map["MISSINGSYM"].calendar_status == "unavailable"
    assert not sym_map["MISSINGSYM"].is_complete


# --- H. Failed BIST Preservation Tests ---

def test_failed_bist_preservation(temp_cache):
    prev_cal = Bist100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        events=[BistEarningsEvent(symbol="CCOLA", company_name="CCOLA", earnings_date="2026-08-20")]
    )
    assert temp_cache.stage_bist(prev_cal) is not None

    failed_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="failed",
        events=[]
    )

    # Failed payload returns False and does NOT overwrite
    assert temp_cache.stage_bist(failed_cal) is None
    loaded = temp_cache.load_bist()
    assert loaded.generated_at == "2026-08-12T12:00:00"
    assert len(loaded.events) == 1


def test_failed_bist_without_previous_cache(temp_cache):
    failed_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="failed",
        events=[]
    )
    assert temp_cache.stage_bist(failed_cal) is None
    assert temp_cache.load_bist() is None


# --- I. Nasdaq Save Policy Tests ---

def test_nasdaq_save_policy(temp_cache):
    prev_event = NasdaqEarningsEvent(symbol="AAPL", company_name="Apple", earnings_date="2026-08-15", session="pre_market", session_label="Pre-Market")
    prev_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-12T12:00:00",
        coverage_start="2026-08-12",
        coverage_end="2026-11-10",
        status="success",
        events=[prev_event]
    )
    assert temp_cache.stage_nasdaq(prev_cal) is not None

    # 1. Partial Nasdaq when valid previous cache exists -> returns False and preserves previous cache!
    partial_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=[NasdaqEarningsEvent(symbol="MSFT", company_name="Microsoft", earnings_date="2026-08-20", session="after_hours", session_label="After Hours")]
    )
    assert temp_cache.stage_nasdaq(partial_cal) is None
    loaded = temp_cache.load_nasdaq()
    assert loaded.events[0].symbol == "AAPL"

    # 2. Failed Nasdaq -> returns False
    failed_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="failed",
        events=[]
    )
    assert temp_cache.stage_nasdaq(failed_cal) is None
    assert temp_cache.load_nasdaq().events[0].symbol == "AAPL"

    # 3. Success replace -> replaces cache
    success_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="success",
        events=[NasdaqEarningsEvent(symbol="NVDA", company_name="Nvidia", earnings_date="2026-08-25", session="after_hours", session_label="After Hours")]
    )
    assert temp_cache.stage_nasdaq(success_cal) is not None
    assert temp_cache.load_nasdaq().events[0].symbol == "NVDA"


# --- J. Market Independence Test ---

def test_market_independence(temp_cache):
    bist_cal = Bist100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11")
    nasdaq_cal = Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11")

    assert temp_cache.stage_bist(bist_cal) is not None
    assert temp_cache.load_nasdaq() is None  # Saving BIST did not touch Nasdaq

    assert temp_cache.stage_nasdaq(nasdaq_cal) is not None
    assert temp_cache.load_bist() is not None  # Saving Nasdaq did not touch BIST

