from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.economic_calendar import EconomicProviderResult, RollingEconomicCalendar
from app.models.market import EconomicEvent
from app.providers.calendar import CalendarProviderFetchResult, match_tracked_event
from app.services.economic_calendar import EconomicCalendarService


def test_tracked_events_allowlist_matching():
    assert match_tracked_event("TÜİK", "Tüketici Fiyat Endeksi (TÜFE)") == "Türkiye TÜFE"
    assert match_tracked_event("BLS", "Employment Situation") == "ABD İstihdam Raporu (NFP)"
    assert match_tracked_event("Federal Reserve", "FOMC Meeting") == "Fed Faiz Kararı (FOMC)"
    assert match_tracked_event("TÜİK", "Bilinmeyen İstatistik Anketi") is None

@pytest.mark.asyncio
async def test_get_rolling_calendar_five_providers_success():
    tz_tr = ZoneInfo("Europe/Istanbul")
    ref_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr)

    mock_providers = []
    for pid, pname in [
        ("TUIK", "TÜİK"), ("TCMB", "TCMB"), ("BLS", "BLS"), ("BEA", "BEA"), ("FED", "FED")
    ]:
        p = MagicMock()
        p.provider_id = pid
        p.provider_name = pname
        p.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
            events=[
                EconomicEvent(
                    name=f"Event {pid}", country="TR" if pid in ("TUIK", "TCMB") else "US",
                    scheduled_at="", scheduled_at_istanbul="2026-08-20T10:00:00+03:00",
                    source=pname, provider_id=pid
                )
            ],
            warnings=[],
            structure_validated=True,
            candidate_count=1,
            malformed_count=0
        ))
        mock_providers.append(p)

    service = EconomicCalendarService(providers=mock_providers)
    cal = await service.get_rolling_calendar(ref_dt)

    assert cal.status == "success"
    assert len(cal.provider_results) == 5
    assert all(pr.status == "success" for pr in cal.provider_results)
    assert len(cal.events) == 5

@pytest.mark.asyncio
async def test_get_rolling_calendar_malformed_event_cannot_be_healthy_empty():
    tz_tr = ZoneInfo("Europe/Istanbul")
    ref_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr)

    p1 = MagicMock()
    p1.provider_id = "TUIK"
    p1.provider_name = "TÜİK"
    p1.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
        events=[
            EconomicEvent(
                name="Türkiye TÜFE",
                country="TR",
                scheduled_at="invalid",
                scheduled_at_istanbul="not-a-date",
                source="TÜİK",
                provider_id="TUIK"
            )
        ],
        warnings=[],
        structure_validated=True,
        candidate_count=1,
        malformed_count=0
    ))

    service = EconomicCalendarService(providers=[p1])
    cal = await service.get_rolling_calendar(ref_dt)

    assert cal.status == "failed"
    assert len(cal.events) == 0
    pr = cal.provider_results[0]
    assert pr.status == "failed"
    assert len(pr.warnings) > 0
    assert "invalid event timestamp" in pr.warnings[0]
    assert any("invalid event timestamp" in w for w in cal.warnings)

# ----------------------------------------------------
# STABLE IDENTITY CONFLICT & DUPLICATE TESTS
# ----------------------------------------------------

@pytest.mark.asyncio
async def test_test_a_exact_duplicate_same_timestamp():
    tz_tr = ZoneInfo("Europe/Istanbul")
    ref_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr)

    # Provider returns two exact duplicate events
    p1 = MagicMock()
    p1.provider_id = "BEA"
    p1.provider_name = "BEA"
    p1.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
        events=[
            EconomicEvent(
                name="ABD GSYH", country="US",
                scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second"
            ),
            EconomicEvent(
                name="ABD GSYH", country="US",
                scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second"
            )
        ],
        warnings=[],
        structure_validated=True,
        candidate_count=2,
        malformed_count=0
    ))

    service = EconomicCalendarService(providers=[p1])
    cal = await service.get_rolling_calendar(ref_dt)

    assert len(cal.events) == 1
    assert cal.events[0].source_event_id == "BEA:US_GDP:2026-Q2:second"
    assert cal.events[0].reference_period == "2026-Q2"
    assert cal.status == "success"
    assert cal.provider_results[0].event_count == 1

