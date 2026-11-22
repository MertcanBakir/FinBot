import json
import os
from unittest.mock import patch

import pytest

from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    KapEarningsFetchMetrics,
    KapSymbolEarningsStatus,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsEvent,
)
from app.services.calendar_state_store import (
    JsonCalendarStateStore,
)


@pytest.fixture
def temp_cache(tmp_path):
    bist_path = str(tmp_path / "bist100_earnings_calendar.json")
    nasdaq_path = str(tmp_path / "nasdaq100_earnings_calendar.json")
    return JsonCalendarStateStore(bist_file_path=bist_path, nasdaq_file_path=nasdaq_path)


# --- A. Round-Trip Tests ---

def test_bist_round_trip(temp_cache):
    event = BistEarningsEvent(
        symbol="CCOLA",
        company_name="COCA-COLA İÇECEK A.Ş. (Türkçe Karakter Testi)",
        earnings_date="2026-08-15",
        period_start="2026-04-01",
        period_end="2026-06-30",
        publish_date="2026-08-01"
    )
    sym_status = KapSymbolEarningsStatus(
        symbol="CCOLA",
        member_resolution_status="resolved",
        refresh_status="success",
        calendar_status="scheduled",
        scheduled_event_count=1,
        is_complete=True
    )
    metrics = KapEarningsFetchMetrics(input_symbol_count=100, resolved_symbol_count=100)
    calendar = Bist100EarningsCalendar(
        schema_version="2.0",
        generated_at="2026-08-13T12:00:00+03:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        market="BIST100",
        status="success",
        provider_status="success",
        events=[event],
        symbol_results=[sym_status],
        warnings=["Test warning"],
        metrics=metrics
    )

    assert temp_cache.save_bist(calendar) is True
    loaded = temp_cache.load_bist()

    assert loaded is not None
    assert loaded.schema_version == "2.0"
    assert loaded.market == "BIST100"
    assert loaded.events[0].company_name == "COCA-COLA İÇECEK A.Ş. (Türkçe Karakter Testi)"
    assert loaded.symbol_results[0].symbol == "CCOLA"
    assert loaded.metrics.input_symbol_count == 100
    assert loaded.warnings == ["Test warning"]


def test_nasdaq_round_trip(temp_cache):
    event = NasdaqEarningsEvent(
        symbol="AAPL",
        company_name="Apple Inc.",
        earnings_date="2026-08-15",
        session="pre_market",
        session_label="Pre-Market"
    )
    calendar = Nasdaq100EarningsCalendar(
        schema_version="2.0",
        generated_at="2026-08-13T12:00:00+03:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        market="NASDAQ100",
        status="success",
        provider_status="success",
        events=[event],
        warnings=[]
    )

    assert temp_cache.save_nasdaq(calendar) is True
    loaded = temp_cache.load_nasdaq()

    assert loaded is not None
    assert loaded.schema_version == "2.0"
    assert loaded.market == "NASDAQ100"
    assert loaded.events[0].symbol == "AAPL"


# --- B. Missing / Corrupt / Wrong Market Tests ---

def test_missing_corrupt_and_wrong_schema_cache(temp_cache, tmp_path):
    # 1. Missing file -> None
    assert temp_cache.load_bist() is None
    assert temp_cache.load_nasdaq() is None

    # 2. Corrupt JSON -> None, file not deleted
    corrupt_file = tmp_path / "bist100_earnings_calendar.json"
    corrupt_file.write_text("{invalid_json: true", encoding="utf-8")
    assert temp_cache.load_bist() is None
    assert corrupt_file.exists()

    # 3. Wrong schema / wrong market in BIST file -> None
    wrong_bist = tmp_path / "bist100_wrong.json"
    wrong_bist.write_text(json.dumps({"schema_version": "1.0", "market": "BIST100"}), encoding="utf-8")
    cache_wrong_bist = JsonCalendarStateStore(bist_file_path=str(wrong_bist))
    assert cache_wrong_bist.load_bist() is None

    # 4. Nasdaq loader rejects BIST payload
    bist_payload = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11"
    ).model_dump(mode="json")

    nasdaq_file = tmp_path / "nasdaq100_earnings_calendar.json"
    nasdaq_file.write_text(json.dumps(bist_payload), encoding="utf-8")
    assert temp_cache.load_nasdaq() is None


