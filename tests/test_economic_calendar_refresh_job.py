import os
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.jobs.economic_calendar_refresh_job import (
    STATE_WRITE_FAILED_WARNING,
    EconomicCalendarRefreshJob,
    calculate_economic_diff,
    exit_code_for_result,
    validate_smoke_quality_gate,
)
from app.models.economic_calendar import EconomicProviderResult, RollingEconomicCalendar
from app.models.economic_calendar_refresh import (
    EconomicCalendarRefreshJobResult,
    EconomicEventDiff,
    EconomicProviderRefreshOutcome,
)
from app.models.market import EconomicEvent
from app.services.economic_calendar_state_store import JsonEconomicCalendarStateStore
from app.services.economic_event_identity import economic_provider_key
from tests.conftest import create_mock_db_connection


@pytest.fixture(autouse=True)
def mock_job_db(monkeypatch):
    mock_conn = create_mock_db_connection()
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_conn
    monkeypatch.setattr("app.jobs.economic_calendar_refresh_job.get_db_connection", lambda: mock_ctx)


def _build_test_calendar(
    status="success",
    coverage_start="2026-08-17",
    generated_at="2026-08-17T07:30:00+03:00",
    provider_results=None,
    events=None,
    warnings=None
) -> RollingEconomicCalendar:
    d_start = date.fromisoformat(coverage_start)
    d_end = d_start + timedelta(days=90)
    prs = provider_results
    all_evs = events or []
    if prs is None:
        p_counts: dict[str, tuple[int, int]] = {}
        for e in all_evs:
            pk = economic_provider_key(e)
            fresh = 1 if not e.carried_forward else 0
            carried = 1 if e.carried_forward else 0
            curr_f, curr_c = p_counts.get(pk, (0, 0))
            p_counts[pk] = (curr_f + fresh, curr_c + carried)

        if p_counts:
            prs = [
                EconomicProviderResult(
                    provider_id=pk,
                    provider_name=pk,
                    status="success",
                    structure_validated=True,
                    event_count=cnt[0],
                    carried_forward_event_count=cnt[1]
                )
                for pk, cnt in p_counts.items()
            ]
        else:
            prs = []

    return RollingEconomicCalendar(
        schema_version="2.0",
        generated_at=generated_at,
        coverage_start=d_start.isoformat(),
        coverage_end=d_end.isoformat(),
        timezone="Europe/Istanbul",
        status=status,
        provider_results=prs,
        events=all_evs,
        warnings=warnings or []
    )

@pytest.mark.asyncio
async def test_job_full_failure_with_previous_cache_has_matching_carried_diff(tmp_path):
    state_source = str(tmp_path / "test_calendar.json")
    state_store = JsonEconomicCalendarStateStore(file_path=state_source)

    # Prepopulate previous valid state_store with 1 event in coverage
    prev_cal = _build_test_calendar(
        status="success",
        generated_at="2026-08-16T07:30:00+03:00",
        coverage_start="2026-08-16",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1)
        ],
        events=[
            EconomicEvent(name="Türkiye TÜFE", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-03T10:00:00+03:00", source="TÜİK", provider_id="TUIK")
        ]
    )
    state_store.save(prev_cal)
    with open(state_source, "rb") as f:
        prev_bytes = f.read()

    tz_tr = ZoneInfo("Europe/Istanbul")
    today_dt = datetime(2026, 8, 17, 7, 30, tzinfo=tz_tr)

    mock_service = MagicMock()
    # Today's fetch fails completely
    mock_service.get_rolling_calendar = AsyncMock(return_value=_build_test_calendar(
        status="failed",
        generated_at=today_dt.isoformat(),
        coverage_start="2026-08-17",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="failed", structure_validated=False, event_count=0, error="TUIK down")
        ],
        events=[]
    ))

    mock_email = MagicMock()
    mock_email.is_configured.return_value = False

    job = EconomicCalendarRefreshJob(service=mock_service, state_store=state_store, email_service=mock_email)
    res = await job.run(as_of=today_dt)

    assert res.status == "failed"
    assert res.sync_action == "preserved_previous"
    assert exit_code_for_result(res) == 1

    # Exact equality between provider outcome carried count and diff carried count
    tuik_out = next(p for p in res.provider_outcomes if p.provider_id == "TUIK")
    assert tuik_out.event_count == 0
    assert tuik_out.carried_forward_count == 1
    assert res.diff.carried_forward_count == 1
    assert res.diff.unchanged_count == 0
    assert res.diff.removed_count == 0
    assert res.diff.added_count == 0

    # Ensure previous state_store file on disk was preserved byte-identical
    with open(state_source, "rb") as f:
        final_bytes = f.read()
    assert final_bytes == prev_bytes

