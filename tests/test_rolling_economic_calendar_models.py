from datetime import date, timedelta

import pytest

from app.models.economic_calendar import EconomicProviderResult, RollingEconomicCalendar
from app.models.market import EconomicEvent


def test_rolling_economic_calendar_valid_90_day_coverage():
    start = date(2026, 8, 17)
    end = start + timedelta(days=90)  # 2026-11-15 (91 days inclusive)

    cal = RollingEconomicCalendar(
        schema_version="2.0",
        generated_at="2026-08-17T07:30:00+03:00",
        coverage_start=start.isoformat(),
        coverage_end=end.isoformat(),
        timezone="Europe/Istanbul",
        status="success",
        provider_results=[
            EconomicProviderResult(provider_id="TUIK", provider_name="TÜİK Veri Takvimi", status="success", event_count=1)
        ],
        events=[
            EconomicEvent(
                name="Türkiye TÜFE",
                country="TR",
                scheduled_at="2026-09-03T10:00:00+03:00",
                scheduled_at_istanbul="2026-09-03T10:00:00+03:00",
                source="TÜİK",
                provider_id="TUIK",
                event_code="TR_CPI"
            )
        ]
    )

    assert cal.schema_version == "2.0"
    assert (date.fromisoformat(cal.coverage_end) - date.fromisoformat(cal.coverage_start)).days == 90
    assert len(cal.events) == 1
    assert cal.events[0].provider_id == "TUIK"

def test_rolling_economic_calendar_invalid_delta_raises_value_error():
    start = date(2026, 8, 17)
    end = start + timedelta(days=89)  # only 89 days delta

    with pytest.raises(ValueError, match="Coverage window must be exactly 90 days"):
        RollingEconomicCalendar(
            schema_version="2.0",
            generated_at="2026-08-17T07:30:00+03:00",
            coverage_start=start.isoformat(),
            coverage_end=end.isoformat(),
            timezone="Europe/Istanbul",
            status="success"
        )

def test_rolling_economic_calendar_event_outside_coverage_raises():
    start = date(2026, 8, 17)
    end = start + timedelta(days=90)

    # Event date is 2026-08-10 (before coverage_start)
    with pytest.raises(ValueError, match="falls outside coverage range"):
        RollingEconomicCalendar(
            schema_version="2.0",
            generated_at="2026-08-17T07:30:00+03:00",
            coverage_start=start.isoformat(),
            coverage_end=end.isoformat(),
            timezone="Europe/Istanbul",
            status="success",
            events=[
                EconomicEvent(
                    name="Türkiye TÜFE",
                    country="TR",
                    scheduled_at="2026-08-10T10:00:00+03:00",
                    scheduled_at_istanbul="2026-08-10T10:00:00+03:00",
                    source="TÜİK",
                    provider_id="TUIK",
                    event_code="TR_CPI"
                )
            ]
        )

def test_rolling_economic_calendar_duplicate_event_raises():
    start = date(2026, 8, 17)
    end = start + timedelta(days=90)

    ev1 = EconomicEvent(
        name="Türkiye TÜFE",
        country="TR",
        scheduled_at="2026-09-03T10:00:00+03:00",
        scheduled_at_istanbul="2026-09-03T10:00:00+03:00",
        source="TÜİK",
        provider_id="TUIK",
        event_code="TR_CPI"
    )
    ev2 = ev1.model_copy()

    with pytest.raises(ValueError, match="Duplicate event found in calendar"):
        RollingEconomicCalendar(
            schema_version="2.0",
            generated_at="2026-08-17T07:30:00+03:00",
            coverage_start=start.isoformat(),
            coverage_end=end.isoformat(),
            timezone="Europe/Istanbul",
            status="success",
            events=[ev1, ev2]
        )
