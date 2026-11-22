import json
import os
import shutil
import tempfile

import pytest

from app.models.earnings import BistEarningsEvent, BistFiscalPeriodEarningsCalendar
from app.services.bist_fiscal_period_archive import (
    BistFiscalPeriodArchive,
)


@pytest.fixture
def temp_cache_dir():
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)


def make_event(
    symbol: str = "PGSUS",
    company_name: str = "PEGASUS",
    fiscal_year: int = 2026,
    fiscal_period: str = "Q1",
    period_start: str = "2026-01-01",
    period_end: str = "2026-03-31",
    earnings_date: str = "2026-05-11",
    publish_date: str = "2026-04-16",
    disclosure_index: int = 1594449
) -> BistEarningsEvent:
    return BistEarningsEvent(
        symbol=symbol,
        company_name=company_name,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        period_start=period_start,
        period_end=period_end,
        earnings_date=earnings_date,
        publish_date=publish_date,
        date_type="company_announced",
        source="KAP",
        source_type="financial_calendar",
        disclosure_index=disclosure_index,
        source_url=f"https://www.kap.org.tr/tr/Bildirim/{disclosure_index}"
    )


def test_official_ir_company_expected_is_rejected_from_partition(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev_expected = BistEarningsEvent(
        symbol="AKSA",
        company_name="AKSA",
        fiscal_year=2026,
        fiscal_period="Q1",
        period_start="2026-01-01",
        period_end="2026-03-31",
        earnings_date="2026-05-07",
        publish_date=None,
        date_type="company_expected",  # MUST BE REJECTED
        source="Official IR",
        source_provider="AKSA Investor Relations",
        source_type="official_financial_calendar",
        source_event_id="aksa:2026:Q1",
        source_url="https://aksa"
    )
    is_valid, reason = cache._validate_event_for_partition(ev_expected)
    assert is_valid is False
    assert "invalid source metadata" in (reason or "")

    res = cache.upsert_events([ev_expected], status="success")
    assert res.status == "failed"
    assert res.files_created == 0
    assert len(os.listdir(temp_cache_dir)) == 0


def test_q1_and_q2_separate_partition_files(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev_q1 = make_event(fiscal_period="Q1", period_start="2026-01-01", period_end="2026-03-31", earnings_date="2026-05-11")
    ev_q2 = make_event(fiscal_period="Q2", period_start="2026-01-01", period_end="2026-06-30", earnings_date="2026-08-12")

    res = cache.upsert_events([ev_q1, ev_q2])
    assert res.status == "success"
    assert res.files_created == 2

    p_q1 = cache.load_partition(2026, "Q1")
    p_q2 = cache.load_partition(2026, "Q2")

    assert p_q1 is not None and len(p_q1.events) == 1
    assert p_q2 is not None and len(p_q2.events) == 1
    assert p_q1.events[0].earnings_date == "2026-05-11"
    assert p_q2.events[0].earnings_date == "2026-08-12"


def test_success_status_with_valid_and_invalid_mix(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev_valid = make_event()
    ev_invalid = BistEarningsEvent(symbol="BADSYM", company_name="BAD", earnings_date="2026-05-11")

    res = cache.upsert_events([ev_valid, ev_invalid], status="success")

    assert res.status == "partial"
    part = cache.load_partition(2026, "Q1")
    assert part is not None
    assert part.status == "partial"
    assert part.provider_status == "success"
    assert any("missing" in w for w in part.warnings)


def test_provider_partial_with_valid_event(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev_valid = make_event()

    res = cache.upsert_events([ev_valid], status="partial", warnings=["OID timeout"])

    assert res.status == "partial"
    part = cache.load_partition(2026, "Q1")
    assert part is not None
    assert part.status == "partial"
    assert part.provider_status == "partial"
    assert "OID timeout" in part.warnings


def test_identical_upsert_does_not_rewrite_file(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev = make_event()

    res1 = cache.upsert_events([ev], status="success", generated_at="2026-08-14T10:00:00Z")
    assert res1.files_created == 1

    filepath = cache._get_partition_path("Q1", 2026)
    with open(filepath, "rb") as f:
        bytes1 = f.read()

    res2 = cache.upsert_events([ev], status="success", generated_at="2026-08-14T12:00:00Z")
    assert res2.events_unchanged == 1
    assert res2.files_created == 0
    assert res2.files_updated == 0
    assert res2.affected_files == []

    with open(filepath, "rb") as f:
        bytes2 = f.read()

    assert bytes1 == bytes2


def test_partial_to_success_metadata_update(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev = make_event()

    # Initial partial save
    cache.upsert_events([ev], status="partial", generated_at="2026-08-14T10:00:00Z", warnings=["Warning 1"])
    p_initial = cache.load_partition(2026, "Q1")
    assert p_initial.status == "partial"
    assert p_initial.provider_status == "partial"

    # Re-upsert with status=success and new timestamp
    res = cache.upsert_events([ev], status="success", generated_at="2026-08-14T12:00:00Z", warnings=[])
    assert res.files_updated == 1

    p_updated = cache.load_partition(2026, "Q1")
    assert p_updated.status == "success"
    assert p_updated.provider_status == "success"
    assert p_updated.generated_at == "2026-08-14T12:00:00Z"


def test_warnings_change_triggers_metadata_update(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev = make_event()

    cache.upsert_events([ev], status="partial", generated_at="2026-08-14T10:00:00Z", warnings=["W1"])

    res = cache.upsert_events([ev], status="partial", generated_at="2026-08-14T12:00:00Z", warnings=["W1", "W2"])
    assert res.files_updated == 1

    p_updated = cache.load_partition(2026, "Q1")
    assert p_updated.warnings == ["W1", "W2"]
    assert p_updated.generated_at == "2026-08-14T12:00:00Z"


def test_cross_partition_duplicate_cleanup_incoming_same_identity(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)

    ev_q1 = make_event(
        symbol="PGSUS",
        fiscal_year=2026,
        fiscal_period="Q1",
        period_start="2026-01-01",
        period_end="2026-03-31",
        publish_date="2026-04-01",
        disclosure_index=100
    )
    cache.upsert_events([ev_q1])

    ev_q2 = make_event(
        symbol="PGSUS",
        fiscal_year=2026,
        fiscal_period="Q2",
        period_start="2026-01-01",
        period_end="2026-03-31",
        publish_date="2026-04-15",
        disclosure_index=200
    )
    res = cache.upsert_events([ev_q2])

    assert res.status == "success"
    p_q1 = cache.load_partition(2026, "Q1")
    p_q2 = cache.load_partition(2026, "Q2")

    assert p_q1 is not None and len(p_q1.events) == 0
    assert p_q2 is not None and len(p_q2.events) == 1
    assert p_q2.events[0].disclosure_index == 200

    assert "bist100_Q1_2026_earnings_calendar.json" in res.affected_files
    assert "bist100_Q2_2026_earnings_calendar.json" in res.affected_files


def test_preexisting_cross_partition_duplicate_cleanup_with_unrelated_incoming(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)

    # 1. Manually create Q1 partition with older PGSUS event
    ev_old = make_event(
        symbol="PGSUS",
        fiscal_year=2026,
        fiscal_period="Q1",
        period_start="2026-01-01",
        period_end="2026-03-31",
        publish_date="2026-04-01",
        disclosure_index=100
    )
    cal_q1 = BistFiscalPeriodEarningsCalendar(
        schema_version="2.1",
        dataset_type="fiscal_period_partition",
        generated_at="2026-08-14T10:00:00Z",
        market="BIST100",
        fiscal_year=2026,
        fiscal_period="Q1",
        status="success",
        events=[ev_old]
    )
    cache._atomic_save(cal_q1)

    # 2. Manually create Q2 partition with newer PGSUS event (same identity!)
    ev_new = make_event(
        symbol="PGSUS",
        fiscal_year=2026,
        fiscal_period="Q2",
        period_start="2026-01-01",
        period_end="2026-03-31",
        publish_date="2026-04-15",
        disclosure_index=200
    )
    cal_q2 = BistFiscalPeriodEarningsCalendar(
        schema_version="2.1",
        dataset_type="fiscal_period_partition",
        generated_at="2026-08-14T10:00:00Z",
        market="BIST100",
        fiscal_year=2026,
        fiscal_period="Q2",
        status="success",
        events=[ev_new]
    )
    cache._atomic_save(cal_q2)

    # 3. Now run upsert_events with ONLY an unrelated CCOLA event
    ev_ccola = make_event(
        symbol="CCOLA",
        company_name="COCA COLA",
        fiscal_year=2026,
        fiscal_period="Q2",
        period_start="2026-04-01",
        period_end="2026-06-30",
        earnings_date="2026-08-20",
        publish_date="2026-08-10",
        disclosure_index=300
    )

    res = cache.upsert_events([ev_ccola])

    assert res.status == "success"
    p_q1 = cache.load_partition(2026, "Q1")
    p_q2 = cache.load_partition(2026, "Q2")

    # Older PGSUS in Q1 must be proactively cleaned up!
    assert p_q1 is not None and len(p_q1.events) == 0
    # Q2 contains both winner PGSUS and new CCOLA
    assert p_q2 is not None and len(p_q2.events) == 2
    assert any(e.symbol == "PGSUS" and e.disclosure_index == 200 for e in p_q2.events)
    assert any(e.symbol == "CCOLA" for e in p_q2.events)

    # Affected files must include Q1 (cleaned) and Q2 (updated)
    assert "bist100_Q1_2026_earnings_calendar.json" in res.affected_files
    assert "bist100_Q2_2026_earnings_calendar.json" in res.affected_files


def test_invalid_incoming_events_handling(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev_valid = make_event()
    ev_invalid = BistEarningsEvent(symbol="BADSYM", company_name="BAD", earnings_date="2026-05-11")

    res = cache.upsert_events([ev_valid, ev_invalid])
    assert res.status == "partial"
    assert any("missing" in w for w in res.warnings)
    assert res.events_added == 1

    p = cache.load_partition(2026, "Q1")
    assert p is not None and len(p.events) == 1
    assert p.events[0].symbol == "PGSUS"


def test_all_invalid_incoming_events_fails_without_modifying_files(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    ev_invalid = BistEarningsEvent(symbol="BADSYM", company_name="BAD", earnings_date="2026-05-11")

    res = cache.upsert_events([ev_invalid])
    assert res.status == "failed"
    assert len(os.listdir(temp_cache_dir)) == 0


def test_filename_and_payload_metadata_mismatch_fails_safely(temp_cache_dir):
    cache = BistFiscalPeriodArchive(data_dir=temp_cache_dir)
    os.makedirs(temp_cache_dir, exist_ok=True)

    filepath = os.path.join(temp_cache_dir, "bist100_Q1_2026_earnings_calendar.json")
    bad_cal = BistFiscalPeriodEarningsCalendar(
        schema_version="2.1",
        dataset_type="fiscal_period_partition",
        generated_at="2026-08-14T10:00:00Z",
        market="BIST100",
        fiscal_year=2026,
        fiscal_period="Q2",
        status="success",
        events=[]
    )
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(bad_cal.model_dump(mode="json"), f)

    ev = make_event()
    res = cache.upsert_events([ev])

    assert res.status == "failed"
    assert "corrupt or invalid" in res.error