@pytest.mark.asyncio
async def test_job_save_failure_with_no_previous_cache_reports_zero_persisted(tmp_path):
    state_source = str(tmp_path / "test_calendar.json")
    state_store = JsonEconomicCalendarStateStore(file_path=state_source)

    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=_build_test_calendar(
        status="success",
        events=[
            EconomicEvent(name="Türkiye TÜFE", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-03T10:00:00+03:00", source="TÜİK", provider_id="TUIK")
        ]
    ))

    mock_email = MagicMock()
    mock_email.is_configured.return_value = False

    job = EconomicCalendarRefreshJob(service=mock_service, state_store=state_store, email_service=mock_email)

    with patch.object(state_store, "save", return_value=False):
        res = await job.run()

        assert res.status == "failed"
        assert res.sync_action == "write_failed"
        assert exit_code_for_result(res) == 1
        assert res.diff.final_event_count == 0
        assert res.diff.added_count == 0
        assert res.provider_outcomes[0].event_count == 1
        assert not os.path.exists(state_source)

@pytest.mark.asyncio
async def test_identical_bea_fixture_diff_yields_zero_changed():
    from app.providers.calendar.bea_calendar import BeaCalendarProvider
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "economic_calendar", "bea_calendar.html")
    with open(fixture_path, encoding="utf-8") as f:
        html = f.read()

    provider = BeaCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 1, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 10, 1, 0, 0, tzinfo=tz_tr)

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.status_code = 200
        res.text = html
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        events = fetch_res.events
        assert len(events) == 4

    prev_cal = _build_test_calendar(events=events)
    curr_cal = _build_test_calendar(events=events)

    diff = calculate_economic_diff(prev_cal, curr_cal, curr_cal)

    assert diff.changed_count == 0
    assert diff.added_count == 0
    assert diff.removed_count == 0
    assert diff.unchanged_count == len(events)

@pytest.mark.asyncio
async def test_bea_second_estimate_reschedule_detected_as_changed():
    from app.providers.calendar.bea_calendar import BeaCalendarProvider
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "economic_calendar", "bea_calendar.html")
    with open(fixture_path, encoding="utf-8") as f:
        html = f.read()

    provider = BeaCalendarProvider()
    tz_tr = ZoneInfo("Europe/Istanbul")
    start = datetime(2026, 8, 1, 0, 0, tzinfo=tz_tr)
    end = datetime(2026, 10, 1, 0, 0, tzinfo=tz_tr)

    with patch("httpx.AsyncClient.get") as mock_get:
        res = MagicMock()
        res.status_code = 200
        res.text = html
        mock_get.return_value = res

        fetch_res = await provider.fetch_events(start, end)
        prev_events = fetch_res.events

    # Reschedule only Second Estimate by 1 day
    curr_events = []
    for ev in prev_events:
        if ev.source_event_id == "BEA:US_GDP:2026-Q2:second":
            curr_events.append(ev.model_copy(update={"scheduled_at_istanbul": "2026-08-28T15:30:00+03:00"}))
        else:
            curr_events.append(ev)

    prev_cal = _build_test_calendar(events=prev_events)
    curr_cal = _build_test_calendar(events=curr_events)

    diff = calculate_economic_diff(prev_cal, curr_cal, curr_cal)

    assert diff.changed_count == 1
    assert diff.unchanged_count == len(prev_events) - 1
    assert diff.added_count == 0
    assert diff.removed_count == 0
    changed_detail = next(d for d in diff.details if d.change_type == "changed")
    assert changed_detail.source_event_id == "BEA:US_GDP:2026-Q2:second"