# --- C. Atomic Write & Exception Cleanup Tests ---

def test_atomic_write_success_and_error_handling(temp_cache, tmp_path):
    calendar = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11"
    )

    # Successful write
    assert temp_cache.save_bist(calendar) is True
    assert os.path.exists(temp_cache.bist_file_path)

    # Verify no leftover .tmp files
    parent_files = os.listdir(tmp_path)
    assert not any(f.endswith(".tmp") for f in parent_files)

    # Save initial content
    initial_bytes = open(temp_cache.bist_file_path, "rb").read()

    # Mock os.replace error
    with patch("os.replace", side_effect=OSError("Disk replace error")):
        success = temp_cache.save_bist(calendar)
        assert success is False
        # Existing file content unchanged
        assert open(temp_cache.bist_file_path, "rb").read() == initial_bytes
        # Temp file cleaned up
        parent_files = os.listdir(tmp_path)
        assert not any(f.endswith(".tmp") for f in parent_files)


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
    assert temp_cache.save_bist(prev_cal) is True

    # New success calendar with empty events (healthy empty)
    new_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="success",
        events=[]
    )
    assert temp_cache.save_bist(new_cal) is True

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
    assert temp_cache.save_bist(prev_cal) is True

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

    assert temp_cache.save_bist(curr_cal) is True
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
    assert any("Carried forward previous cached BIST events" in w for w in merged.warnings)


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
    assert temp_cache.save_bist(prev_cal) is True

    curr_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=[],
        symbol_results=[KapSymbolEarningsStatus(symbol="GARAN", is_complete=False, calendar_status="unavailable")]
    )
    assert temp_cache.save_bist(curr_cal) is True
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
    assert temp_cache.save_bist(prev_cal) is True

    # Current partial refresh completely omits MISSINGSYM from symbol_results
    curr_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=[],
        symbol_results=[]
    )
    assert temp_cache.save_bist(curr_cal) is True
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
    assert temp_cache.save_bist(prev_cal) is True

    failed_cal = Bist100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="failed",
        events=[]
    )

    # Failed payload returns False and does NOT overwrite
    assert temp_cache.save_bist(failed_cal) is False
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
    assert temp_cache.save_bist(failed_cal) is False
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
    assert temp_cache.save_nasdaq(prev_cal) is True

    # 1. Partial Nasdaq when valid previous cache exists -> returns False and preserves previous cache!
    partial_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="partial",
        events=[NasdaqEarningsEvent(symbol="MSFT", company_name="Microsoft", earnings_date="2026-08-20", session="after_hours", session_label="After Hours")]
    )
    assert temp_cache.save_nasdaq(partial_cal) is False
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
    assert temp_cache.save_nasdaq(failed_cal) is False
    assert temp_cache.load_nasdaq().events[0].symbol == "AAPL"

    # 3. Success replace -> replaces cache
    success_cal = Nasdaq100EarningsCalendar(
        generated_at="2026-08-13T12:00:00",
        coverage_start="2026-08-13",
        coverage_end="2026-11-11",
        status="success",
        events=[NasdaqEarningsEvent(symbol="NVDA", company_name="Nvidia", earnings_date="2026-08-25", session="after_hours", session_label="After Hours")]
    )
    assert temp_cache.save_nasdaq(success_cal) is True
    assert temp_cache.load_nasdaq().events[0].symbol == "NVDA"


# --- J. Market Independence Test ---

def test_market_independence(temp_cache):
    bist_cal = Bist100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11")
    nasdaq_cal = Nasdaq100EarningsCalendar(generated_at="2026-08-13T12:00:00", coverage_start="2026-08-13", coverage_end="2026-11-11")

    assert temp_cache.save_bist(bist_cal) is True
    assert temp_cache.load_nasdaq() is None  # Saving BIST did not touch Nasdaq

    assert temp_cache.save_nasdaq(nasdaq_cal) is True
    assert temp_cache.load_bist() is not None  # Saving Nasdaq did not touch BIST

