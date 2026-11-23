from typing import Optional

from pydantic import BaseModel


class EconomicEvent(BaseModel):
    name: str
    country: str
    scheduled_at: str
    scheduled_at_istanbul: str
    source: str
    days_remaining: Optional[int] = None

    provider_id: Optional[str] = None
    event_code: Optional[str] = None
    reference_period: Optional[str] = None
    source_event_id: Optional[str] = None
    source_url: Optional[str] = None

    carried_forward: bool = False
    stale: bool = False
