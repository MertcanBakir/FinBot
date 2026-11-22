import logging
from datetime import datetime, time, timedelta
from typing import List, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_connection
from app.models.earnings import WeeklyEarningsEvent
from app.models.market import EconomicEvent
from app.repositories.refresh_repository import RefreshRepository

logger = logging.getLogger(__name__)


class MorningCalendarService:
    """Service to fetch weekly economic and earnings calendar events for the Morning Job.

    Supabase PostgreSQL tables (economic_events, earnings_events) are the only
    runtime source of truth. Database failures are surfaced instead of serving
    an increasingly stale checked-in JSON snapshot.
    """

    def __init__(
        self,
        timezone_name: str = settings.TIMEZONE,
    ):
        self.tz = ZoneInfo(timezone_name)

    def get_weekly_calendar(
        self,
        as_of: datetime,
    ) -> Tuple[List[EconomicEvent], List[WeeklyEarningsEvent], List[str], str]:
        """Fetch weekly economic events and earnings events.

        Returns:
            (economic_events, earnings_events, warnings, source)
            source: 'database'
        """
        # Smart Week Window (Option A):
        # Mon-Thu (weekday 0-3): Current week (Mon-Sun)
        # Fri-Sun (weekday 4-6): Next week (Next Mon-Next Sun) to prepare investors for the upcoming week
        if as_of.weekday() >= 4:
            days_until_next_monday = 7 - as_of.weekday()
            monday = as_of.date() + timedelta(days=days_until_next_monday)
        else:
            monday = as_of.date() - timedelta(days=as_of.weekday())
        sunday = monday + timedelta(days=6)

        start_dt = datetime.combine(monday, time.min, tzinfo=self.tz)
        end_dt = datetime.combine(sunday, time.max, tzinfo=self.tz)

        warnings: List[str] = []

        if not settings.SUPABASE_DATABASE_URL:
            raise RuntimeError("Supabase database is not configured for the morning calendar.")

        try:
            with get_db_connection() as conn:
                econ_events = RefreshRepository.get_weekly_economic_events(conn, start_dt, end_dt)
                earn_events = RefreshRepository.get_weekly_earnings_events(conn, monday, sunday)
        except Exception as exc:
            logger.error(
                "Failed to query weekly calendar from Supabase (error_type=%s)",
                type(exc).__name__,
            )
            raise RuntimeError("Supabase calendar query failed.") from None

        logger.info(
            "Fetched %d economic events and %d earnings events from Supabase for week %s to %s",
            len(econ_events),
            len(earn_events),
            monday,
            sunday,
        )
        return econ_events, earn_events, warnings, "database"


morning_calendar_service = MorningCalendarService()