@pytest.mark.asyncio
async def test_job_dry_run_no_email_does_not_save_and_skips_email(tmp_path):
    state_source = str(tmp_path / "test_calendar.json")
    state_store = JsonEconomicCalendarStateStore(file_path=state_source)

    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=_build_test_calendar(
        status="success",
        events=[
            EconomicEvent(name="Türkiye TÜFE", country="TR", scheduled_at="", scheduled_at_istanbul="2026-09-03T10:00:00+03:00", source="TÜİK", provider_id="TUIK")
        ]
    ))

    mock_email = MagicMock()
    mock_email.is_configured.return_value = True

    job = EconomicCalendarRefreshJob(service=mock_service, state_store=state_store, email_service=mock_email, dry_run=True, no_email=True)
    res = await job.run()

    assert res.sync_action == "dry_run"
    assert res.email_sent is False
    assert res.email_status == "skipped_dry_run"
    assert not os.path.exists(state_source)
    mock_email.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_job_rule_c_empty_partial_fails_and_does_not_write_cache(tmp_path):
    state_source = str(tmp_path / "test_calendar.json")
    state_store = JsonEconomicCalendarStateStore(file_path=state_source)

    mock_service = MagicMock()
    # 4 providers failed, 1 returned 0 events
    mock_service.get_rolling_calendar = AsyncMock(return_value=_build_test_calendar(
        status="failed",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="failed", structure_validated=False, event_count=0),
            EconomicProviderResult(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=0),
        ],
        events=[]
    ))

    mock_email = MagicMock()
    mock_email.is_configured.return_value = False

    job = EconomicCalendarRefreshJob(service=mock_service, state_store=state_store, email_service=mock_email)
    res = await job.run()

    assert res.status == "failed"
    assert res.sync_action == "not_written"
    assert exit_code_for_result(res) == 1
    assert not os.path.exists(state_source)


def test_validate_smoke_quality_gate_all_success():
    res = EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status="success",
        sync_action="dry_run",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=5, final_event_count=5),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=1),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=1),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=1),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=1),
        ]
    )

    passed, errors = validate_smoke_quality_gate(res)
    assert passed is True
    assert len(errors) == 0


def test_validate_smoke_quality_gate_rejects_partial_or_failure():
    res = EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status="partial",
        sync_action="dry_run",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=4, final_event_count=4),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=1),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=1),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=1),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="FED", status="failed", structure_validated=False, event_count=0),
        ]
    )

    passed, errors = validate_smoke_quality_gate(res)
    assert passed is False
    assert any("FED" in e for e in errors)
    assert any("Overall job status" in e for e in errors)


def test_email_status_serialization_all_literal_values():
    valid_statuses = ["sent", "failed", "skipped_not_configured", "skipped_dry_run", "skipped_no_email"]
    for st in valid_statuses:
        res = EconomicCalendarRefreshJobResult(
            schema_version="2.0",
            generated_at="2026-08-17T07:30:00+03:00",
            coverage_start="2026-08-17",
            coverage_end="2026-11-15",
            status="success",
            sync_action="dry_run",
            state_source="data/generated/economic_calendar.json",
            diff=EconomicEventDiff(),
            email_status=st
        )
        json_data = res.model_dump_json()
        restored = EconomicCalendarRefreshJobResult.model_validate_json(json_data)
        assert restored.email_status == st


def test_smoke_gate_rejects_event_count_loss():
    # 1. Regression test: provider total 35 -> fetched/final 34 => Gate must fail!
    res_mismatch = EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status="success",
        sync_action="dry_run",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=34, final_event_count=34),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=3),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=6),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=2),
        ]
    )
    passed, errors = validate_smoke_quality_gate(res_mismatch)
    assert passed is False
    assert any("Provider fresh total mismatch" in e for e in errors)

    # 2. Fixed state: provider total 35 == fetched 35 == final 35 => Gate must pass!
    res_matched = EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status="success",
        sync_action="dry_run",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=35, final_event_count=35),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=3),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=6),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=2),
        ]
    )
    passed_ok, errors_ok = validate_smoke_quality_gate(res_matched)
    assert passed_ok is True
    assert len(errors_ok) == 0


def test_smoke_gate_accepts_healthy_empty_provider():
    # Authoritative healthy empty: FED has 0 events, but structure_validated is True and status is success
    res_healthy_empty = EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status="success",
        sync_action="dry_run",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=33, final_event_count=33),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=3),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=6),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=0),
        ]
    )
    passed, errors = validate_smoke_quality_gate(res_healthy_empty)
    assert passed is True
    assert len(errors) == 0


def test_smoke_gate_rejects_unvalidated_structure_even_if_success():
    res_invalid_struct = EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status="success",
        sync_action="dry_run",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=33, final_event_count=33),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=3),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=6),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="FED", status="success", structure_validated=False, event_count=0),
        ]
    )
    passed, errors = validate_smoke_quality_gate(res_invalid_struct)
    assert passed is False
    assert any("structure_validated is False" in e for e in errors)