@pytest.mark.asyncio
async def test_test_b_conflicting_timestamps_no_previous():
    tz_tr = ZoneInfo("Europe/Istanbul")
    ref_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr)

    # Provider returns two events with the same stable identity and conflicting timestamps
    p1 = MagicMock()
    p1.provider_id = "BEA"
    p1.provider_name = "BEA"
    p1.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
        events=[
            EconomicEvent(
                name="ABD GSYH", country="US",
                scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second"
            ),
            EconomicEvent(
                name="ABD GSYH", country="US",
                scheduled_at="", scheduled_at_istanbul="2026-08-28T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second"
            )
        ],
        warnings=[],
        structure_validated=True,
        candidate_count=2,
        malformed_count=0
    ))

    service = EconomicCalendarService(providers=[p1])
    cal = await service.get_rolling_calendar(ref_dt)

    # Conflicting events must be excluded from current events; 0 total events -> status is failed (Rule C)
    assert cal.status == "failed"
    pr = cal.provider_results[0]
    assert pr.status == "failed"
    assert pr.event_count == 0
    assert cal.events == []
    assert len(pr.warnings) == 1
    assert "Conflicting events detected for stable identity 'BEA:US_GDP:2026-Q2:second'" in pr.warnings[0]
    assert "conflicting current events were excluded" in pr.warnings[0]

@pytest.mark.asyncio
async def test_test_c_conflicting_timestamps_plus_valid_event():
    tz_tr = ZoneInfo("Europe/Istanbul")
    ref_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr)

    # Provider returns 2 conflicting GDP Second Estimate + 1 valid PCE
    p1 = MagicMock()
    p1.provider_id = "BEA"
    p1.provider_name = "BEA"
    p1.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
        events=[
            EconomicEvent(
                name="ABD GSYH", country="US",
                scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second"
            ),
            EconomicEvent(
                name="ABD GSYH", country="US",
                scheduled_at="", scheduled_at_istanbul="2026-08-28T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second"
            ),
            EconomicEvent(
                name="ABD PCE / Personal Income and Outlays", country="US",
                scheduled_at="", scheduled_at_istanbul="2026-08-28T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_PCE", reference_period="2026-07",
                source_event_id="BEA:US_PCE:2026-07"
            )
        ],
        warnings=[],
        structure_validated=True,
        candidate_count=3,
        malformed_count=0
    ))

    service = EconomicCalendarService(providers=[p1])
    cal = await service.get_rolling_calendar(ref_dt)

    assert cal.status == "partial"
    pr = cal.provider_results[0]
    assert pr.status == "partial"
    assert pr.event_count == 1
    assert len(cal.events) == 1
    assert cal.events[0].name == "ABD PCE / Personal Income and Outlays"
    assert not any(e.name == "ABD GSYH" for e in cal.events)

@pytest.mark.asyncio
async def test_get_rolling_calendar_warning_exact_deduplication():
    tz_tr = ZoneInfo("Europe/Istanbul")
    ref_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr)

    shared_warn = "Generic Table Parser Notice"
    p1 = MagicMock()
    p1.provider_id = "TUIK"
    p1.provider_name = "TÜİK"
    p1.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
        events=[],
        warnings=[shared_warn, shared_warn],
        structure_validated=True,
        candidate_count=0,
        malformed_count=0
    ))

    service = EconomicCalendarService(providers=[p1])
    cal = await service.get_rolling_calendar(ref_dt)

    assert cal.warnings.count(shared_warn) == 1


# ----------------------------------------------------
# UNIFIED IDENTITY & REGRESSION TESTS
# ----------------------------------------------------

