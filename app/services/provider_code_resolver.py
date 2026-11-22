"""Canonical provider-code normalization used by database synchronization."""

from typing import Optional


def resolve_bist_provider_code(source: Optional[str]) -> Optional[str]:
    if not source:
        return None
    normalized = source.strip()
    if normalized in ("KAP", "KAP Financial Calendar"):
        return "KAP"
    if normalized in ("Official IR", "OFFICIAL_IR"):
        return "OFFICIAL_IR"
    return None


def resolve_economic_provider_code(
    provider_id: Optional[str], source: Optional[str]
) -> Optional[str]:
    for value in (provider_id, source):
        if not value:
            continue
        normalized = value.strip().upper()
        if normalized in ("TUIK", "TÜİK"):
            return "TUIK"
        if normalized in {"TCMB", "BLS", "BEA", "FED"}:
            return normalized
        if normalized == "FEDERAL RESERVE":
            return "FED"
    return None
