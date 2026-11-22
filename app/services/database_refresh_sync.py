import logging
import os
from datetime import date, datetime
from typing import List, Optional, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_connection
from app.models.earnings import (
    Bist100EarningsCalendar,
    Nasdaq100EarningsCalendar,
)
from app.models.earnings_refresh import (
    EarningsRefreshJobResult,
    OfficialIrProviderSummary,
)
from app.models.economic_calendar import RollingEconomicCalendar
from app.models.economic_calendar_refresh import EconomicCalendarRefreshJobResult
from app.repositories.refresh_repository import (
    RefreshRepository,
    SyncEarningsEventData,
    SyncEconomicEventData,
)
from app.services.database_event_identity import (
    build_bist_earnings_identity,
    build_economic_event_identity,
    build_nasdaq_earnings_identity,
)
from app.services.provider_code_resolver import (
    resolve_bist_provider_code,
    resolve_economic_provider_code,
)

logger = logging.getLogger(__name__)

TZ_ISTANBUL = ZoneInfo(settings.TIMEZONE)


def parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value.strip())


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_ISTANBUL)
    return parsed

VALID_SYNC_ACTIONS = {
    "replaced",
    "merged_and_updated",
    "initial_partial_written",
}


def resolve_official_ir_aggregate_status(providers: List[OfficialIrProviderSummary]) -> str:
    """Resolve aggregate status for Official IR providers in Supabase telemetry.

    Rules:
    - Empty provider list -> 'failed'
    - All providers 'success' and parser_failed is False -> 'success'
    - All providers unavailable (status == 'failed' or parser_failed is True) -> 'failed'
    - Any other mix (e.g. some success, some partial, or all partial) -> 'partial'
    """
    if not providers:
        return "failed"

    all_success = all(p.status == "success" and not getattr(p, "parser_failed", False) for p in providers)
    if all_success:
        return "success"

    all_unavailable = all(p.status == "failed" or getattr(p, "parser_failed", False) for p in providers)
    if all_unavailable:
        return "failed"

    return "partial"


def resolve_refresh_trigger_type() -> str:
    """Resolve job trigger type based on GITHUB_EVENT_NAME environment variable.

    Returns:
    - 'scheduled' if GITHUB_EVENT_NAME == 'schedule'
    - 'manual' for workflow_dispatch, local runs, or any other trigger
    """
    return (
        "scheduled"
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule"
        else "manual"
    )