def test_smoke_gate_rejects_failed_provider():
    res_failed_provider = EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status="success",
        sync_action="dry_run",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=33, final_event_count=33),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=3),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=12),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=6),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="FED", status="failed", structure_validated=False, event_count=0),
        ]
    )
    passed, errors = validate_smoke_quality_gate(res_failed_provider)
    assert passed is False
    assert any("status is 'failed'" in e for e in errors)


def test_smoke_gate_rejects_all_providers_zero_events():
    res_all_zero = EconomicCalendarRefreshJobResult(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start="2026-08-17",
        coverage_end="2026-11-15",
        status="success",
        sync_action="dry_run",
        state_source="data/generated/economic_calendar.json",
        diff=EconomicEventDiff(fetched_event_count=0, final_event_count=0),
        provider_outcomes=[
            EconomicProviderRefreshOutcome(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=0),
            EconomicProviderRefreshOutcome(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=0),
            EconomicProviderRefreshOutcome(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=0),
            EconomicProviderRefreshOutcome(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=0),
            EconomicProviderRefreshOutcome(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=0),
        ]
    )
    passed, errors = validate_smoke_quality_gate(res_all_zero)
    assert passed is False
    assert any("Total fresh event count is 0" in e for e in errors)


# ====================================================
# SERVICE EXCEPTION ISOLATION & PARTIAL MERGE DEFENSE
# ====================================================