@pytest.mark.asyncio
async def test_service_preserves_same_timestamp_different_periods(tmp_path):
    import json
    import os

    from app.jobs.validate_economic_calendar_state import (
        validate_economic_calendar_state_source,
    )
    from app.providers.calendar.tuik_calendar import TuikCalendarProvider

    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "economic_calendar", "tuik_annual_2026.json")
    with open(fixture_path, encoding="utf-8") as f:
        tuik_json = json.load(f)

    tz_tr = ZoneInfo("Europe/Istanbul")
    ref_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr)

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.json = MagicMock(return_value=tuik_json)
        res.raise_for_status = MagicMock()
        mock_get.return_value = res

        tuik_provider = TuikCalendarProvider()
        # Mock other 4 providers as healthy empty
        empty_providers = []
        for pid, pname in [("TCMB", "TCMB"), ("BLS", "BLS"), ("BEA", "BEA"), ("FED", "FED")]:
            p = MagicMock()
            p.provider_id = pid
            p.provider_name = pname
            p.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
                events=[], warnings=[], structure_validated=True, observed_candidate_count=0, candidate_count=0, malformed_count=0
            ))
            empty_providers.append(p)

        service = EconomicCalendarService(providers=[tuik_provider] + empty_providers)
        cal = await service.get_rolling_calendar(ref_dt)

        assert cal.status == "success"
        tuik_pr = next(pr for pr in cal.provider_results if pr.provider_id == "TUIK")
        assert tuik_pr.event_count == 12
        assert len(cal.events) == 12

        # Invariant check
        assert sum(pr.event_count for pr in cal.provider_results) == len(cal.events)

        # Both GSYH events preserved simultaneously at 2026-08-31T10:00:00+03:00
        annual_gdp = [
            e for e in cal.events
            if e.event_code == "TR_GDP" and e.reference_period == "2025" and e.scheduled_at_istanbul == "2026-08-31T10:00:00+03:00"
        ]
        assert len(annual_gdp) == 1
        assert annual_gdp[0].source_event_id == "TUIK:TR_GDP:2025"

        quarterly_gdp = [
            e for e in cal.events
            if e.event_code == "TR_GDP" and e.reference_period == "2026-Q2" and e.scheduled_at_istanbul == "2026-08-31T10:00:00+03:00"
        ]
        assert len(quarterly_gdp) == 1
        assert quarterly_gdp[0].source_event_id == "TUIK:TR_GDP:2026-Q2"

        # Model serialization and reload
        cal_json = cal.model_dump_json(indent=2)
        reloaded = RollingEconomicCalendar.model_validate_json(cal_json)
        assert len(reloaded.events) == 12

        # Cache file validator accepts this calendar
        temp_cache = tmp_path / "economic_calendar.json"
        temp_cache.write_text(cal_json, encoding="utf-8")
        is_valid, val_errors = validate_economic_calendar_state_source(temp_cache, require_current_coverage=False)
        assert is_valid is True
        assert len(val_errors) == 0


def test_model_accepts_same_timestamp_different_periods():
    start_d = date(2026, 8, 17)
    end_d = start_d + timedelta(days=90)
    cal = RollingEconomicCalendar(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end=end_d.isoformat(),
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=2)
        ],
        events=[
            EconomicEvent(
                name="Gayrisafi Yurt İçi Hasıla", country="TR",
                scheduled_at="2026-08-31T10:00:00+03:00", scheduled_at_istanbul="2026-08-31T10:00:00+03:00",
                source="TÜİK", provider_id="TUIK", event_code="TR_GDP", reference_period="2025",
                source_event_id="TUIK:TR_GDP:2025"
            ),
            EconomicEvent(
                name="Gayrisafi Yurt İçi Hasıla", country="TR",
                scheduled_at="2026-08-31T10:00:00+03:00", scheduled_at_istanbul="2026-08-31T10:00:00+03:00",
                source="TÜİK", provider_id="TUIK", event_code="TR_GDP", reference_period="2026-Q2",
                source_event_id="TUIK:TR_GDP:2026-Q2"
            )
        ]
    )
    assert len(cal.events) == 2