def sync_earnings_refresh_to_database(
    result: EarningsRefreshJobResult,
    final_bist: Optional[Bist100EarningsCalendar],
    final_nasdaq: Optional[Nasdaq100EarningsCalendar],
    refresh_run_id: Optional[UUID] = None,
    dry_run: bool = False,
) -> Tuple[str, Optional[str]]:
    """Synchronize completed daily earnings calendar events to Supabase PostgreSQL.

    Ownership of refresh_runs and provider_run_results records belongs solely to TrackedJobRunner.
    Returns (database_status, database_error).
    """
    if dry_run:
        return ("skipped_dry_run", None)

    if refresh_run_id is None:
        logger.error("Mandatory refresh_run_id is missing in production earnings database synchronization.")
        return ("failed", "Missing mandatory refresh_run_id in production mode")

    if not settings.SUPABASE_DATABASE_URL:
        return ("skipped_not_configured", None)

    try:
        with get_db_connection() as conn:
            with conn.transaction():
                # 1. Fetch provider and instrument ID mappings inside the transaction
                provider_map = RefreshRepository.get_provider_id_map(conn)
                instrument_map = RefreshRepository.get_instrument_id_map(conn)

                cov_start = date.fromisoformat(result.coverage_start)
                cov_end = date.fromisoformat(result.coverage_end)

                # 2. Synchronize BIST Events (if state was updated)
                if (
                    result.bist.sync_action in VALID_SYNC_ACTIONS
                    and final_bist is not None
                ):
                    bist_sync_events: List[SyncEarningsEventData] = []
                    for ev in final_bist.events:
                        p_code = resolve_bist_provider_code(ev.source)
                        if not p_code:
                            raise ValueError(
                                f"Unknown or missing provider for BIST event: source='{ev.source}' symbol='{ev.symbol}'"
                            )

                        id_key = build_bist_earnings_identity(
                            ev.symbol, ev.period_start, ev.period_end
                        )
                        bist_sync_events.append(
                            SyncEarningsEventData(
                                market="BIST100",
                                symbol=ev.symbol.strip().upper(),
                                identity_key=id_key,
                                earnings_date=date.fromisoformat(ev.earnings_date),
                                provider_code=p_code,
                                fiscal_year=ev.fiscal_year,
                                fiscal_period=ev.fiscal_period,
                                period_start=parse_iso_date(ev.period_start),
                                period_end=parse_iso_date(ev.period_end),
                                date_type=ev.date_type,
                                session=None,
                                source_provider=ev.source_provider,
                                source_event_id=ev.source_event_id,
                                source_url=ev.source_url,
                                source_type=ev.source_type,
                                publish_date=parse_iso_date(ev.publish_date),
                                disclosure_index=ev.disclosure_index,
                                is_active=True,
                                is_stale=False,
                                carried_forward=False,
                            )
                        )

                    RefreshRepository.upsert_earnings_events(
                        conn, refresh_run_id, bist_sync_events, instrument_map, provider_map
                    )

                    # Scoped inactive: only when fetch_status == "success" and sync_action == "replaced"
                    if (
                        result.bist.fetch_status == "success"
                        and result.bist.sync_action == "replaced"
                    ):
                        active_bist_keys = {ev.identity_key for ev in bist_sync_events}
                        RefreshRepository.deactivate_missing_earnings_events(
                            conn, refresh_run_id, "BIST100", cov_start, cov_end, active_bist_keys
                        )

                # 5. Synchronize NASDAQ Events (if state was updated)
                if (
                    result.nasdaq.sync_action in VALID_SYNC_ACTIONS
                    and final_nasdaq is not None
                ):
                    nasdaq_sync_events: List[SyncEarningsEventData] = []
                    for ev in final_nasdaq.events:
                        id_key = build_nasdaq_earnings_identity(
                            ev.symbol, ev.earnings_date, ev.session
                        )
                        nasdaq_sync_events.append(
                            SyncEarningsEventData(
                                market="NASDAQ100",
                                symbol=ev.symbol.strip().upper(),
                                identity_key=id_key,
                                earnings_date=date.fromisoformat(ev.earnings_date),
                                provider_code="YAHOO_FINANCE",
                                fiscal_year=None,
                                fiscal_period=None,
                                period_start=None,
                                period_end=None,
                                date_type="estimated",
                                session=ev.session,
                                source_provider="Yahoo Finance",
                                source_event_id=None,
                                source_url=None,
                                source_type=None,
                                publish_date=None,
                                disclosure_index=None,
                                is_active=True,
                                is_stale=False,
                                carried_forward=False,
                            )
                        )

                    RefreshRepository.upsert_earnings_events(
                        conn, refresh_run_id, nasdaq_sync_events, instrument_map, provider_map
                    )

                    # Scoped inactive: only when fetch_status == "success" and sync_action == "replaced"
                    if (
                        result.nasdaq.fetch_status == "success"
                        and result.nasdaq.sync_action == "replaced"
                    ):
                        active_nasdaq_keys = {ev.identity_key for ev in nasdaq_sync_events}
                        RefreshRepository.deactivate_missing_earnings_events(
                            conn, refresh_run_id, "NASDAQ100", cov_start, cov_end, active_nasdaq_keys
                        )

                return ("success", None)

    except AssertionError:
        raise
    except Exception as exc:
        logger.error(f"Database synchronization failed: {type(exc).__name__}")
        return ("failed", f"{type(exc).__name__}: database synchronization error")


