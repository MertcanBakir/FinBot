"""Supabase-backed calendar snapshots used by APIs and refresh jobs.

Rolling calendar state lives in PostgreSQL.  The JSON calendar classes remain
available for tests and the explicit BIST fiscal-period archive, but production
read paths must not depend on checked-in rolling JSON files.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from psycopg import Connection

from app.core.config import settings
from app.core.database import get_db_connection
from app.core.job_names import (
    JOB_NAME_EARNINGS_REFRESH,
    JOB_NAME_ECONOMIC_CALENDAR_REFRESH,
)
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistEarningsEvent,
    Nasdaq100EarningsCalendar,
    NasdaqEarningsEvent,
)
from app.models.economic_calendar import RollingEconomicCalendar
from app.repositories.refresh_repository import RefreshRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RefreshState:
    generated_at: datetime
    coverage_start: date
    coverage_end: date
    status: str


class DatabaseCalendarReader:
    """Reconstruct canonical rolling calendar models from Supabase tables."""

    @staticmethod
    def _latest_state(conn: Connection, job_name: str) -> Optional[_RefreshState]:
        query = """
            SELECT
              COALESCE(finished_at, started_at),
              coverage_start,
              coverage_end,
              status
            FROM refresh_runs
            WHERE job_name = %s
              AND coverage_start IS NOT NULL
              AND coverage_end IS NOT NULL
              AND status IN ('success', 'partial')
            ORDER BY COALESCE(finished_at, started_at) DESC
            LIMIT 1;
        """
        with conn.cursor() as cur:
            cur.execute(query, (job_name,))
            row = cur.fetchone()
        if not row:
            return None
        return _RefreshState(
            generated_at=row[0],
            coverage_start=row[1],
            coverage_end=row[2],
            status=row[3],
        )

    @staticmethod
    def _safe_status(value: str) -> str:
        return value if value in {"success", "partial"} else "partial"

    def load_bist(self) -> Optional[Bist100EarningsCalendar]:
        if not settings.SUPABASE_DATABASE_URL:
            return None
        try:
            with get_db_connection() as conn:
                state = self._latest_state(conn, JOB_NAME_EARNINGS_REFRESH)
                if state is None:
                    return None
                query = """
                    SELECT
                      i.symbol, i.company_name, ee.fiscal_year, ee.fiscal_period,
                      ee.earnings_date, ee.period_start, ee.period_end,
                      ee.publish_date, ee.date_type, p.code, ee.source_provider,
                      ee.source_type, ee.source_event_id, ee.disclosure_index,
                      ee.source_url
                    FROM earnings_events ee
                    JOIN instruments i ON i.id = ee.instrument_id
                    LEFT JOIN data_providers p ON p.id = ee.provider_id
                    WHERE ee.market = 'BIST100'
                      AND ee.is_active = true
                      AND ee.earnings_date BETWEEN %s AND %s
                    ORDER BY ee.earnings_date, i.symbol;
                """
                with conn.cursor() as cur:
                    cur.execute(query, (state.coverage_start, state.coverage_end))
                    rows = cur.fetchall()

            events = []
            for row in rows:
                provider_code = str(row[9] or "").upper()
                source = "KAP" if provider_code == "KAP" else "Official IR"
                events.append(
                    BistEarningsEvent(
                        symbol=str(row[0]).strip().upper(),
                        company_name=str(row[1]).strip(),
                        fiscal_year=int(row[2]) if row[2] is not None else None,
                        fiscal_period=str(row[3]) if row[3] is not None else None,
                        earnings_date=str(row[4])[:10],
                        period_start=str(row[5])[:10] if row[5] else None,
                        period_end=str(row[6])[:10] if row[6] else None,
                        publish_date=str(row[7])[:10] if row[7] else None,
                        date_type=str(row[8] or "company_announced"),
                        source=source,
                        source_provider=str(row[10]) if row[10] else None,
                        source_type=str(row[11] or "financial_calendar"),
                        source_event_id=str(row[12]) if row[12] else None,
                        disclosure_index=int(row[13]) if row[13] is not None else None,
                        source_url=str(row[14]) if row[14] else None,
                    )
                )

            status = self._safe_status(state.status)
            return Bist100EarningsCalendar(
                generated_at=state.generated_at.isoformat(),
                coverage_start=state.coverage_start.isoformat(),
                coverage_end=state.coverage_end.isoformat(),
                status=status,
                provider_status=status,
                events=events,
            )
        except Exception as exc:
            logger.error("Supabase BIST calendar read failed (error_type=%s)", type(exc).__name__)
            return None

    def load_nasdaq(self) -> Optional[Nasdaq100EarningsCalendar]:
        if not settings.SUPABASE_DATABASE_URL:
            return None
        try:
            with get_db_connection() as conn:
                state = self._latest_state(conn, JOB_NAME_EARNINGS_REFRESH)
                if state is None:
                    return None
                query = """
                    SELECT i.symbol, i.company_name, ee.earnings_date, ee.session
                    FROM earnings_events ee
                    JOIN instruments i ON i.id = ee.instrument_id
                    WHERE ee.market = 'NASDAQ100'
                      AND ee.is_active = true
                      AND ee.earnings_date BETWEEN %s AND %s
                    ORDER BY ee.earnings_date, i.symbol;
                """
                with conn.cursor() as cur:
                    cur.execute(query, (state.coverage_start, state.coverage_end))
                    rows = cur.fetchall()

            labels = {
                "pre_market": "Pre-Market",
                "after_hours": "After Hours",
                "time_not_supplied": "Time Not Supplied",
            }
            events = []
            for row in rows:
                session = str(row[3] or "time_not_supplied")
                events.append(
                    NasdaqEarningsEvent(
                        symbol=str(row[0]).strip().upper(),
                        company_name=str(row[1]).strip(),
                        earnings_date=str(row[2])[:10],
                        session=session,
                        session_label=labels.get(session, "Time Not Supplied"),
                    )
                )

            status = self._safe_status(state.status)
            return Nasdaq100EarningsCalendar(
                generated_at=state.generated_at.isoformat(),
                coverage_start=state.coverage_start.isoformat(),
                coverage_end=state.coverage_end.isoformat(),
                status=status,
                provider_status=status,
                events=events,
            )
        except Exception as exc:
            logger.error("Supabase NASDAQ calendar read failed (error_type=%s)", type(exc).__name__)
            return None

    def load_economic(self) -> Optional[RollingEconomicCalendar]:
        if not settings.SUPABASE_DATABASE_URL:
            return None
        try:
            with get_db_connection() as conn:
                state = self._latest_state(conn, JOB_NAME_ECONOMIC_CALENDAR_REFRESH)
                if state is None:
                    return None
                tz = ZoneInfo(settings.TIMEZONE)
                start_dt = datetime.combine(state.coverage_start, datetime.min.time(), tzinfo=tz)
                end_dt = datetime.combine(state.coverage_end, datetime.max.time(), tzinfo=tz)
                events = RefreshRepository.get_weekly_economic_events(conn, start_dt, end_dt)

            return RollingEconomicCalendar(
                generated_at=state.generated_at.astimezone(timezone.utc).isoformat(),
                coverage_start=state.coverage_start.isoformat(),
                coverage_end=state.coverage_end.isoformat(),
                status=self._safe_status(state.status),
                provider_results=[],
                events=events,
            )
        except Exception as exc:
            logger.error("Supabase economic calendar read failed (error_type=%s)", type(exc).__name__)
            return None


database_calendar_reader = DatabaseCalendarReader()
