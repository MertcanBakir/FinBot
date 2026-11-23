from typing import Optional

from app.models.earnings import Bist100EarningsCalendar, Nasdaq100EarningsCalendar
from app.models.economic_calendar import RollingEconomicCalendar
from app.services.calendar_state_store import merge_bist_partial_calendar


class MemoryCalendarStateStore:
    """In-memory test double matching the production earnings staging contract."""

    state_source = "supabase://earnings_events"

    def __init__(self) -> None:
        self._bist: Optional[Bist100EarningsCalendar] = None
        self._nasdaq: Optional[Nasdaq100EarningsCalendar] = None

    def load_bist(self) -> Optional[Bist100EarningsCalendar]:
        return self._bist

    def load_nasdaq(self) -> Optional[Nasdaq100EarningsCalendar]:
        return self._nasdaq

    def stage_bist(self, calendar: Bist100EarningsCalendar) -> Optional[Bist100EarningsCalendar]:
        if calendar.status == "failed":
            return None
        self._bist = (
            merge_bist_partial_calendar(self._bist, calendar)
            if calendar.status == "partial" and self._bist is not None
            else calendar.model_copy(deep=True)
        )
        return self._bist

    def stage_nasdaq(self, calendar: Nasdaq100EarningsCalendar) -> Optional[Nasdaq100EarningsCalendar]:
        if calendar.status == "failed":
            return None
        if calendar.status == "partial" and self._nasdaq is not None:
            return None
        self._nasdaq = calendar.model_copy(deep=True)
        return self._nasdaq


class MemoryEconomicCalendarStateStore:
    """In-memory test double matching the production economic staging contract."""

    state_source = "supabase://economic_events"

    def __init__(self) -> None:
        self._pending: Optional[RollingEconomicCalendar] = None

    def load(self) -> Optional[RollingEconomicCalendar]:
        return self._pending

    def stage(self, calendar: RollingEconomicCalendar) -> RollingEconomicCalendar:
        self._pending = calendar.model_copy(deep=True)
        return self._pending
