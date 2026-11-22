import json
from datetime import date, timedelta

from app.jobs.validate_economic_calendar_state import (
    validate_economic_calendar_state_source,
)
from app.models.economic_calendar import EconomicProviderResult, RollingEconomicCalendar
from app.models.market import EconomicEvent


def _create_valid_calendar(
    cov_start: str = "2026-08-17",
    status: str = "success"
) -> RollingEconomicCalendar:
    start_d = date.fromisoformat(cov_start)
    end_d = start_d + timedelta(days=90)
    return RollingEconomicCalendar(
        schema_version="2.0",
        generated_at=f"{cov_start}T07:30:00+03:00",
        coverage_start=cov_start,
        coverage_end=end_d.isoformat(),
        timezone="Europe/Istanbul",
        status=status,
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=1),
            EconomicProviderResult(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=1),
            EconomicProviderResult(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=1),
            EconomicProviderResult(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=1),
            EconomicProviderResult(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=1),
        ],
        events=[
            EconomicEvent(name="Türkiye TÜFE", country="TR", scheduled_at="2026-09-03T10:00:00+03:00", scheduled_at_istanbul="2026-09-03T10:00:00+03:00", source="TÜİK", provider_id="TUIK", event_code="TR_CPI", reference_period="2026-08"),
            EconomicEvent(name="TCMB Faiz Kararı", country="TR", scheduled_at="2026-09-10T14:00:00+03:00", scheduled_at_istanbul="2026-09-10T14:00:00+03:00", source="TCMB", provider_id="TCMB", event_code="TR_POLICY_RATE"),
            EconomicEvent(name="ABD NFP", country="US", scheduled_at="2026-09-04T15:30:00+03:00", scheduled_at_istanbul="2026-09-04T15:30:00+03:00", source="BLS", provider_id="BLS", event_code="US_NFP"),
            EconomicEvent(name="ABD GSYH", country="US", scheduled_at="2026-09-30T15:30:00+03:00", scheduled_at_istanbul="2026-09-30T15:30:00+03:00", source="BEA", provider_id="BEA", event_code="US_GDP"),
            EconomicEvent(name="Fed Faiz Kararı (FOMC)", country="US", scheduled_at="2026-09-16T21:00:00+03:00", scheduled_at_istanbul="2026-09-16T21:00:00+03:00", source="FED", provider_id="FED", event_code="US_FOMC"),
        ]
    )


def test_validator_passes_valid_cache(tmp_path):
    cache_path = tmp_path / "economic_calendar.json"
    cal = _create_valid_calendar()
    cache_path.write_text(cal.model_dump_json(indent=2), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path, require_current_coverage=False)
    assert is_valid is True
    assert len(errors) == 0


def test_validator_fails_missing_file(tmp_path):
    missing_path = tmp_path / "non_existent.json"
    is_valid, errors = validate_economic_calendar_state_source(missing_path)
    assert is_valid is False
    assert any("does not exist" in e for e in errors)


