import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Connection

from app.models.market import EconomicEvent


@dataclass(frozen=True)
class RefreshRunData:
    job_name: str
    started_at: datetime
    status: str
    fresh_event_count: int
    final_event_count: int
    finished_at: Optional[datetime] = None
    coverage_start: Optional[date] = None
    coverage_end: Optional[date] = None
    trigger_type: str = "scheduled"
    target_year: Optional[int] = None
    sync_action: Optional[str] = None
    warnings: List[str] = None
    metrics: Dict[str, Any] = None
    error: Optional[str] = None
    github_run_id: Optional[str] = None


@dataclass(frozen=True)
class ProviderRunResultData:
    provider_id: UUID
    scope: str
    status: str
    structure_validated: Optional[bool] = None
    observed_count: int = 0
    candidate_count: int = 0
    fresh_event_count: int = 0
    carried_forward_count: int = 0
    malformed_count: int = 0
    retry_count: int = 0
    warnings: List[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class SyncEarningsEventData:
    market: str
    symbol: str
    identity_key: str
    earnings_date: date
    provider_code: str
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    date_type: Optional[str] = None
    session: Optional[str] = None
    source_provider: Optional[str] = None
    source_event_id: Optional[str] = None
    source_url: Optional[str] = None
    source_type: Optional[str] = None
    publish_date: Optional[date] = None
    disclosure_index: Optional[int] = None
    is_active: bool = True
    is_stale: bool = False
    carried_forward: bool = False


@dataclass(frozen=True)
class SyncEconomicEventData:
    identity_key: str
    provider_code: str
    name: str
    country: str
    scheduled_at: datetime
    event_code: Optional[str] = None
    reference_period: Optional[str] = None
    source_event_id: Optional[str] = None
    source_url: Optional[str] = None
    is_active: bool = True
    is_stale: bool = False
    carried_forward: bool = False


class RefreshRepository:
    """Repository for transactional logging and synchronization of daily refresh runs."""

    @staticmethod
    def get_provider_id_map(conn: Connection) -> Dict[str, UUID]:
        """Fetch all data providers mapping code -> UUID."""
        with conn.cursor() as cur:
            cur.execute("SELECT code, id FROM data_providers;")
            rows = cur.fetchall()
            return {row[0]: UUID(str(row[1])) for row in rows}

    @staticmethod
    def get_instrument_id_map(conn: Connection) -> Dict[Tuple[str, str], UUID]:
        """Fetch all instruments mapping (market, symbol) -> UUID."""
        with conn.cursor() as cur:
            cur.execute("SELECT market, symbol, id FROM instruments;")
            rows = cur.fetchall()
            return {(row[0], row[1]): UUID(str(row[2])) for row in rows}

    @staticmethod
    def insert_refresh_run(
        conn: Connection,
        run_data: RefreshRunData,
    ) -> UUID:
        """Insert a new refresh_runs record and return its generated UUID."""
        query = """
            INSERT INTO refresh_runs (
              job_name, trigger_type, target_year, started_at, finished_at,
              coverage_start, coverage_end, status, sync_action,
              fresh_event_count, final_event_count, warnings, metrics, error, github_run_id
            )
            VALUES (
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s
            )
            RETURNING id;
        """
        warnings_json = json.dumps(run_data.warnings or [], ensure_ascii=False)
        metrics_json = json.dumps(run_data.metrics or {}, ensure_ascii=False)

        params = (
            run_data.job_name,
            run_data.trigger_type,
            run_data.target_year,
            run_data.started_at,
            run_data.finished_at,
            run_data.coverage_start,
            run_data.coverage_end,
            run_data.status,
            run_data.sync_action,
            run_data.fresh_event_count,
            run_data.final_event_count,
            warnings_json,
            metrics_json,
            run_data.error,
            run_data.github_run_id,
        )
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return UUID(str(row[0]))

    @staticmethod
    def insert_provider_run_results(
        conn: Connection,
        refresh_run_id: UUID,
        results: List[ProviderRunResultData],
    ) -> int:
        """Insert provider_run_results batch linked to a refresh_run."""
        if not results:
            return 0

        query = """
            INSERT INTO provider_run_results (
              refresh_run_id, provider_id, scope, status, structure_validated,
              observed_count, candidate_count, fresh_event_count, carried_forward_count,
              malformed_count, retry_count, warnings, error
            )
            VALUES (
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s,
              %s, %s, %s, %s
            )
            ON CONFLICT (refresh_run_id, provider_id, scope) DO UPDATE
            SET
              status = EXCLUDED.status,
              structure_validated = EXCLUDED.structure_validated,
              observed_count = EXCLUDED.observed_count,
              candidate_count = EXCLUDED.candidate_count,
              fresh_event_count = EXCLUDED.fresh_event_count,
              carried_forward_count = EXCLUDED.carried_forward_count,
              malformed_count = EXCLUDED.malformed_count,
              retry_count = EXCLUDED.retry_count,
              warnings = EXCLUDED.warnings,
              error = EXCLUDED.error;
        """
        params = [
            (
                refresh_run_id,
                res.provider_id,
                res.scope,
                res.status,
                res.structure_validated,
                res.observed_count,
                res.candidate_count,
                res.fresh_event_count,
                res.carried_forward_count,
                res.malformed_count,
                res.retry_count,
                json.dumps(res.warnings or [], ensure_ascii=False),
                res.error,
            )
            for res in results
        ]
        with conn.cursor() as cur:
            cur.executemany(query, params)
            return cur.rowcount if cur.rowcount != -1 else len(results)

    @staticmethod
    def upsert_earnings_events(
        conn: Connection,
        refresh_run_id: UUID,
        events: List[SyncEarningsEventData],
        instrument_map: Dict[Tuple[str, str], UUID],
        provider_map: Dict[str, UUID],
    ) -> int:
        """Upsert earnings events with last_refresh_run_id tracking."""
        if not events:
            return 0

        query = """
            INSERT INTO earnings_events (
              instrument_id, market, identity_key, fiscal_year, fiscal_period,
              period_start, period_end, earnings_date, date_type, session,
              provider_id, source_provider, source_event_id, source_url, source_type,
              publish_date, disclosure_index, is_active, is_stale, carried_forward,
              last_refresh_run_id
            )
            VALUES (
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s
            )
            ON CONFLICT (market, identity_key) DO UPDATE
            SET
              instrument_id = EXCLUDED.instrument_id,
              fiscal_year = EXCLUDED.fiscal_year,
              fiscal_period = EXCLUDED.fiscal_period,
              period_start = EXCLUDED.period_start,
              period_end = EXCLUDED.period_end,
              earnings_date = EXCLUDED.earnings_date,
              date_type = EXCLUDED.date_type,
              session = EXCLUDED.session,
              provider_id = EXCLUDED.provider_id,
              source_provider = EXCLUDED.source_provider,
              source_event_id = EXCLUDED.source_event_id,
              source_url = EXCLUDED.source_url,
              source_type = EXCLUDED.source_type,
              publish_date = EXCLUDED.publish_date,
              disclosure_index = EXCLUDED.disclosure_index,
              is_active = EXCLUDED.is_active,
              is_stale = EXCLUDED.is_stale,
              carried_forward = EXCLUDED.carried_forward,
              last_refresh_run_id = EXCLUDED.last_refresh_run_id,
              last_seen_at = now(),
              last_changed_at = CASE
                WHEN earnings_events.instrument_id IS DISTINCT FROM EXCLUDED.instrument_id
                  OR earnings_events.fiscal_year IS DISTINCT FROM EXCLUDED.fiscal_year
                  OR earnings_events.fiscal_period IS DISTINCT FROM EXCLUDED.fiscal_period
                  OR earnings_events.period_start IS DISTINCT FROM EXCLUDED.period_start
                  OR earnings_events.period_end IS DISTINCT FROM EXCLUDED.period_end
                  OR earnings_events.earnings_date IS DISTINCT FROM EXCLUDED.earnings_date
                  OR earnings_events.date_type IS DISTINCT FROM EXCLUDED.date_type
                  OR earnings_events.session IS DISTINCT FROM EXCLUDED.session
                  OR earnings_events.provider_id IS DISTINCT FROM EXCLUDED.provider_id
                  OR earnings_events.source_provider IS DISTINCT FROM EXCLUDED.source_provider
                  OR earnings_events.source_event_id IS DISTINCT FROM EXCLUDED.source_event_id
                  OR earnings_events.source_url IS DISTINCT FROM EXCLUDED.source_url
                  OR earnings_events.source_type IS DISTINCT FROM EXCLUDED.source_type
                  OR earnings_events.publish_date IS DISTINCT FROM EXCLUDED.publish_date
                  OR earnings_events.disclosure_index IS DISTINCT FROM EXCLUDED.disclosure_index
                  OR earnings_events.is_active IS DISTINCT FROM EXCLUDED.is_active
                  OR earnings_events.is_stale IS DISTINCT FROM EXCLUDED.is_stale
                  OR earnings_events.carried_forward IS DISTINCT FROM EXCLUDED.carried_forward
                THEN now()
                ELSE earnings_events.last_changed_at
              END,
              updated_at = now()
            WHERE
              earnings_events.instrument_id IS DISTINCT FROM EXCLUDED.instrument_id
              OR earnings_events.fiscal_year IS DISTINCT FROM EXCLUDED.fiscal_year
              OR earnings_events.fiscal_period IS DISTINCT FROM EXCLUDED.fiscal_period
              OR earnings_events.period_start IS DISTINCT FROM EXCLUDED.period_start
              OR earnings_events.period_end IS DISTINCT FROM EXCLUDED.period_end
              OR earnings_events.earnings_date IS DISTINCT FROM EXCLUDED.earnings_date
              OR earnings_events.date_type IS DISTINCT FROM EXCLUDED.date_type
              OR earnings_events.session IS DISTINCT FROM EXCLUDED.session
              OR earnings_events.provider_id IS DISTINCT FROM EXCLUDED.provider_id
              OR earnings_events.source_provider IS DISTINCT FROM EXCLUDED.source_provider
              OR earnings_events.source_event_id IS DISTINCT FROM EXCLUDED.source_event_id
              OR earnings_events.source_url IS DISTINCT FROM EXCLUDED.source_url
              OR earnings_events.source_type IS DISTINCT FROM EXCLUDED.source_type
              OR earnings_events.publish_date IS DISTINCT FROM EXCLUDED.publish_date
              OR earnings_events.disclosure_index IS DISTINCT FROM EXCLUDED.disclosure_index
              OR earnings_events.is_active IS DISTINCT FROM EXCLUDED.is_active
              OR earnings_events.is_stale IS DISTINCT FROM EXCLUDED.is_stale
              OR earnings_events.carried_forward IS DISTINCT FROM EXCLUDED.carried_forward
              OR earnings_events.last_refresh_run_id IS DISTINCT FROM EXCLUDED.last_refresh_run_id;
        """
        params = []
        for ev in events:
            inst_id = instrument_map.get((ev.market, ev.symbol))
            if not inst_id:
                raise ValueError(
                    f"Instrument not found in database for {ev.market} symbol {ev.symbol}"
                )

            prov_id = provider_map.get(ev.provider_code)
            if not prov_id:
                raise ValueError(
                    f"Provider code '{ev.provider_code}' not found in data_providers"
                )

            params.append(
                (
                    inst_id,
                    ev.market,
                    ev.identity_key,
                    ev.fiscal_year,
                    ev.fiscal_period,
                    ev.period_start,
                    ev.period_end,
                    ev.earnings_date,
                    ev.date_type,
                    ev.session,
                    prov_id,
                    ev.source_provider,
                    ev.source_event_id,
                    ev.source_url,
                    ev.source_type,
                    ev.publish_date,
                    ev.disclosure_index,
                    ev.is_active,
                    ev.is_stale,
                    ev.carried_forward,
                    refresh_run_id,
                )
            )

        with conn.cursor() as cur:
            cur.executemany(query, params)
            return cur.rowcount if cur.rowcount != -1 else len(events)

    @staticmethod
    def deactivate_missing_earnings_events(
        conn: Connection,
        refresh_run_id: UUID,
        market: str,
        coverage_start: date,
        coverage_end: date,
        active_identity_keys: Set[str],
    ) -> int:
        """Deactivate in-coverage earnings events that are no longer in the final calendar."""
        query = """
            UPDATE earnings_events
            SET is_active = false,
                last_refresh_run_id = %s,
                last_changed_at = now(),
                updated_at = now()
            WHERE market = %s
              AND earnings_date >= %s
              AND earnings_date <= %s
              AND is_active = true
              AND NOT (identity_key = ANY(%s));
        """
        keys_list = list(active_identity_keys)
        with conn.cursor() as cur:
            cur.execute(query, (refresh_run_id, market, coverage_start, coverage_end, keys_list))
            return cur.rowcount if cur.rowcount != -1 else 0

    @staticmethod
    def upsert_economic_events(
        conn: Connection,
        refresh_run_id: UUID,
        events: List[SyncEconomicEventData],
        provider_map: Dict[str, UUID],
    ) -> int:
        """Upsert economic events with last_refresh_run_id tracking."""
        if not events:
            return 0

        query = """
            INSERT INTO economic_events (
              identity_key, provider_id, event_code, name, country, reference_period,
              scheduled_at, source_event_id, source_url, is_active, is_stale, carried_forward,
              last_refresh_run_id
            )
            VALUES (
              %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s,
              %s
            )
            ON CONFLICT (identity_key) DO UPDATE
            SET
              provider_id = EXCLUDED.provider_id,
              event_code = EXCLUDED.event_code,
              name = EXCLUDED.name,
              country = EXCLUDED.country,
              reference_period = EXCLUDED.reference_period,
              scheduled_at = EXCLUDED.scheduled_at,
              source_event_id = EXCLUDED.source_event_id,
              source_url = EXCLUDED.source_url,
              is_active = EXCLUDED.is_active,
              is_stale = EXCLUDED.is_stale,
              carried_forward = EXCLUDED.carried_forward,
              last_refresh_run_id = EXCLUDED.last_refresh_run_id,
              last_seen_at = now(),
              last_changed_at = CASE
                WHEN economic_events.provider_id IS DISTINCT FROM EXCLUDED.provider_id
                  OR economic_events.event_code IS DISTINCT FROM EXCLUDED.event_code
                  OR economic_events.name IS DISTINCT FROM EXCLUDED.name
                  OR economic_events.country IS DISTINCT FROM EXCLUDED.country
                  OR economic_events.reference_period IS DISTINCT FROM EXCLUDED.reference_period
                  OR economic_events.scheduled_at IS DISTINCT FROM EXCLUDED.scheduled_at
                  OR economic_events.source_event_id IS DISTINCT FROM EXCLUDED.source_event_id
                  OR economic_events.source_url IS DISTINCT FROM EXCLUDED.source_url
                  OR economic_events.is_active IS DISTINCT FROM EXCLUDED.is_active
                  OR economic_events.is_stale IS DISTINCT FROM EXCLUDED.is_stale
                  OR economic_events.carried_forward IS DISTINCT FROM EXCLUDED.carried_forward
                THEN now()
                ELSE economic_events.last_changed_at
              END,
              updated_at = now()
            WHERE
              economic_events.provider_id IS DISTINCT FROM EXCLUDED.provider_id
              OR economic_events.event_code IS DISTINCT FROM EXCLUDED.event_code
              OR economic_events.name IS DISTINCT FROM EXCLUDED.name
              OR economic_events.country IS DISTINCT FROM EXCLUDED.country
              OR economic_events.reference_period IS DISTINCT FROM EXCLUDED.reference_period
              OR economic_events.scheduled_at IS DISTINCT FROM EXCLUDED.scheduled_at
              OR economic_events.source_event_id IS DISTINCT FROM EXCLUDED.source_event_id
              OR economic_events.source_url IS DISTINCT FROM EXCLUDED.source_url
              OR economic_events.is_active IS DISTINCT FROM EXCLUDED.is_active
              OR economic_events.is_stale IS DISTINCT FROM EXCLUDED.is_stale
              OR economic_events.carried_forward IS DISTINCT FROM EXCLUDED.carried_forward
              OR economic_events.last_refresh_run_id IS DISTINCT FROM EXCLUDED.last_refresh_run_id;
        """
        params = []
        for ev in events:
            prov_id = provider_map.get(ev.provider_code)
            if not prov_id:
                raise ValueError(
                    f"Provider code '{ev.provider_code}' not found in data_providers"
                )

            params.append(
                (
                    ev.identity_key,
                    prov_id,
                    ev.event_code,
                    ev.name,
                    ev.country,
                    ev.reference_period,
                    ev.scheduled_at,
                    ev.source_event_id,
                    ev.source_url,
                    ev.is_active,
                    ev.is_stale,
                    ev.carried_forward,
                    refresh_run_id,
                )
            )

        with conn.cursor() as cur:
            cur.executemany(query, params)
            return cur.rowcount if cur.rowcount != -1 else len(events)

    @staticmethod
    def deactivate_missing_economic_events(
        conn: Connection,
        refresh_run_id: UUID,
        provider_id: UUID,
        coverage_start_dt: datetime,
        coverage_end_dt: datetime,
        active_identity_keys: Set[str],
    ) -> int:
        """Deactivate in-coverage economic events for a validated provider that are missing from final calendar."""
        query = """
            UPDATE economic_events
            SET is_active = false,
                last_refresh_run_id = %s,
                last_changed_at = now(),
                updated_at = now()
            WHERE provider_id = %s
              AND scheduled_at >= %s
              AND scheduled_at <= %s
              AND is_active = true
              AND NOT (identity_key = ANY(%s));
        """
        keys_list = list(active_identity_keys)
        with conn.cursor() as cur:
            cur.execute(query, (refresh_run_id, provider_id, coverage_start_dt, coverage_end_dt, keys_list))
            return cur.rowcount if cur.rowcount != -1 else 0

    @staticmethod
    def get_weekly_economic_events(
        conn: Connection,
        start_dt: datetime,
        end_dt: datetime,
    ) -> List[EconomicEvent]:
        """Fetch active economic events scheduled between start_dt and end_dt."""
        query = """
            SELECT
              e.name,
              e.country,
              e.scheduled_at,
              COALESCE(p.name, p.code, 'TCMB') as source,
              p.code as provider_code,
              e.event_code,
              e.reference_period,
              e.source_event_id,
              e.source_url,
              e.carried_forward,
              e.is_stale
            FROM economic_events e
            LEFT JOIN data_providers p ON e.provider_id = p.id
            WHERE e.is_active = true
              AND e.scheduled_at >= %s
              AND e.scheduled_at <= %s
            ORDER BY e.scheduled_at ASC;
        """
        with conn.cursor() as cur:
            cur.execute(query, (start_dt, end_dt))
            rows = cur.fetchall()

        events: List[EconomicEvent] = []
        tz_istanbul = ZoneInfo("Europe/Istanbul")
        for row in rows:
            name = str(row[0])
            country = str(row[1])
            sched_at_dt = row[2]
            if isinstance(sched_at_dt, datetime):
                sched_at_utc = sched_at_dt.astimezone(timezone.utc).isoformat()
                sched_at_tr = sched_at_dt.astimezone(tz_istanbul).isoformat()
            else:
                sched_at_utc = str(sched_at_dt)
                sched_at_tr = str(sched_at_dt)

            source = str(row[3])
            provider_code = str(row[4]) if row[4] else None
            event_code = str(row[5]) if row[5] else None
            reference_period = str(row[6]) if row[6] else None
            source_event_id = str(row[7]) if row[7] else None
            source_url = str(row[8]) if row[8] else None
            carried_forward = bool(row[9])
            is_stale = bool(row[10])

            events.append(
                EconomicEvent(
                    name=name,
                    country=country,
                    scheduled_at=sched_at_utc,
                    scheduled_at_istanbul=sched_at_tr,
                    source=source,
                    provider_id=provider_code,
                    event_code=event_code,
                    reference_period=reference_period,
                    source_event_id=source_event_id,
                    source_url=source_url,
                    carried_forward=carried_forward,
                    stale=is_stale,
                )
            )
        return events
