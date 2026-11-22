import logging
from datetime import date, datetime, timedelta
from typing import List, Optional, Set
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.earnings import (
    Nasdaq100EarningsCalendar,
    NasdaqEarningsCalendar,
    NasdaqEarningsEvent,
)
from app.providers.earnings.yahoo_provider import YahooEarningsProvider

logger = logging.getLogger(__name__)

SESSION_ORDER = {
    "pre_market": 0,
    "time_not_supplied": 1,
    "after_hours": 2
}


def build_nasdaq100_rolling_calendar(
    events: List[NasdaqEarningsEvent],
    warnings: List[str],
    coverage_start: date,
    coverage_end: date,
    generated_at: datetime,
    whitelist: Set[str],
) -> Nasdaq100EarningsCalendar:
    """Pure transformation helper to construct Nasdaq100EarningsCalendar (schema 2.0)."""
    cov_start_str = coverage_start.isoformat()
    cov_end_str = coverage_end.isoformat()

    filtered_events: List[NasdaqEarningsEvent] = [
        ev for ev in events
        if cov_start_str <= ev.earnings_date <= cov_end_str
        and ev.symbol.upper() in whitelist
    ]

    filtered_events.sort(
        key=lambda ev: (
            ev.earnings_date,
            SESSION_ORDER.get(ev.session, 1),
            ev.symbol
        )
    )

    status = "partial" if warnings else "success"

    return Nasdaq100EarningsCalendar(
        schema_version="2.0",
        generated_at=generated_at.isoformat(),
        coverage_start=cov_start_str,
        coverage_end=cov_end_str,
        market="NASDAQ100",
        status=status,
        provider_status=status,
        events=filtered_events,
        warnings=warnings
    )


class Nasdaq100EarningsService:
    def __init__(self, provider: Optional[YahooEarningsProvider] = None):
        self.provider = provider or YahooEarningsProvider()

    async def get_week_events(
        self,
        offset: int = 0,
        ref_date: Optional[datetime] = None
    ) -> NasdaqEarningsCalendar:
        tz = ZoneInfo(settings.TIMEZONE)
        if ref_date is None:
            ref_date = datetime.now(tz)
        elif ref_date.tzinfo is None:
            ref_date = ref_date.replace(tzinfo=tz)
        else:
            ref_date = ref_date.astimezone(tz)

        target_ref = ref_date + timedelta(weeks=offset)

        monday = target_ref - timedelta(days=target_ref.weekday())
        sunday = monday + timedelta(days=6)

        mon_str = monday.strftime("%Y-%m-%d")
        sun_str = sunday.strftime("%Y-%m-%d")

        try:
            raw_events, warnings = await self.provider.get_earnings_events(monday, sunday)
            provider_status = "success" if not warnings else "partial"

            _, nasdaq100_whitelist = self.provider._load_nasdaq100_symbols()

            week_events: List[NasdaqEarningsEvent] = [
                ev for ev in raw_events
                if mon_str <= ev.earnings_date <= sun_str and ev.symbol.upper() in nasdaq100_whitelist
            ]

            week_events.sort(
                key=lambda ev: (
                    ev.earnings_date,
                    SESSION_ORDER.get(ev.session, 1),
                    ev.symbol
                )
            )

            return NasdaqEarningsCalendar(
                schema_version="1.0",
                generated_at=datetime.now(tz).isoformat(),
                week_start=mon_str,
                week_end=sun_str,
                market="NASDAQ100",
                status="success",
                provider_status=provider_status,
                events=week_events,
                warnings=warnings
            )
        except Exception as e:
            logger.error(f"Error generating NASDAQ100 weekly earnings calendar: {e}", exc_info=True)
            return NasdaqEarningsCalendar(
                schema_version="1.0",
                generated_at=datetime.now(tz).isoformat(),
                week_start=mon_str,
                week_end=sun_str,
                market="NASDAQ100",
                status="failed",
                provider_status="failed",
                events=[],
                warnings=[f"Failed to fetch NASDAQ100 earnings: {str(e)}"]
            )

    async def get_rolling_calendar(
        self,
        as_of: Optional[datetime] = None
    ) -> Nasdaq100EarningsCalendar:
        """Construct rolling 91-calendar-day canonical NASDAQ100 earnings calendar (schema 2.0)."""
        tz = ZoneInfo(settings.TIMEZONE)
        if as_of is None:
            as_of_dt = datetime.now(tz)
        elif as_of.tzinfo is None:
            as_of_dt = as_of.replace(tzinfo=tz)
        else:
            as_of_dt = as_of.astimezone(tz)

        as_of_date = as_of_dt.date()
        coverage_start = as_of_date
        coverage_end = as_of_date + timedelta(days=90)  # inclusive 91 calendar days

        coverage_start_dt = datetime.combine(coverage_start, datetime.min.time(), tzinfo=tz)
        coverage_end_dt = datetime.combine(coverage_end, datetime.max.time(), tzinfo=tz)

        try:
            raw_events, warnings = await self.provider.get_earnings_events(coverage_start_dt, coverage_end_dt)
            _, whitelist = self.provider._load_nasdaq100_symbols()
            return build_nasdaq100_rolling_calendar(
                events=raw_events,
                warnings=warnings,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                generated_at=as_of_dt,
                whitelist=whitelist
            )
        except Exception as e:
            logger.error(f"Error generating NASDAQ100 rolling earnings calendar: {e}", exc_info=True)
            return Nasdaq100EarningsCalendar(
                schema_version="2.0",
                generated_at=as_of_dt.isoformat(),
                coverage_start=coverage_start.isoformat(),
                coverage_end=coverage_end.isoformat(),
                market="NASDAQ100",
                status="failed",
                provider_status="failed",
                events=[],
                warnings=[f"Failed to fetch NASDAQ100 earnings: {str(e)}"]
            )


nasdaq100_earnings_service = Nasdaq100EarningsService()
