from abc import ABC, abstractmethod
from datetime import datetime
from typing import List

from pydantic import BaseModel, Field

from app.models.market import EconomicEvent


class CalendarProviderFetchResult(BaseModel):
    events: List[EconomicEvent] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    structure_validated: bool = False
    observed_candidate_count: int = 0
    candidate_count: int = 0
    malformed_count: int = 0


class BaseCalendarProvider(ABC):
    provider_id: str = "UNKNOWN"
    provider_name: str = "Unknown Calendar Provider"

    @abstractmethod
    async def fetch_events(self, start: datetime, end: datetime) -> CalendarProviderFetchResult:
        """
        Fetch economic calendar events occurring between start and end (inclusive)
        in Europe/Istanbul timezone.
        Returns a structured, type-safe CalendarProviderFetchResult.
        """
        pass
