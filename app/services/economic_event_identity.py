from datetime import datetime
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo

from app.models.market import EconomicEvent

TZ_ISTANBUL = ZoneInfo("Europe/Istanbul")


def canonical_istanbul_iso(ts_str: Optional[str]) -> str:
    """
    Parse timestamp string and convert to canonical Europe/Istanbul ISO string.
    If already ISO with timezone or naive, normalizes to Europe/Istanbul.
    """
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_ISTANBUL)
        else:
            dt = dt.astimezone(TZ_ISTANBUL)
        return dt.isoformat()
    except Exception:
        return ts_str.strip()


def economic_provider_key(event: EconomicEvent) -> str:
    """
    Returns the canonical provider key for an event.
    """
    return event.provider_id or event.source or "UNKNOWN"


def economic_stable_identity(event: EconomicEvent) -> Optional[Tuple[str, ...]]:
    """
    Extracts the unique logical lifecycle identity of an economic event.
    Events with the same stable identity represent the same economic publication
    (e.g., BEA Q2 GDP Second Estimate, TÜİK 2025 Annual GDP, FOMC Sept 2026 meeting).
    
    Priority:
    1. ("source_id", provider_key, source_event_id) if source_event_id is present.
    2. ("code_period", provider_key, event_code, reference_period) if event_code and reference_period exist.
    3. None if no stable identity can be determined.
    """
    p_key = economic_provider_key(event)
    if event.source_event_id:
        return ("source_id", p_key, event.source_event_id)
    if event.event_code and event.reference_period:
        return ("code_period", p_key, event.event_code, event.reference_period)
    return None


def economic_occurrence_key(event: EconomicEvent) -> Tuple[Any, ...]:
    """
    Extracts the occurrence key (identity + scheduled release time).
    Two events with the same occurrence key represent the exact same occurrence
    (true duplicate) and must be deduplicated.
    
    Two events with different reference periods or different stable identities
    at the EXACT SAME timestamp (e.g. TR_GDP 2025 Annual and TR_GDP 2026-Q2 Quarterly)
    will produce DIFFERENT occurrence keys and are thus preserved simultaneously.
    """
    p_key = economic_provider_key(event)
    c_ts = canonical_istanbul_iso(event.scheduled_at_istanbul)
    s_id = economic_stable_identity(event)

    if s_id is not None:
        return ("stable_occurrence", *s_id, c_ts)

    # Fallback for events without source_event_id or reference_period
    norm_name = event.name.strip().upper() if event.name else ""
    return (
        "fallback_occurrence",
        p_key,
        event.event_code or norm_name,
        event.reference_period or "",
        c_ts,
        event.country
    )