def test_merge_preserves_same_timestamp_different_periods():
    from app.services.economic_calendar_state_store import (
        merge_partial_economic_calendar,
    )
    start_d = date(2026, 8, 17)
    end_d = start_d + timedelta(days=90)
    curr = RollingEconomicCalendar(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end=end_d.isoformat(),
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=2)
        ],
        events=[
            EconomicEvent(
                name="Gayrisafi Yurt İçi Hasıla", country="TR",
                scheduled_at="2026-08-31T10:00:00+03:00", scheduled_at_istanbul="2026-08-31T10:00:00+03:00",
                source="TÜİK", provider_id="TUIK", event_code="TR_GDP", reference_period="2025",
                source_event_id="TUIK:TR_GDP:2025"
            ),
            EconomicEvent(
                name="Gayrisafi Yurt İçi Hasıla", country="TR",
                scheduled_at="2026-08-31T10:00:00+03:00", scheduled_at_istanbul="2026-08-31T10:00:00+03:00",
                source="TÜİK", provider_id="TUIK", event_code="TR_GDP", reference_period="2026-Q2",
                source_event_id="TUIK:TR_GDP:2026-Q2"
            )
        ]
    )
    merged = merge_partial_economic_calendar(None, curr)
    assert len(merged.events) == 2
    assert {e.reference_period for e in merged.events} == {"2025", "2026-Q2"}


def test_diff_tracks_periods_independently():
    from app.jobs.economic_calendar_refresh_job import calculate_economic_diff
    start_d = date(2026, 8, 17)
    end_d = start_d + timedelta(days=90)

    ev_2025 = EconomicEvent(
        name="Gayrisafi Yurt İçi Hasıla", country="TR",
        scheduled_at="2026-08-31T10:00:00+03:00", scheduled_at_istanbul="2026-08-31T10:00:00+03:00",
        source="TÜİK", provider_id="TUIK", event_code="TR_GDP", reference_period="2025",
        source_event_id="TUIK:TR_GDP:2025"
    )
    ev_q2_old = EconomicEvent(
        name="Gayrisafi Yurt İçi Hasıla", country="TR",
        scheduled_at="2026-08-31T10:00:00+03:00", scheduled_at_istanbul="2026-08-31T10:00:00+03:00",
        source="TÜİK", provider_id="TUIK", event_code="TR_GDP", reference_period="2026-Q2",
        source_event_id="TUIK:TR_GDP:2026-Q2"
    )
    ev_q2_rescheduled = ev_q2_old.model_copy(update={"scheduled_at_istanbul": "2026-09-01T10:00:00+03:00"})

    prev_cal = RollingEconomicCalendar(
        schema_version="2.0", generated_at="2026-08-17T07:30:00+03:00", coverage_start="2026-08-17",
        coverage_end=end_d.isoformat(), timezone="Europe/Istanbul", status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=2)],
        events=[ev_2025, ev_q2_old]
    )
    curr_cal = RollingEconomicCalendar(
        schema_version="2.0", generated_at="2026-08-17T07:30:00+03:00", coverage_start="2026-08-17",
        coverage_end=end_d.isoformat(), timezone="Europe/Istanbul", status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=2)],
        events=[ev_2025, ev_q2_rescheduled]
    )

    diff = calculate_economic_diff(prev_cal, curr_cal, curr_cal)
    assert diff.unchanged_count == 1  # 2025 GDP unchanged
    assert diff.changed_count == 1    # 2026-Q2 GDP rescheduled (changed, not removed+added)
    assert diff.added_count == 0
    assert diff.removed_count == 0