@pytest.mark.asyncio
async def test_service_exception_with_previous_preserves_cache_and_reports_failure(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    prev_cal = _build_test_calendar(
        status="success",
        events=[
            EconomicEvent(
                name="TCMB Faiz Kararı", country="TR",
                scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
                source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
                source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
            )
        ]
    )
    cache_path.write_text(prev_cal.model_dump_json(indent=2), encoding="utf-8")
    initial_bytes = cache_path.read_bytes()

    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    mock_service = MagicMock()
    mock_service.providers = []
    mock_service.get_rolling_calendar = AsyncMock(side_effect=RuntimeError("Service network explosion"))

    job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
    res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

    assert res.status == "failed"
    assert res.sync_action == "preserved_previous"
    assert exit_code_for_result(res) == 1
    # Byte-identical assertion
    assert cache_path.read_bytes() == initial_bytes
    # Carried forward count correctly reported
    assert res.diff.carried_forward_count == 1
    # Fallback provider outcomes
    assert len(res.provider_outcomes) == 5
    assert all(po.status == "failed" for po in res.provider_outcomes)
    assert any("RuntimeError" in po.error for po in res.provider_outcomes if po.error)


@pytest.mark.asyncio
async def test_service_exception_without_previous_writes_nothing(tmp_path):
    cache_path = tmp_path / "non_existent_calendar.json"
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    mock_service = MagicMock()
    mock_service.providers = []
    mock_service.get_rolling_calendar = AsyncMock(side_effect=ValueError("Fatal parsing failure"))

    job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
    res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

    assert res.status == "failed"
    assert res.sync_action == "not_written"
    assert not cache_path.exists()
    assert exit_code_for_result(res) == 1
    assert any("ValueError" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_service_exception_sends_operational_failure_email_when_configured(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    mock_service = MagicMock()
    mock_service.providers = []
    mock_service.get_rolling_calendar = AsyncMock(side_effect=RuntimeError("Service fatal crash"))

    mock_email = MagicMock()
    mock_email.is_configured = MagicMock(return_value=True)
    mock_email.send_email = MagicMock(return_value=True)

    job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, email_service=mock_email, no_email=False)
    res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

    assert res.status == "failed"
    assert res.email_sent is True
    assert res.email_status == "sent"
    assert mock_email.send_email.call_count == 1


@pytest.mark.asyncio
async def test_service_exception_dry_run_never_sends_email_or_writes_cache(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    mock_service = MagicMock()
    mock_service.providers = []
    mock_service.get_rolling_calendar = AsyncMock(side_effect=RuntimeError("Dry run crash"))

    mock_email = MagicMock()
    mock_email.is_configured = MagicMock(return_value=True)
    mock_email.send_email = MagicMock(return_value=True)

    job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, email_service=mock_email, dry_run=True, no_email=False)
    res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

    assert res.status == "failed"
    assert res.sync_action == "dry_run"
    assert not cache_path.exists()
    assert res.email_sent is False
    assert res.email_status == "skipped_dry_run"
    assert mock_email.send_email.call_count == 0


@pytest.mark.asyncio
async def test_partial_failed_merge_preserves_previous_byte_identical(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    prev_cal = _build_test_calendar(
        status="success",
        events=[
            EconomicEvent(
                name="TCMB Faiz Kararı", country="TR",
                scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
                source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
                source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
            )
        ]
    )
    cache_path.write_text(prev_cal.model_dump_json(indent=2), encoding="utf-8")
    initial_bytes = cache_path.read_bytes()

    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    # Mock current partial calendar
    curr_cal = _build_test_calendar(
        status="partial",
        events=[
            EconomicEvent(
                name="Türkiye TÜFE", country="TR",
                scheduled_at="2026-09-03T10:00:00+03:00", scheduled_at_istanbul="2026-09-03T10:00:00+03:00",
                source="TÜİK", provider_id="TUIK", event_code="TR_CPI", reference_period="2026-08",
                source_event_id="TUIK:TR_CPI:2026-08"
            )
        ]
    )

    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    # Force merge_partial_economic_calendar to return a failed calendar
    failed_merged = prev_cal.model_copy(update={"status": "failed", "events": []})
    with patch("app.jobs.economic_calendar_refresh_job.merge_partial_economic_calendar", return_value=failed_merged):
        job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
        res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

        assert res.status == "failed"
        assert res.sync_action == "preserved_previous"
        assert exit_code_for_result(res) == 1
        assert cache_path.read_bytes() == initial_bytes
        assert any("Partial economic calendar merge produced no usable calendar" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_partial_empty_merge_preserves_previous_byte_identical(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    prev_cal = _build_test_calendar(
        status="success",
        events=[
            EconomicEvent(
                name="TCMB Faiz Kararı", country="TR",
                scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
                source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
                source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
            )
        ]
    )
    cache_path.write_text(prev_cal.model_dump_json(indent=2), encoding="utf-8")
    initial_bytes = cache_path.read_bytes()

    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    curr_cal = _build_test_calendar(status="partial", events=[])
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    empty_merged = prev_cal.model_copy(update={"status": "partial", "events": []})
    with patch("app.jobs.economic_calendar_refresh_job.merge_partial_economic_calendar", return_value=empty_merged):
        job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
        res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

        assert res.status == "failed"
        assert res.sync_action == "preserved_previous"
        assert cache_path.read_bytes() == initial_bytes


@pytest.mark.asyncio
async def test_partial_failed_merge_does_not_call_cache_save(tmp_path):
    mock_cache = MagicMock()
    mock_cache.file_path = "data/generated/economic_calendar.json"
    prev_cal = _build_test_calendar(
        status="success",
        events=[
            EconomicEvent(
                name="TCMB Faiz Kararı", country="TR",
                scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
                source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
                source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
            )
        ]
    )
    mock_cache.load = MagicMock(return_value=prev_cal)
    mock_cache.save = MagicMock()

    curr_cal = _build_test_calendar(status="partial", events=[])
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    failed_merged = prev_cal.model_copy(update={"status": "failed", "events": []})
    with patch("app.jobs.economic_calendar_refresh_job.merge_partial_economic_calendar", return_value=failed_merged):
        job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
        res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

        assert res.status == "failed"
        assert res.sync_action == "preserved_previous"
        mock_cache.save.assert_not_called()


@pytest.mark.asyncio
async def test_partial_failed_merge_reports_carried_forward_counts(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    prev_cal = _build_test_calendar(
        status="success",
        events=[
            EconomicEvent(
                name="TCMB Faiz Kararı", country="TR",
                scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
                source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
                source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
            )
        ]
    )
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    curr_cal = _build_test_calendar(status="partial", events=[])
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    failed_merged = prev_cal.model_copy(update={"status": "failed", "events": []})
    with patch("app.jobs.economic_calendar_refresh_job.merge_partial_economic_calendar", return_value=failed_merged):
        with patch.object(mock_cache, "load", return_value=prev_cal):
            job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
            res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

            assert res.status == "failed"
            assert res.sync_action == "preserved_previous"
            assert res.diff.carried_forward_count == 1
            tcmb_po = next((po for po in res.provider_outcomes if po.provider_id == "TCMB"), None)
            if tcmb_po:
                assert tcmb_po.carried_forward_count == 1


@pytest.mark.asyncio
async def test_partial_without_previous_and_zero_events_writes_nothing(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    curr_cal = _build_test_calendar(status="partial", events=[])
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
    res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

    assert res.status == "failed"
    assert res.sync_action == "not_written"
    assert not cache_path.exists()
    assert exit_code_for_result(res) == 1


@pytest.mark.asyncio
async def test_partial_without_previous_and_usable_events_can_write_initial_partial(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    usable_ev = EconomicEvent(
        name="TCMB Faiz Kararı", country="TR",
        scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
        source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
        source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
    )
    curr_cal = _build_test_calendar(status="partial", events=[usable_ev])
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
    res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

    assert res.status == "partial"
    assert res.sync_action == "initial_partial_written"
    assert cache_path.exists()
    assert exit_code_for_result(res) == 0


@pytest.mark.asyncio
async def test_save_failed_success_no_previous_writes_nothing(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    usable_ev = EconomicEvent(
        name="TCMB Faiz Kararı", country="TR",
        scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
        source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
        source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
    )
    curr_cal = _build_test_calendar(status="success", events=[usable_ev])
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    with patch.object(mock_cache, "save", return_value=False):
        job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
        res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

        assert res.status == "failed"
        assert res.sync_action == "write_failed"
        assert not cache_path.exists()
        assert exit_code_for_result(res) == 1
        assert res.warnings.count(STATE_WRITE_FAILED_WARNING) == 1


@pytest.mark.asyncio
async def test_save_failed_success_with_previous_preserves_cache(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    prev_cal = _build_test_calendar(
        status="success",
        events=[
            EconomicEvent(
                name="TCMB Faiz Kararı", country="TR",
                scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
                source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
                source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
            )
        ]
    )
    cache_path.write_text(prev_cal.model_dump_json(indent=2), encoding="utf-8")
    initial_bytes = cache_path.read_bytes()

    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    curr_cal = _build_test_calendar(status="success", events=[])
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    with patch.object(mock_cache, "save", return_value=False):
        job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
        res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

        assert res.status == "failed"
        assert res.sync_action == "write_failed"
        assert cache_path.read_bytes() == initial_bytes
        assert exit_code_for_result(res) == 1
        assert res.warnings.count(STATE_WRITE_FAILED_WARNING) == 1


@pytest.mark.asyncio
async def test_save_failed_partial_with_previous_preserves_cache(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    prev_cal = _build_test_calendar(
        status="success",
        events=[
            EconomicEvent(
                name="TCMB Faiz Kararı", country="TR",
                scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
                source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
                source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
            )
        ]
    )
    cache_path.write_text(prev_cal.model_dump_json(indent=2), encoding="utf-8")
    initial_bytes = cache_path.read_bytes()

    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    curr_cal = _build_test_calendar(
        status="partial",
        events=[
            EconomicEvent(
                name="Türkiye TÜFE", country="TR",
                scheduled_at="2026-09-03T10:00:00+03:00", scheduled_at_istanbul="2026-09-03T10:00:00+03:00",
                source="TÜİK", provider_id="TUIK", event_code="TR_CPI", reference_period="2026-08",
                source_event_id="TUIK:TR_CPI:2026-08"
            )
        ]
    )
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    with patch.object(mock_cache, "save", return_value=False):
        job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
        res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

        assert res.status == "failed"
        assert res.sync_action == "write_failed"
        assert cache_path.read_bytes() == initial_bytes
        assert exit_code_for_result(res) == 1
        assert res.warnings.count(STATE_WRITE_FAILED_WARNING) == 1


@pytest.mark.asyncio
async def test_save_failed_partial_without_previous_writes_nothing(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    mock_cache = JsonEconomicCalendarStateStore(file_path=str(cache_path))
    usable_ev = EconomicEvent(
        name="TCMB Faiz Kararı", country="TR",
        scheduled_at="2026-08-20T14:00:00+03:00", scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
        source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE", reference_period="2026-08",
        source_event_id="TCMB:TR_POLICY_RATE:2026-N08"
    )
    curr_cal = _build_test_calendar(status="partial", events=[usable_ev])
    mock_service = MagicMock()
    mock_service.get_rolling_calendar = AsyncMock(return_value=curr_cal)

    with patch.object(mock_cache, "save", return_value=False):
        job = EconomicCalendarRefreshJob(state_store=mock_cache, service=mock_service, no_email=True)
        res = await job.run(as_of=datetime(2026, 8, 17, 7, 30, tzinfo=ZoneInfo("Europe/Istanbul")))

        assert res.status == "failed"
        assert res.sync_action == "write_failed"
        assert not cache_path.exists()
        assert exit_code_for_result(res) == 1
        assert res.warnings.count(STATE_WRITE_FAILED_WARNING) == 1