def sync_economic_refresh_to_database(
    result: EconomicCalendarRefreshJobResult,
    final_calendar: Optional[RollingEconomicCalendar],
    refresh_run_id: Optional[UUID] = None,
    dry_run: bool = False,
) -> Tuple[str, Optional[str]]:
    """Synchronize completed daily economic calendar events to Supabase PostgreSQL.

    Ownership of refresh_runs and provider_run_results records belongs solely to TrackedJobRunner.
    Returns (database_status, database_error).
    """
    if dry_run:
        return ("skipped_dry_run", None)

    if refresh_run_id is None:
        logger.error("Mandatory refresh_run_id is missing in production economic database synchronization.")
        return ("failed", "Missing mandatory refresh_run_id in production mode")

    if not settings.SUPABASE_DATABASE_URL:
        return ("skipped_not_configured", None)

    try:
        with get_db_connection() as conn:
            with conn.transaction():
                # 1. Fetch provider ID map inside the transaction
                provider_map = RefreshRepository.get_provider_id_map(conn)

                cov_start = date.fromisoformat(result.coverage_start)
                cov_end = date.fromisoformat(result.coverage_end)

                # 2. Synchronize Economic Events (if state was updated)
                if (
                    result.sync_action in VALID_SYNC_ACTIONS
                    and final_calendar is not None
                ):
                    econ_sync_events: List[SyncEconomicEventData] = []
                    for ev in final_calendar.events:
                        p_code = resolve_economic_provider_code(ev.provider_id, ev.source)
                        if not p_code:
                            raise ValueError(
                                f"Unknown economic provider for event '{ev.name}': provider_id='{ev.provider_id}' source='{ev.source}'"
                            )

                        id_key = build_economic_event_identity(ev)
                        scheduled_dt = parse_iso_datetime(ev.scheduled_at_istanbul)
                        if not scheduled_dt:
                            raise ValueError(
                                f"Invalid scheduled_at_istanbul for event '{ev.name}': {ev.scheduled_at_istanbul}"
                            )

                        econ_sync_events.append(
                            SyncEconomicEventData(
                                identity_key=id_key,
                                provider_code=p_code,
                                name=ev.name,
                                country=ev.country,
                                scheduled_at=scheduled_dt,
                                event_code=ev.event_code,
                                reference_period=ev.reference_period,
                                source_event_id=ev.source_event_id,
                                source_url=ev.source_url,
                                is_active=True,
                                is_stale=ev.stale,
                                carried_forward=ev.carried_forward,
                            )
                        )

                    RefreshRepository.upsert_economic_events(
                        conn, refresh_run_id, econ_sync_events, provider_map
                    )

                    # Scoped inactive: only for providers with status == "success" and structure_validated is True
                    cov_start_dt = datetime.combine(cov_start, datetime.min.time(), tzinfo=TZ_ISTANBUL)
                    cov_end_dt = datetime.combine(cov_end, datetime.max.time(), tzinfo=TZ_ISTANBUL)

                    for po in result.provider_outcomes:
                        if po.status == "success" and po.structure_validated is True:
                            p_code = resolve_economic_provider_code(po.provider_id, po.provider_name)
                            prov_id = provider_map.get(p_code)
                            if prov_id:
                                active_keys_for_prov = {
                                    build_economic_event_identity(e)
                                    for e in final_calendar.events
                                    if resolve_economic_provider_code(e.provider_id, e.source) == p_code
                                }
                                RefreshRepository.deactivate_missing_economic_events(
                                    conn, refresh_run_id, prov_id, cov_start_dt, cov_end_dt, active_keys_for_prov
                                )

                return ("success", None)

    except AssertionError:
        raise
    except Exception as exc:
        logger.error(f"Database synchronization failed: {type(exc).__name__}")
        return ("failed", f"{type(exc).__name__}: database synchronization error")