def test_validator_fails_invalid_json(tmp_path):
    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{bad-json:", encoding="utf-8")
    is_valid, errors = validate_economic_calendar_state_source(corrupt_path)
    assert is_valid is False
    assert any("Invalid JSON" in e for e in errors)


def test_validator_fails_status_failed(tmp_path):
    cache_path = tmp_path / "failed_cal.json"
    cal = _create_valid_calendar()
    cal_dict = json.loads(cal.model_dump_json())
    cal_dict["status"] = "failed"
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("cannot be 'failed'" in e for e in errors)


def test_validator_fails_partial_with_zero_events(tmp_path):
    cache_path = tmp_path / "partial_zero.json"
    cal = _create_valid_calendar(status="partial")
    cal_dict = json.loads(cal.model_dump_json())
    cal_dict["events"] = []
    for pr in cal_dict["provider_results"]:
        pr["event_count"] = 0
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("with 0 events is prohibited" in e for e in errors)


def test_validator_fails_missing_or_extra_provider(tmp_path):
    cache_path = tmp_path / "missing_provider.json"
    cal = _create_valid_calendar()
    cal_dict = json.loads(cal.model_dump_json())
    # Remove FED provider and its corresponding event so model validator doesn't fail on count mismatch
    cal_dict["provider_results"] = [pr for pr in cal_dict["provider_results"] if pr["provider_id"] != "FED"]
    cal_dict["events"] = [ev for ev in cal_dict["events"] if ev.get("provider_id") != "FED"]
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("Missing required providers" in e or "Pydantic schema validation failed" in e for e in errors)


def test_validator_fails_structure_validated_false_on_success(tmp_path):
    cache_path = tmp_path / "unvalidated_provider.json"
    cal = _create_valid_calendar()
    cal_dict = json.loads(cal.model_dump_json())
    cal_dict["provider_results"][0]["structure_validated"] = False
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("structure_validated is False" in e for e in errors)


def test_validator_fails_provider_count_mismatch(tmp_path):
    cache_path = tmp_path / "count_mismatch.json"
    cal = _create_valid_calendar()
    cal_dict = json.loads(cal.model_dump_json())
    cal_dict["provider_results"][0]["event_count"] = 99  # claims 99 but only 1 exists
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("count mismatch" in e or "Pydantic schema validation failed" in e for e in errors)


def test_validator_fails_event_outside_coverage(tmp_path):
    cache_path = tmp_path / "outside_coverage.json"
    cal = _create_valid_calendar()
    cal_dict = json.loads(cal.model_dump_json())
    # Modify one event to be in 2025
    cal_dict["events"][0]["scheduled_at_istanbul"] = "2025-01-01T10:00:00+03:00"
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("outside coverage" in e or "Pydantic schema validation failed" in e for e in errors)


def test_validator_accepts_same_timestamp_different_reference_periods(tmp_path):
    cache_path = tmp_path / "dual_gdp.json"
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
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK", status="success", structure_validated=True, event_count=2),
            EconomicProviderResult(provider_id="TCMB", provider_name="TCMB", status="success", structure_validated=True, event_count=0),
            EconomicProviderResult(provider_id="BLS", provider_name="BLS", status="success", structure_validated=True, event_count=0),
            EconomicProviderResult(provider_id="BEA", provider_name="BEA", status="success", structure_validated=True, event_count=0),
            EconomicProviderResult(provider_id="FED", provider_name="FED", status="success", structure_validated=True, event_count=0),
        ],
        events=[
            EconomicEvent(
                name="Gayrisafi Yurt İçi Hasıla",
                country="TR",
                scheduled_at="2026-08-31T10:00:00+03:00",
                scheduled_at_istanbul="2026-08-31T10:00:00+03:00",
                source="TÜİK",
                provider_id="TUIK",
                event_code="TR_GDP",
                reference_period="2025",
                source_event_id="TUIK:TR_GDP:2025"
            ),
            EconomicEvent(
                name="Gayrisafi Yurt İçi Hasıla",
                country="TR",
                scheduled_at="2026-08-31T10:00:00+03:00",
                scheduled_at_istanbul="2026-08-31T10:00:00+03:00",
                source="TÜİK",
                provider_id="TUIK",
                event_code="TR_GDP",
                reference_period="2026-Q2",
                source_event_id="TUIK:TR_GDP:2026-Q2"
            )
        ]
    )
    cache_path.write_text(cal.model_dump_json(indent=2), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path, require_current_coverage=False)
    assert is_valid is True
    assert len(errors) == 0


def test_validator_rejects_true_duplicate_same_identity_same_timestamp(tmp_path):
    cache_path = tmp_path / "true_duplicate.json"
    cal_dict = json.loads(_create_valid_calendar().model_dump_json())
    # Add exact duplicate of first event
    cal_dict["events"].append(dict(cal_dict["events"][0]))
    cal_dict["provider_results"][0]["event_count"] = 2
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("Duplicate" in e or "Pydantic schema validation failed" in e for e in errors)


def test_validator_rejects_fallback_stable_identity_conflict(tmp_path):
    cache_path = tmp_path / "fallback_conflict.json"
    cal_dict = json.loads(_create_valid_calendar().model_dump_json())
    # Two events with same event_code and reference_period but NO source_event_id at DIFFERENT timestamps
    cal_dict["events"][0] = {
        "name": "Türkiye TÜFE",
        "country": "TR",
        "scheduled_at": "2026-09-03T10:00:00+03:00",
        "scheduled_at_istanbul": "2026-09-03T10:00:00+03:00",
        "source": "TÜİK",
        "provider_id": "TUIK",
        "event_code": "TR_CPI",
        "reference_period": "2026-08",
        "source_event_id": None
    }
    cal_dict["events"].append({
        "name": "Türkiye TÜFE",
        "country": "TR",
        "scheduled_at": "2026-09-04T10:00:00+03:00",
        "scheduled_at_istanbul": "2026-09-04T10:00:00+03:00",
        "source": "TÜİK",
        "provider_id": "TUIK",
        "event_code": "TR_CPI",
        "reference_period": "2026-08",
        "source_event_id": None
    })
    cal_dict["provider_results"][0]["event_count"] = 2
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("Conflicting timestamps" in e for e in errors)


def test_validator_rejects_provider_total_vs_final_count_mismatch(tmp_path):
    cache_path = tmp_path / "total_mismatch.json"
    cal_dict = json.loads(_create_valid_calendar().model_dump_json())
    # Provider sums = 5, but remove one event from events array without updating provider_results
    cal_dict["events"].pop()
    cache_path.write_text(json.dumps(cal_dict), encoding="utf-8")

    is_valid, errors = validate_economic_calendar_state_source(cache_path)
    assert is_valid is False
    assert any("Total calendar event count" in e or "Pydantic schema validation failed" in e for e in errors)

