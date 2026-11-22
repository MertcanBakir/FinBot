import json

from app.models.market import EconomicEvent
from app.services.database_event_identity import (
    build_bist_earnings_identity,
    build_economic_event_identity,
    build_nasdaq_earnings_identity,
)


def test_bist_earnings_identity_normalization_and_stability():
    """Verify BIST earnings identity strips .IS and omits earnings_date."""
    key1 = build_bist_earnings_identity("AEFES.IS", "2026-01-01", "2026-03-31")
    parsed1 = json.loads(key1)

    assert parsed1 == ["v1", "BIST100", "AEFES", "2026-01-01", "2026-03-31"]

    # Changing symbol casing produces canonical upper case
    key2 = build_bist_earnings_identity("aefes", "2026-01-01", "2026-03-31")
    assert key1 == key2

    # Different period produces different identity
    key_q2 = build_bist_earnings_identity("AEFES", "2026-04-01", "2026-06-30")
    assert key1 != key_q2


def test_bist_earnings_identity_none_periods():
    """Verify BIST earnings identity handles None periods safely."""
    key = build_bist_earnings_identity("THYAO", None, None)
    parsed = json.loads(key)
    assert parsed == ["v1", "BIST100", "THYAO", None, None]


def test_nasdaq_earnings_identity_exact_occurrence():
    """Verify NASDAQ earnings identity preserves occurrence parameters."""
    key1 = build_nasdaq_earnings_identity("AAPL", "2026-10-29", "after_hours")
    parsed1 = json.loads(key1)
    assert parsed1 == ["v1", "NASDAQ100", "AAPL", "2026-10-29", "after_hours"]

    # Same symbol, different date -> distinct identity
    key2 = build_nasdaq_earnings_identity("AAPL", "2027-01-28", "after_hours")
    assert key1 != key2

    # Same symbol, same date, different session -> distinct identity
    key3 = build_nasdaq_earnings_identity("AAPL", "2026-10-29", "pre_market")
    assert key1 != key3


def test_economic_event_identity_stable_with_source_event_id():
    """Verify economic event with source_event_id produces stable identity key."""
    event = EconomicEvent(
        name="U.S. GDP Advance Estimate",
        country="US",
        scheduled_at="2026-08-27T15:30:00+03:00",
        scheduled_at_istanbul="2026-08-27T15:30:00+03:00",
        source="BEA",
        provider_id="BEA",
        source_event_id="BEA_GDP_2026_Q2_ADVANCE",
    )
    key = build_economic_event_identity(event)
    parsed = json.loads(key)

    assert parsed == ["v1", "ECONOMIC", "stable", "source_id", "BEA", "BEA_GDP_2026_Q2_ADVANCE"]


def test_economic_event_identity_stable_with_code_and_period():
    """Verify economic event with event_code and reference_period produces stable identity key."""
    event = EconomicEvent(
        name="TCMB PPK Faiz Kararı",
        country="TR",
        scheduled_at="2026-08-20T14:00:00+03:00",
        scheduled_at_istanbul="2026-08-20T14:00:00+03:00",
        source="TCMB",
        provider_id="TCMB",
        event_code="TR_POLICY_RATE",
        reference_period="2026-08",
    )
    key = build_economic_event_identity(event)
    parsed = json.loads(key)

    assert parsed == ["v1", "ECONOMIC", "stable", "code_period", "TCMB", "TR_POLICY_RATE", "2026-08"]


def test_economic_event_identity_distinguishes_same_timestamp_different_releases():
    """Verify TÜİK 2025 Annual GDP and 2026-Q2 Quarterly GDP at identical timestamp produce distinct keys."""
    same_timestamp = "2026-08-31T10:00:00+03:00"

    event_annual = EconomicEvent(
        name="Gayrisafi Yurt İçi Hasıla (Yıllık)",
        country="TR",
        scheduled_at=same_timestamp,
        scheduled_at_istanbul=same_timestamp,
        source="TUIK",
        provider_id="TUIK",
        event_code="TR_GDP",
        reference_period="2025",
    )

    event_quarterly = EconomicEvent(
        name="Dönemsel Gayrisafi Yurt İçi Hasıla (Çeyreklik)",
        country="TR",
        scheduled_at=same_timestamp,
        scheduled_at_istanbul=same_timestamp,
        source="TUIK",
        provider_id="TUIK",
        event_code="TR_GDP",
        reference_period="2026-Q2",
    )

    key_annual = build_economic_event_identity(event_annual)
    key_quarterly = build_economic_event_identity(event_quarterly)

    assert key_annual != key_quarterly
    assert "2025" in key_annual
    assert "2026-Q2" in key_quarterly


def test_economic_event_identity_fallback_occurrence():
    """Verify fallback occurrence key when neither source_event_id nor reference_period exists."""
    event = EconomicEvent(
        name="Special Briefing",
        country="TR",
        scheduled_at="2026-09-01T11:00:00+03:00",
        scheduled_at_istanbul="2026-09-01T11:00:00+03:00",
        source="TCMB",
        provider_id="TCMB",
    )
    key = build_economic_event_identity(event)
    parsed = json.loads(key)

    assert parsed[0] == "v1"
    assert parsed[1] == "ECONOMIC"
    assert parsed[2] == "occurrence"
    assert parsed[3] == "fallback_occurrence"