def test_tuik_identity_stable_across_publication():
    from app.jobs.economic_calendar_refresh_job import calculate_economic_diff
    start_d = date(2026, 8, 17)
    end_d = start_d + timedelta(days=90)

    # 1. First refresh: id=0 (unpublished)
    ev_unpublished = EconomicEvent(
        name="Türkiye TÜFE", country="TR",
        scheduled_at="2026-09-03T10:00:00+03:00", scheduled_at_istanbul="2026-09-03T10:00:00+03:00",
        source="TÜİK", provider_id="TUIK", event_code="TR_CPI", reference_period="2026-08",
        source_event_id="TUIK:TR_CPI:2026-08", source_url="https://takvim.tuik.gov.tr"
    )

    # 2. Second refresh: id=58000 (published with direct link)
    ev_published = EconomicEvent(
        name="Türkiye TÜFE", country="TR",
        scheduled_at="2026-09-03T10:00:00+03:00", scheduled_at_istanbul="2026-09-03T10:00:00+03:00",
        source="TÜİK", provider_id="TUIK", event_code="TR_CPI", reference_period="2026-08",
        source_event_id="TUIK:TR_CPI:2026-08", source_url="https://data.tuik.gov.tr/Bulten/Index?p=58000"
    )

    # Both have the exact same stable source_event_id
    assert ev_unpublished.source_event_id == ev_published.source_event_id == "TUIK:TR_CPI:2026-08"

    prev_cal = RollingEconomicCalendar(
        schema_version="2.0", generated_at="2026-08-17T07:30:00+03:00", coverage_start="2026-08-17",
        coverage_end=end_d.isoformat(), timezone="Europe/Istanbul", status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1)],
        events=[ev_unpublished]
    )
    curr_cal = RollingEconomicCalendar(
        schema_version="2.0", generated_at="2026-08-17T07:30:00+03:00", coverage_start="2026-08-17",
        coverage_end=end_d.isoformat(), timezone="Europe/Istanbul", status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1)],
        events=[ev_published]
    )

    diff = calculate_economic_diff(prev_cal, curr_cal, curr_cal)
    # Same date => unchanged!
    assert diff.unchanged_count == 1
    assert diff.added_count == 0
    assert diff.removed_count == 0


@pytest.mark.asyncio
async def test_healthy_empty_with_only_out_of_scope_observed_candidates():
    from app.services.economic_calendar import _validate_and_scope_provider_events
    tz_tr = ZoneInfo("Europe/Istanbul")
    cov_start = date(2026, 8, 17)
    cov_end = date(2026, 11, 15)

    # Observed 50 candidates in feed, but 0 in coverage window, 0 malformed
    fetch_res = CalendarProviderFetchResult(
        events=[],
        warnings=[],
        structure_validated=True,
        observed_candidate_count=50,
        candidate_count=0,
        malformed_count=0
    )

    scoped, pr_res, warns = _validate_and_scope_provider_events(
        "TUIK", "TÜİK Veri Takvimi", fetch_res, cov_start, cov_end, tz_tr
    )

    assert len(scoped) == 0
    assert pr_res.status == "success"
    assert pr_res.structure_validated is True
    assert pr_res.observed_candidate_count == 50
    assert pr_res.candidate_count == 0
    assert pr_res.malformed_count == 0


@pytest.mark.asyncio
async def test_get_events_between_uses_typed_result():
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 9, 1, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 9, 30, 23, 59, tzinfo=tz_tr)

    p1 = MagicMock()
    p1.provider_id = "TUIK"
    p1.provider_name = "TÜİK"
    p1.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
        events=[
            EconomicEvent(
                name="Türkiye TÜFE", country="TR",
                scheduled_at="2026-09-03T10:00:00+03:00", scheduled_at_istanbul="2026-09-03T10:00:00+03:00",
                source="TÜİK", provider_id="TUIK", event_code="TR_CPI"
            )
        ],
        warnings=["Notice"],
        structure_validated=True,
        observed_candidate_count=1,
        candidate_count=1,
        malformed_count=0
    ))

    service = EconomicCalendarService(providers=[p1])
    events, warns = await service.get_events_between(start, end)

    assert len(events) == 1
    assert events[0].event_code == "TR_CPI"
    assert len(warns) == 1
