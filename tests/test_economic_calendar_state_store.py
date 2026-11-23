from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.models.economic_calendar import EconomicProviderResult, RollingEconomicCalendar
from app.models.market import EconomicEvent
from app.providers.calendar.base import CalendarProviderFetchResult
from app.services.economic_calendar import EconomicCalendarService
from app.services.economic_calendar_state_store import (
    SupabaseEconomicCalendarStateStore,
    merge_partial_economic_calendar,
)


def _build_test_calendar(
    generated_at: str = "2026-08-17T07:30:00+03:00",
    coverage_start: str = "2026-08-17",
    status: str = "success",
    provider_results = None,
    events = None,
    warnings = None
) -> RollingEconomicCalendar:
    d_start = date.fromisoformat(coverage_start)
    d_end = d_start + timedelta(days=90)
    prs = provider_results or []
    all_evs = events or []
    processed_prs = []
    for pr in prs:
        p_fresh = sum(1 for e in all_evs if (e.provider_id == pr.provider_id or (not e.provider_id and e.source == pr.provider_name)) and not e.carried_forward)
        p_carried = sum(1 for e in all_evs if (e.provider_id == pr.provider_id or (not e.provider_id and e.source == pr.provider_name)) and e.carried_forward)
        # Use calculated counts if not explicitly given
        e_cnt = pr.event_count if (pr.event_count + pr.carried_forward_event_count == p_fresh + p_carried) else p_fresh
        c_cnt = pr.carried_forward_event_count if (pr.event_count + pr.carried_forward_event_count == p_fresh + p_carried) else p_carried
        if pr.status == "success" and not pr.structure_validated:
            processed_prs.append(pr.model_copy(update={"structure_validated": True, "event_count": e_cnt, "carried_forward_event_count": c_cnt}))
        else:
            processed_prs.append(pr.model_copy(update={"event_count": e_cnt, "carried_forward_event_count": c_cnt}))

    return RollingEconomicCalendar(
        schema_version="2.0",
        generated_at=generated_at,
        coverage_start=d_start.isoformat(),
        coverage_end=d_end.isoformat(),
        timezone="Europe/Istanbul",
        status=status,
        provider_results=processed_prs,
        events=all_evs,
        warnings=warnings or []
    )

def test_supabase_store_is_stale_detection():
    cache = SupabaseEconomicCalendarStateStore()

    tz = ZoneInfo("Europe/Istanbul")
    gen_dt = datetime(2026, 8, 15, 7, 30, tzinfo=tz)
    now_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz)  # 48 hours later

    cal = _build_test_calendar(generated_at=gen_dt.isoformat(), coverage_start="2026-08-15")
    cache.stage(cal)

    assert cache.is_stale(now_dt, max_age_hours=36.0) is True
    recent_dt = datetime(2026, 8, 15, 17, 30, tzinfo=tz)
    assert cache.is_stale(recent_dt, max_age_hours=36.0) is False

