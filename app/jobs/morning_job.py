import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_connection
from app.models.job_tracking import (
    JobExecutionContext,
    JobRunSpecification,
)
from app.models.market_summary_job import (
    MarketSummaryJobResult,
    MarketSummaryStatus,
    determine_cli_exit_code,
)
from app.services.email_service import EmailService
from app.services.job_tracking.adapters.market_summary_adapter import (
    MarketSummaryResultAdapter,
)
from app.services.job_tracking.notification.policy import JobNotificationRule
from app.services.job_tracking.notification.renderer import (
    MarketSummaryEmailRenderer,
)
from app.services.job_tracking.runner import TrackedJobRunner
from app.services.market_email_report import build_morning_email_content
from app.services.market_summary import (
    MarketSummaryService,
    market_summary_service,
)
from app.services.morning_calendar_service import (
    MorningCalendarService,
    morning_calendar_service,
)

logger = logging.getLogger(__name__)


class MorningJob:
    """Morning Market Summary Job with Supabase weekly calendar integration and central job tracking."""

    def __init__(
        self,
        market_service: Optional[MarketSummaryService] = None,
        calendar_service: Optional[MorningCalendarService] = None,
        email_service: Optional[EmailService] = None,
        db_connection_factory: Optional[Callable[..., Any]] = None,
        dry_run: bool = False,
        no_email: bool = False,
    ):
        self.market_service = market_service or market_summary_service
        self.calendar_service = calendar_service or morning_calendar_service
        self.email_service = email_service or EmailService()
        self.db_connection_factory = db_connection_factory or get_db_connection
        self.dry_run = dry_run
        self.no_email = no_email

    async def _execute_core(
        self,
        context: JobExecutionContext,
        as_of: Optional[datetime] = None,
    ) -> MarketSummaryJobResult:
        """Execute core business logic: collect market and calendar data, generate report content."""
        started_dt = context.started_at or datetime.now(timezone.utc)
        started_str = started_dt.isoformat()

        tz = ZoneInfo(settings.TIMEZONE)
        now = as_of or datetime.now(tz)

        # 1. Fetch market summary
        summary_task = self.market_service.generate(now)

        # 2. Fetch weekly economic and earnings calendars from Supabase
        (
            economic_events,
            earnings_events,
            calendar_warnings,
            source,
        ) = self.calendar_service.get_weekly_calendar(now)

        logger.info(
            "Weekly calendar resolved (source=%s): %d economic events, %d earnings events",
            source,
            len(economic_events),
            len(earnings_events),
        )

        snapshot = await summary_task

        # Combine warnings from snapshot and calendar
        combined_warnings: List[str] = []
        if snapshot and snapshot.warnings:
            combined_warnings.extend(snapshot.warnings)
        if calendar_warnings:
            combined_warnings.extend(calendar_warnings)
        # Deduplicate while preserving order
        combined_warnings = list(dict.fromkeys(combined_warnings))

        # 3. Format morning email content
        email_content = build_morning_email_content(
            snapshot=snapshot,
            events=economic_events,
            as_of=now,
            warnings=calendar_warnings,
            earnings_events=earnings_events,
        )

        # 4. Determine status: success if no warnings, partial if non-fatal warnings exist
        status: MarketSummaryStatus = "partial" if combined_warnings else "success"

        # 5. Extract safe snapshot metrics
        market = snapshot.market if snapshot else None
        bist_price = market.bist100.price if market and market.bist100 else None
        usdtry_price = market.usdtry.price if market and market.usdtry else None
        eurtry_price = market.eurtry.price if market and market.eurtry else None
        gold_price = market.gold.gram_try if market and market.gold else None
        btc_price = market.bitcoin.usd if market and market.bitcoin else None

        metrics: dict[str, Any] = {
            "report_type": "morning",
            "calendar_source": source,
            "economic_event_count": len(economic_events),
            "earnings_event_count": len(earnings_events),
            "warning_count": len(combined_warnings),
            "bist_price": bist_price,
            "usdtry_price": usdtry_price,
            "eurtry_price": eurtry_price,
            "gold_gram_try": gold_price,
            "bitcoin_usd": btc_price,
        }

        finished_dt = datetime.now(timezone.utc)
        finished_str = finished_dt.isoformat()
        duration = max(0.0, (finished_dt - started_dt).total_seconds())

        return MarketSummaryJobResult(
            status=status,
            report_type="morning",
            started_at=started_str,
            finished_at=finished_str,
            duration_seconds=duration,
            warnings=combined_warnings,
            error=None,
            metrics=metrics,
            subject=email_content.subject,
            body_text=email_content.body_text,
            body_html=email_content.body_html,
            dry_run=self.dry_run,
            no_email=self.no_email,
        )

    async def run(self, as_of: Optional[datetime] = None) -> MarketSummaryJobResult:
        """Run morning job through central TrackedJobRunner lifecycle."""
        runner = TrackedJobRunner(
            spec=JobRunSpecification(
                job_name="morning_job",
                operation_type="market_summary",
                scope="MARKET_SUMMARY",
                provider_code=None,
            ),
            adapter=MarketSummaryResultAdapter(),
            renderer=MarketSummaryEmailRenderer(),
            notification_rule=JobNotificationRule.ALWAYS_ON_FINISH,
            email_service=self.email_service,
            db_connection_factory=self.db_connection_factory,
        )

        return await runner.run_async(
            lambda ctx: self._execute_core(context=ctx, as_of=as_of),
            dry_run=self.dry_run,
            no_email=self.no_email,
        )


async def run_morning_job(
    argv: Optional[List[str]] = None,
    market_service: Optional[MarketSummaryService] = None,
    calendar_service: Optional[MorningCalendarService] = None,
    email_service: Optional[EmailService] = None,
    db_connection_factory: Optional[Callable[..., Any]] = None,
    as_of: Optional[datetime] = None,
    dry_run: bool = False,
    no_email: bool = False,
) -> MarketSummaryJobResult:
    """CLI and programmatic entry point for morning job."""
    if argv is not None:
        parser = argparse.ArgumentParser(description="Run Morning Market Summary Job")
        parser.add_argument("--dry-run", action="store_true", help="Execute without DB writes or email delivery")
        parser.add_argument("--no-email", action="store_true", help="Execute tracking without sending email")
        args = parser.parse_args(argv)
        dry_run = dry_run or args.dry_run
        no_email = no_email or args.no_email

    job = MorningJob(
        market_service=market_service,
        calendar_service=calendar_service,
        email_service=email_service,
        db_connection_factory=db_connection_factory,
        dry_run=dry_run,
        no_email=no_email,
    )
    return await job.run(as_of=as_of)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    result = asyncio.run(run_morning_job(argv=sys.argv[1:]))
    sys.exit(determine_cli_exit_code(result))
