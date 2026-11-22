import json
from typing import Optional

from app.models.market import EconomicEvent
from app.services.economic_event_identity import (
    economic_occurrence_key,
    economic_stable_identity,
)


def build_bist_earnings_identity(
    symbol: str,
    period_start: Optional[str],
    period_end: Optional[str],
) -> str:
    """Build deterministic, versioned identity key for BIST100 earnings events.

    Format: ["v1", "BIST100", symbol, period_start, period_end]
    Note: earnings_date is intentionally omitted so date updates do not alter record identity.
    """
    clean_sym = symbol.strip().upper().replace(".IS", "")
    p_start = period_start.strip() if period_start else None
    p_end = period_end.strip() if period_end else None
    payload = ["v1", "BIST100", clean_sym, p_start, p_end]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def build_nasdaq_earnings_identity(
    symbol: str,
    earnings_date: str,
    session: str,
) -> str:
    """Build deterministic, versioned occurrence identity key for NASDAQ100 earnings events.

    Format: ["v1", "NASDAQ100", symbol, earnings_date, session]
    """
    clean_sym = symbol.strip().upper()
    clean_date = earnings_date.strip()
    clean_session = session.strip().lower()
    payload = ["v1", "NASDAQ100", clean_sym, clean_date, clean_session]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def build_economic_event_identity(event: EconomicEvent) -> str:
    """Build deterministic, versioned identity key for Economic Calendar events.

    Uses economic_stable_identity() if available, otherwise falls back to economic_occurrence_key().
    Guarantees distinct events at identical timestamps (e.g. TR_GDP 2025 Annual vs 2026-Q2) produce distinct keys.
    """
    s_id = economic_stable_identity(event)
    if s_id is not None:
        payload = ["v1", "ECONOMIC", "stable", *s_id]
    else:
        occ = economic_occurrence_key(event)
        payload = ["v1", "ECONOMIC", "occurrence", *occ]

    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