def test_supabase_store_events_between_calculates_dynamic_days_remaining():
    cache = SupabaseEconomicCalendarStateStore()

    cal = _build_test_calendar(
        coverage_start="2026-08-17",
        events=[
            EconomicEvent(name="Ev1", country="TR", scheduled_at="", scheduled_at_istanbul="2026-08-17T10:00:00+03:00", source="TÜİK", provider_id="TUIK"),
            EconomicEvent(name="Ev2", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-19T15:30:00+03:00", source="BLS", provider_id="BLS"),
            EconomicEvent(name="Ev3", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-25T15:30:00+03:00", source="FED", provider_id="FED")
        ]
    )
    cache.stage(cal)

    today = date(2026, 8, 17)
    events, warns, loaded_cal = cache.events_between(date(2026, 8, 17), date(2026, 8, 20), as_of=today)

    assert len(events) == 2
    assert events[0].name == "Ev1"
    assert events[0].days_remaining == 0
    assert events[1].name == "Ev2"
    assert events[1].days_remaining == 2
    assert loaded_cal is not None

def test_merge_partial_economic_calendar_success_empty_provider_cleans_old_events():
    prev = _build_test_calendar(
        coverage_start="2026-08-17",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", event_count=1)],
        events=[
            EconomicEvent(name="Eski TÜİK", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-03T10:00:00+03:00", source="TÜİK", provider_id="TUIK")
        ]
    )
    curr = _build_test_calendar(
        coverage_start="2026-08-17",
        status="success",
        provider_results=[EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", event_count=0)],
        events=[]
    )

    merged = merge_partial_economic_calendar(prev, curr)
    assert len(merged.events) == 0
    assert merged.status == "success"

def test_merge_partial_economic_calendar_failed_provider_carries_forward_previous():
    prev = _build_test_calendar(
        coverage_start="2026-08-17",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", event_count=1),
            EconomicProviderResult(provider_id="BLS", provider_name="BLS", status="success", event_count=1)
        ],
        events=[
            EconomicEvent(name="TÜFE", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-03T10:00:00+03:00", source="TÜİK", provider_id="TUIK"),
            EconomicEvent(name="CPI", country="US", scheduled_at="", scheduled_at_istanbul="2026-09-10T15:30:00+03:00", source="BLS", provider_id="BLS")
        ]
    )

    curr = _build_test_calendar(
        coverage_start="2026-08-17",
        status="partial",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", event_count=1),
            EconomicProviderResult(provider_id="BLS", provider_name="BLS", status="failed", event_count=0, error="Network timeout")
        ],
        events=[
            EconomicEvent(name="TÜFE", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-04T10:00:00+03:00", source="TÜİK", provider_id="TUIK")
        ]
    )

    merged = merge_partial_economic_calendar(prev, curr)
    assert merged.status == "partial"
    assert len(merged.events) == 2

    tuik_ev = next(e for e in merged.events if e.provider_id == "TUIK")
    assert tuik_ev.scheduled_at_istanbul == "2026-09-04T10:00:00+03:00"
    assert tuik_ev.carried_forward is False

    bls_ev = next(e for e in merged.events if e.provider_id == "BLS")
    assert bls_ev.name == "CPI"
    assert bls_ev.carried_forward is True
    assert bls_ev.stale is True

def test_merge_partial_economic_calendar_does_not_carry_forward_aged_out_events():
    prev = _build_test_calendar(
        coverage_start="2026-08-16",
        provider_results=[EconomicProviderResult(provider_id="FED", provider_name="FED", status="success", event_count=1)],
        events=[
            EconomicEvent(name="Past FOMC", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-16T21:00:00+03:00", source="FED", provider_id="FED")
        ]
    )

    curr = _build_test_calendar(
        coverage_start="2026-08-17",
        status="failed",
        provider_results=[EconomicProviderResult(provider_id="FED", provider_name="FED", status="failed", event_count=0, error="Failed")],
        events=[]
    )

    merged = merge_partial_economic_calendar(prev, curr)
    assert len(merged.events) == 0

# ----------------------------------------------------
# DEFENSE-IN-DEPTH TEST MATRIX (TESTS A, B, C, D)
# ----------------------------------------------------

def test_defense_test_a_two_different_carried_forward_timestamps():
    # Previous corrupted cache has two carried forward events for same stable identity with different timestamps
    curr = _build_test_calendar(
        coverage_start="2026-08-17",
        status="partial",
        provider_results=[EconomicProviderResult(provider_id="BEA", provider_name="BEA", status="success", event_count=2)],
        events=[
            EconomicEvent(
                name="ABD GSYH", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-26T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second", carried_forward=True, stale=True
            ),
            EconomicEvent(
                name="ABD GSYH", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second", carried_forward=True, stale=True
            )
        ]
    )

    merged = merge_partial_economic_calendar(None, curr)
    # Ambiguous carried forward: must be excluded, warning generated, provider status partial
    second_events = [e for e in merged.events if e.source_event_id == "BEA:US_GDP:2026-Q2:second"]
    assert len(second_events) == 0
    bea_pr = next(pr for pr in merged.provider_results if pr.provider_id == "BEA")
    assert bea_pr.status == "partial"
    assert any("Ambiguous carried-forward events" in w for w in merged.warnings)

def test_defense_test_b_one_fresh_plus_one_carried():
    curr = _build_test_calendar(
        coverage_start="2026-08-17",
        status="partial",
        provider_results=[EconomicProviderResult(provider_id="BEA", provider_name="BEA", status="partial", event_count=1)],
        events=[
            # 1 carried from previous
            EconomicEvent(
                name="ABD GSYH", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-26T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second", carried_forward=True, stale=True
            ),
            # 1 fresh from today
            EconomicEvent(
                name="ABD GSYH", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second", carried_forward=False, stale=False
            )
        ]
    )

    merged = merge_partial_economic_calendar(None, curr)
    # Fresh event is authoritative
    second_events = [e for e in merged.events if e.source_event_id == "BEA:US_GDP:2026-Q2:second"]
    assert len(second_events) == 1
    assert second_events[0].scheduled_at_istanbul == "2026-08-27T15:30:00+03:00"
    assert second_events[0].carried_forward is False

def test_defense_test_c_two_different_fresh():
    curr = _build_test_calendar(
        coverage_start="2026-08-17",
        status="partial",
        provider_results=[EconomicProviderResult(provider_id="BEA", provider_name="BEA", status="success", event_count=2)],
        events=[
            EconomicEvent(
                name="ABD GSYH", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second", carried_forward=False
            ),
            EconomicEvent(
                name="ABD GSYH", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-28T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second", carried_forward=False
            )
        ]
    )

    merged = merge_partial_economic_calendar(None, curr)
    second_events = [e for e in merged.events if e.source_event_id == "BEA:US_GDP:2026-Q2:second"]
    assert len(second_events) == 0
    bea_pr = next(pr for pr in merged.provider_results if pr.provider_id == "BEA")
    assert bea_pr.status == "partial"
    assert any("Conflicting fresh events" in w for w in merged.warnings)

def test_defense_test_d_same_timestamp_duplicate():
    prev = _build_test_calendar(
        coverage_start="2026-08-17",
        status="success",
        provider_results=[EconomicProviderResult(provider_id="BEA", provider_name="BEA", status="success", event_count=1)],
        events=[
            EconomicEvent(
                name="ABD GSYH", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second", carried_forward=False
            )
        ]
    )
    curr = _build_test_calendar(
        coverage_start="2026-08-17",
        status="success",
        provider_results=[EconomicProviderResult(provider_id="BEA", provider_name="BEA", status="success", event_count=1)],
        events=[
            EconomicEvent(
                name="ABD GSYH", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
                source="BEA", provider_id="BEA", event_code="US_GDP", reference_period="2026-Q2",
                source_event_id="BEA:US_GDP:2026-Q2:second", carried_forward=False
            )
        ]
    )

    merged = merge_partial_economic_calendar(prev, curr)
    second_events = [e for e in merged.events if e.source_event_id == "BEA:US_GDP:2026-Q2:second"]
    assert len(second_events) == 1
    assert second_events[0].carried_forward is False

# ----------------------------------------------------
# SERVICE TO CACHE STRUCTURAL FAILURE INTEGRATION
# ----------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("pid,pname,event_name", [
    ("BLS", "U.S. Bureau of Labor Statistics Release Calendar", "ABD İstihdam Raporu (NFP)"),
    ("FED", "Federal Reserve FOMC Calendar", "Fed Faiz Kararı (FOMC)")
])
async def test_service_to_cache_structural_failure_preserves_previous(pid, pname, event_name):
    # Previous reliable cache had an event
    prev_cal = _build_test_calendar(
        coverage_start="2026-08-17",
        provider_results=[EconomicProviderResult(provider_id=pid, provider_name=pname, status="success", event_count=1)],
        events=[
            EconomicEvent(name=event_name, country="US", scheduled_at="", scheduled_at_istanbul="2026-09-10T15:30:00+03:00", source=pname, provider_id=pid)
        ]
    )

    # Current service fetch encounters generic invalid HTML
    mock_prov = MagicMock()
    mock_prov.provider_id = pid
    mock_prov.provider_name = pname
    mock_prov.fetch_events = AsyncMock(return_value=CalendarProviderFetchResult(
        events=[],
        warnings=[f"{pname} sayfa yapısı doğrulanamadı"],
        structure_validated=False,
        candidate_count=0,
        malformed_count=0
    ))

    service = EconomicCalendarService(providers=[mock_prov])
    tz_tr = ZoneInfo("Europe/Istanbul")
    curr_cal = await service.get_rolling_calendar(datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr))

    assert curr_cal.status == "failed"
    assert len(curr_cal.events) == 0

    # Merged with previous cache: previous event is preserved with carried_forward=True, stale=True
    merged = merge_partial_economic_calendar(prev_cal, curr_cal)
    assert len(merged.events) == 1
    assert merged.events[0].name == event_name
    assert merged.events[0].carried_forward is True
    assert merged.events[0].stale is True


# ----------------------------------------------------
# DECISION MATRIX & REGRESSION TESTS (RULES A, B, C, D)
# ----------------------------------------------------

def test_regression_four_failed_providers_and_zero_events_is_overall_failed():
    # Production incident scenario: 4 failed providers + 1 false success with 0 events
    curr_cal = _build_test_calendar(
        coverage_start="2026-08-17",
        status="failed",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="failed", structure_validated=False, event_count=0),
            EconomicProviderResult(provider_id="TCMB", provider_name="TCMB", status="failed", structure_validated=False, event_count=0),
            EconomicProviderResult(provider_id="BLS", provider_name="BLS", status="failed", structure_validated=False, event_count=0),
            EconomicProviderResult(provider_id="BEA", provider_name="BEA", status="failed", structure_validated=False, event_count=0),
            EconomicProviderResult(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=0),
        ],
        events=[]
    )

    # When no previous cache exists:
    merged = merge_partial_economic_calendar(None, curr_cal)
    assert merged.status == "failed"
    assert len(merged.events) == 0


def test_rule_b_partial_with_positive_fresh_events_is_partial():
    # At least one provider failed, but at least 1 fresh valid event exists
    curr_cal = _build_test_calendar(
        coverage_start="2026-08-17",
        status="partial",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="failed", structure_validated=False, event_count=0),
            EconomicProviderResult(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=1),
        ],
        events=[
            EconomicEvent(name="TCMB Faiz Kararı", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-10T14:00:00+03:00", source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE")
        ]
    )

    merged = merge_partial_economic_calendar(None, curr_cal)
    assert merged.status == "partial"
    assert len(merged.events) == 1


def test_rule_a_all_success_is_overall_success():
    curr_cal = _build_test_calendar(
        coverage_start="2026-08-17",
        status="success",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1),
            EconomicProviderResult(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=1),
        ],
        events=[
            EconomicEvent(name="Türkiye TÜFE", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-03T10:00:00+03:00", source="TÜİK", provider_id="TUIK", event_code="TR_CPI"),
            EconomicEvent(name="TCMB Faiz Kararı", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-10T14:00:00+03:00", source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE")
        ]
    )

    merged = merge_partial_economic_calendar(None, curr_cal)
    assert merged.status == "success"
    assert len(merged.events) == 2

