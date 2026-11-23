import asyncio
import logging
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Set
from zoneinfo import ZoneInfo

from pydantic import BaseModel, model_validator

from app.core.config import settings
from app.models.earnings import (
    Bist100EarningsCalendar,
    BistCombinedEarningsResult,
    BistEarningsCalendar,
    BistEarningsEvent,
    KapEarningsFetchMetrics,
    KapEarningsFetchResult,
    OfficialIrProviderResult,
)
from app.providers.earnings.kap_provider import KapEarningsProvider
from app.providers.earnings.official_ir.registry import (
    OfficialIrRegistry,
    official_ir_registry,
)
from app.services.bist_reconciliation import (
    reconcile_kap_and_official_ir,
    scope_official_ir_results,
)

logger = logging.getLogger(__name__)


class Bist100EarningsRefreshBundle(BaseModel):
    rolling_calendar: Bist100EarningsCalendar
    combined_result: BistCombinedEarningsResult

    @model_validator(mode="before")
    @classmethod
    def handle_fetch_result_compat(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "combined_result" not in data and "fetch_result" in data:
                fetch_res = data["fetch_result"]
                norm_events = getattr(fetch_res, "normalized_events", None) or getattr(fetch_res, "events", [])
                data["combined_result"] = BistCombinedEarningsResult(
                    status=getattr(fetch_res, "status", "success"),
                    rolling_events=getattr(fetch_res, "events", []),
                    normalized_events=norm_events,
                    kap_fetch_result=fetch_res if isinstance(fetch_res, KapEarningsFetchResult) else KapEarningsFetchResult(status="success"),
                    warnings=getattr(fetch_res, "warnings", [])
                )
        return data

    @property
    def fetch_result(self) -> KapEarningsFetchResult:
        """Backward-compatible accessor for KAP fetch result."""
        return self.combined_result.kap_fetch_result


def build_bist100_rolling_calendar(
    combined_result: Optional[BistCombinedEarningsResult] = None,
    fetch_result: Optional[KapEarningsFetchResult] = None,
    coverage_start: Optional[date] = None,
    coverage_end: Optional[date] = None,
    generated_at: Optional[datetime] = None,
    whitelist: Optional[Set[str]] = None,
) -> Bist100EarningsCalendar:
    """Pure transformation helper to construct Bist100EarningsCalendar (schema 2.0)."""
    if combined_result is None and fetch_result is not None:
        norm_events = fetch_result.normalized_events or fetch_result.events
        combined_result = BistCombinedEarningsResult(
            status=fetch_result.status,
            rolling_events=fetch_result.events,
            normalized_events=norm_events,
            kap_fetch_result=fetch_result,
            warnings=fetch_result.warnings
        )

    if combined_result is None:
        raise ValueError("Either combined_result or fetch_result must be provided")

    cov_start = coverage_start or date.today()
    cov_end = coverage_end or (cov_start + timedelta(days=90))
    gen_dt = generated_at or datetime.now(ZoneInfo(settings.TIMEZONE))
    sym_whitelist = whitelist or set()

    cov_start_str = cov_start.isoformat()
    cov_end_str = cov_end.isoformat()

    filtered_events: List[BistEarningsEvent] = [
        ev for ev in combined_result.normalized_events
        if cov_start_str <= ev.earnings_date <= cov_end_str
        and (not sym_whitelist or ev.symbol.upper().replace(".IS", "") in sym_whitelist)
    ]

    filtered_events.sort(key=lambda ev: (ev.earnings_date, ev.symbol, ev.fiscal_year or 0, ev.fiscal_period or ""))

    # Update symbol_results from KAP with any IR scheduled events
    symbol_results = deepcopy(combined_result.kap_fetch_result.symbol_results)
    scheduled_symbols_in_rolling = {ev.symbol for ev in filtered_events}

    for sr in symbol_results:
        if sr.symbol in scheduled_symbols_in_rolling:
            if sr.calendar_status != "scheduled":
                sr.calendar_status = "scheduled"
            sym_ev_count = sum(1 for ev in filtered_events if ev.symbol == sr.symbol)
            sr.scheduled_event_count = sym_ev_count

    return Bist100EarningsCalendar(
        schema_version="2.0",
        generated_at=gen_dt.isoformat(),
        coverage_start=cov_start_str,
        coverage_end=cov_end_str,
        market="BIST100",
        status=combined_result.status,
        provider_status=combined_result.status,
        events=filtered_events,
        symbol_results=symbol_results,
        ir_incomplete_symbols=combined_result.ir_incomplete_symbols,
        warnings=combined_result.warnings,
        metrics=combined_result.kap_fetch_result.metrics
    )


class Bist100EarningsService:
    def __init__(
        self,
        provider: Optional[KapEarningsProvider] = None,
        official_ir_reg: Optional[OfficialIrRegistry] = None
    ):
        self.provider = provider or KapEarningsProvider()
        self.official_ir_registry = official_ir_reg or official_ir_registry

    async def get_week_events(
        self,
        offset: int = 0,
        ref_date: Optional[datetime] = None
    ) -> BistEarningsCalendar:
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
            fetch_result = await self.provider.get_earnings_result(monday, sunday)
            bist100_whitelist = set(self.provider._load_bist100_symbols())

            week_events: List[BistEarningsEvent] = [
                ev for ev in fetch_result.events
                if mon_str <= ev.earnings_date <= sun_str and ev.symbol.upper().replace(".IS", "") in bist100_whitelist
            ]

            week_events.sort(key=lambda ev: (ev.earnings_date, ev.symbol))

            return BistEarningsCalendar(
                schema_version="1.0",
                generated_at=datetime.now(tz).isoformat(),
                week_start=mon_str,
                week_end=sun_str,
                market="BIST100",
                status=fetch_result.status,
                provider_status=fetch_result.status,
                events=week_events,
                warnings=fetch_result.warnings
            )
        except Exception as e:
            logger.error(f"Error generating BIST100 weekly earnings calendar: {e}", exc_info=True)
            return BistEarningsCalendar(
                schema_version="1.0",
                generated_at=datetime.now(tz).isoformat(),
                week_start=mon_str,
                week_end=sun_str,
                market="BIST100",
                status="failed",
                provider_status="failed",
                events=[],
                warnings=[f"Failed to fetch BIST100 earnings: {str(e)}"]
            )

    async def get_refresh_bundle(
        self,
        as_of: Optional[datetime] = None
    ) -> Bist100EarningsRefreshBundle:
        """Construct BIST100 refresh bundle by reconciling KAP and Official IR provider fetches with exception isolation."""
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
            kap_task = self.provider.get_earnings_result(coverage_start_dt, coverage_end_dt)
            ir_task = self.official_ir_registry.fetch_all() if self.official_ir_registry else asyncio.sleep(0, result=[])

            # Isolated execution: One source failure does not discard the other
            gather_results = await asyncio.gather(kap_task, ir_task, return_exceptions=True)
            kap_res_raw = gather_results[0]
            ir_res_raw = gather_results[1]

            if isinstance(kap_res_raw, Exception):
                logger.error(
                    f"Exception during KAP provider fetch: {kap_res_raw}",
                    exc_info=(type(kap_res_raw), kap_res_raw, kap_res_raw.__traceback__)
                )
                kap_fetch_result = KapEarningsFetchResult(
                    status="failed",
                    events=[],
                    normalized_events=[],
                    warnings=[f"KAP fetch exception: {str(kap_res_raw)}"]
                )
            else:
                kap_fetch_result = kap_res_raw

            if isinstance(ir_res_raw, Exception):
                logger.error(
                    f"Exception during Official IR fetch: {ir_res_raw}",
                    exc_info=(type(ir_res_raw), ir_res_raw, ir_res_raw.__traceback__)
                )
                ir_results: List[OfficialIrProviderResult] = []
                # If registry has known providers, create structured failed results for each
                if self.official_ir_registry:
                    for prov in self.official_ir_registry.get_all_providers():
                        ir_results.append(OfficialIrProviderResult(
                            symbol=prov.symbol,
                            source_provider=prov.source_provider,
                            status="failed",
                            parser_failed=True,
                            warnings=[f"Official IR registry exception: {str(ir_res_raw)}"]
                        ))
            else:
                ir_results = ir_res_raw

            # Keep provider results within the same rolling window saved to Supabase.
            scoped_ir_results = scope_official_ir_results(
                ir_results=ir_results,
                earnings_date_from=coverage_start,
                earnings_date_to=coverage_end,
            )

            combined_result = reconcile_kap_and_official_ir(
                kap_result=kap_fetch_result,
                ir_results=scoped_ir_results,
                relevance_start=coverage_start,
                relevance_end=coverage_end,
                emit_out_of_scope_warnings=False
            )

            whitelist = set(self.provider._load_bist100_symbols())
            rolling_calendar = build_bist100_rolling_calendar(
                combined_result=combined_result,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                generated_at=as_of_dt,
                whitelist=whitelist
            )
            combined_result.rolling_events = rolling_calendar.events

            return Bist100EarningsRefreshBundle(
                rolling_calendar=rolling_calendar,
                combined_result=combined_result
            )
        except Exception as e:
            logger.error(f"Fatal error generating BIST100 earnings refresh bundle: {e}", exc_info=True)
            failed_calendar = Bist100EarningsCalendar(
                schema_version="2.0",
                generated_at=as_of_dt.isoformat(),
                coverage_start=coverage_start.isoformat(),
                coverage_end=coverage_end.isoformat(),
                market="BIST100",
                status="failed",
                provider_status="failed",
                events=[],
                symbol_results=[],
                warnings=[f"Failed to build rolling BIST100 earnings calendar: {str(e)}"],
                metrics=KapEarningsFetchMetrics()
            )
            failed_fetch = KapEarningsFetchResult(
                status="failed",
                events=[],
                normalized_events=[],
                warnings=[f"Failed to fetch BIST100 earnings: {str(e)}"]
            )
            failed_combined = BistCombinedEarningsResult(
                status="failed",
                rolling_events=[],
                normalized_events=[],
                kap_fetch_result=failed_fetch,
                official_ir_results=[],
                warnings=[f"Failed to build rolling BIST100 earnings calendar: {str(e)}"]
            )
            return Bist100EarningsRefreshBundle(
                rolling_calendar=failed_calendar,
                combined_result=failed_combined
            )

    async def get_rolling_calendar(
        self,
        as_of: Optional[datetime] = None
    ) -> Bist100EarningsCalendar:
        """Construct rolling 91-calendar-day canonical BIST100 earnings calendar (schema 2.0)."""
        bundle = await self.get_refresh_bundle(as_of=as_of)
        return bundle.rolling_calendar


bist100_earnings_service = Bist100EarningsService()
