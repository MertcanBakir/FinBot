import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Sequence
from uuid import UUID

from psycopg import Connection

from app.core.error_sanitization import (
    sanitize_tracking_error as sanitize_tracking_error,
)
from app.models.job_tracking import (
    DuplicateJobRunError,
    JobRunContext,
    JobRunOutcome,
    JobRunSpecification,
    JobTrackingFinalizeError,
    JobTrackingNotificationError,
    JobTrackingStartError,
    NotificationStatus,
    ProviderRunOutcome,
)

logger = logging.getLogger(__name__)

SENSITIVE_KEY_NAMES = {
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "credential", "credentials", "authorization",
    "smtp_password", "database_url", "db_url", "dsn"
}


def sanitize_recursive(val: Any) -> Any:
    """Recursively sanitize string values inside lists, dictionaries, or standalone values."""
    if isinstance(val, str):
        return sanitize_tracking_error(val) or ""
    elif isinstance(val, list):
        return [sanitize_recursive(item) for item in val]
    elif isinstance(val, dict):
        cleaned = {}
        for k, v in val.items():
            k_lower = str(k).lower().strip()
            if any(sens in k_lower for sens in SENSITIVE_KEY_NAMES):
                cleaned[k] = "***MASKED***"
            else:
                cleaned[k] = sanitize_recursive(v)
        return cleaned
    return val


class JobTrackingRepository:
    """Repository for centralized job lifecycle tracking, provider telemetry, and notification observability."""

    @staticmethod
    def get_provider_id_map(conn: Connection) -> Dict[str, UUID]:
        """Fetch data_providers mapping code -> UUID. Fails closed on database errors."""
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT code, id FROM public.data_providers WHERE is_active = true;")
                rows = cur.fetchall()
                if not rows or not isinstance(rows, (list, tuple)):
                    raise JobTrackingStartError("data_providers table is empty or inaccessible")
                res: Dict[str, UUID] = {}
                for row in rows:
                    if row and len(row) >= 2:
                        code = str(row[0]).strip().upper()
                        val = row[1]
                        if isinstance(val, UUID):
                            res[code] = val
                        else:
                            try:
                                res[code] = UUID(str(val))
                            except Exception as exc:
                                raise JobTrackingStartError(f"Invalid UUID in data_providers for {code}: {val}") from exc
                if not res:
                    raise JobTrackingStartError("data_providers returned 0 valid records")
                return res
        except JobTrackingStartError:
            raise
        except Exception as exc:
            logger.error("Failed to query data_providers (error_type=%s)", type(exc).__name__)
            raise JobTrackingStartError(f"Failed to query data_providers: {type(exc).__name__}") from exc

    @classmethod
    def start_run(
        cls,
        conn: Connection,
        spec: JobRunSpecification,
        context: JobRunContext,
    ) -> UUID:
        """Create a new 'running' execution record in refresh_runs. Fails closed on any error."""
        provider_id: Optional[UUID] = None
        if spec.provider_code:
            prov_map = cls.get_provider_id_map(conn)
            code_key = spec.provider_code.strip().upper()
            provider_id = prov_map.get(code_key)
            if provider_id is None:
                raise JobTrackingStartError(f"Provider code '{spec.provider_code}' not found in data_providers")

        query = """
            INSERT INTO public.refresh_runs (
                job_name, operation_type, scope, provider_id,
                started_at, trigger_type, status,
                github_run_id, github_run_attempt,
                notification_status, metrics, warnings
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, 'running',
                %s, %s,
                'not_required', '{}'::jsonb, '[]'::jsonb
            )
            ON CONFLICT (job_name, github_run_id, github_run_attempt)
            WHERE (github_run_id IS NOT NULL AND github_run_attempt IS NOT NULL)
            DO NOTHING
            RETURNING id;
        """
        try:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        spec.job_name,
                        spec.operation_type,
                        spec.scope,
                        provider_id,
                        context.started_at,
                        context.trigger_type,
                        context.github_run_id,
                        context.github_run_attempt,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    if context.github_run_id and context.github_run_attempt is not None:
                        raise DuplicateJobRunError(
                            f"Duplicate GitHub run detected for job '{spec.job_name}' "
                            f"(github_run_id={context.github_run_id}, attempt={context.github_run_attempt})"
                        )
                    raise JobTrackingStartError("Failed to insert refresh_runs record: no ID returned")

                raw_id = row[0]
                if isinstance(raw_id, UUID):
                    return raw_id
                try:
                    return UUID(str(raw_id))
                except Exception as exc:
                    raise JobTrackingStartError(f"Invalid UUID returned from database: {raw_id}") from exc
        except (DuplicateJobRunError, JobTrackingStartError):
            raise
        except Exception as exc:
            logger.error("Failed to start job tracking in database (error_type=%s)", type(exc).__name__)
            raise JobTrackingStartError(f"Database error during start_run: {type(exc).__name__}") from exc

    @staticmethod
    def finalize_run(
        conn: Connection,
        run_id: UUID,
        outcome: JobRunOutcome,
        notification_status: NotificationStatus = "not_required",
        notification_error: Optional[str] = None,
        notified_at: Optional[datetime] = None,
    ) -> bool:
        """Update refresh_runs record to final state (success/partial/failed/skipped) with sanitized error.

        Only updates if current state is 'running'. Returns True if updated, False otherwise.
        """
        clean_error = sanitize_tracking_error(outcome.error)
        clean_notif_error = sanitize_tracking_error(notification_error)
        clean_warnings = sanitize_recursive(outcome.warnings or [])
        clean_metrics = sanitize_recursive(outcome.metrics or {})

        fresh_cnt = outcome.fresh_event_count if outcome.fresh_event_count is not None else 0
        final_cnt = outcome.final_event_count if outcome.final_event_count is not None else 0

        query = """
            UPDATE public.refresh_runs
            SET
                status = %s,
                sync_action = %s,
                skip_reason = %s,
                finished_at = %s,
                coverage_start = %s,
                coverage_end = %s,
                fresh_event_count = %s,
                final_event_count = %s,
                warnings = %s::jsonb,
                metrics = %s::jsonb,
                error = %s,
                notification_status = %s,
                notification_error = %s,
                notified_at = %s,
                updated_at = clock_timestamp()
            WHERE id = %s AND status = 'running';
        """
        try:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        outcome.status,
                        outcome.sync_action,
                        outcome.skip_reason,
                        outcome.finished_at,
                        outcome.coverage_start,
                        outcome.coverage_end,
                        fresh_cnt,
                        final_cnt,
                        json.dumps(clean_warnings),
                        json.dumps(clean_metrics),
                        clean_error,
                        notification_status,
                        clean_notif_error,
                        notified_at,
                        run_id,
                    ),
                )
                rowcount = cur.rowcount
                return bool(isinstance(rowcount, int) and rowcount > 0)
        except Exception as exc:
            logger.error("Failed to finalize run '%s' in database (error_type=%s)", run_id, type(exc).__name__)
            raise JobTrackingFinalizeError(f"Database error during finalize_run: {type(exc).__name__}") from exc

    @classmethod
    def upsert_provider_results(
        cls,
        conn: Connection,
        run_id: UUID,
        outcomes: Sequence[ProviderRunOutcome],
    ) -> None:
        """Atomically upsert provider execution records with ON CONFLICT (refresh_run_id, provider_id, scope)."""
        if not outcomes:
            return

        prov_map = cls.get_provider_id_map(conn)

        query = """
            INSERT INTO public.provider_run_results (
                refresh_run_id, provider_id, scope, status,
                structure_validated, observed_count, candidate_count,
                fresh_event_count, carried_forward_count, malformed_count,
                retry_count, warnings, error, metrics
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s::jsonb, %s, %s::jsonb
            )
            ON CONFLICT (refresh_run_id, provider_id, scope)
            DO UPDATE SET
                status = EXCLUDED.status,
                structure_validated = EXCLUDED.structure_validated,
                observed_count = EXCLUDED.observed_count,
                candidate_count = EXCLUDED.candidate_count,
                fresh_event_count = EXCLUDED.fresh_event_count,
                carried_forward_count = EXCLUDED.carried_forward_count,
                malformed_count = EXCLUDED.malformed_count,
                retry_count = EXCLUDED.retry_count,
                warnings = EXCLUDED.warnings,
                error = EXCLUDED.error,
                metrics = EXCLUDED.metrics;
        """
        try:
            with conn.cursor() as cur:
                for out in outcomes:
                    code_key = out.provider_code.strip().upper()
                    prov_id = prov_map.get(code_key)
                    if not prov_id:
                        raise JobTrackingFinalizeError(f"Unknown provider_code '{out.provider_code}' in provider outcome")
                    clean_err = sanitize_tracking_error(out.error)
                    clean_warnings = sanitize_recursive(out.warnings or [])
                    clean_metrics = sanitize_recursive(out.metrics or {})
                    cur.execute(
                        query,
                        (
                            run_id,
                            prov_id,
                            out.scope,
                            out.status,
                            out.structure_validated,
                            out.observed_count,
                            out.candidate_count,
                            out.fresh_event_count,
                            out.carried_forward_count,
                            out.malformed_count,
                            out.retry_count,
                            json.dumps(clean_warnings),
                            clean_err,
                            json.dumps(clean_metrics),
                        ),
                    )
        except JobTrackingFinalizeError:
            raise
        except Exception as exc:
            logger.error("Database error during upsert_provider_results (error_type=%s)", type(exc).__name__)
            raise JobTrackingFinalizeError(f"Database error in upsert_provider_results: {type(exc).__name__}") from exc

    @classmethod
    def update_notification_status(
        cls,
        conn: Connection,
        run_id: UUID,
        status: NotificationStatus,
        error: Optional[str] = None,
        notified_at: Optional[datetime] = None,
    ) -> bool:
        """Update notification status and notification timestamp/error for a finalized run."""
        clean_err = sanitize_tracking_error(error)
        query = """
            UPDATE public.refresh_runs
            SET
                notification_status = %s,
                notification_error = %s,
                notified_at = %s,
                updated_at = clock_timestamp()
            WHERE id = %s AND status != 'running';
        """
        try:
            with conn.cursor() as cur:
                cur.execute(query, (status, clean_err, notified_at, run_id))
                rowcount = cur.rowcount
                return bool(isinstance(rowcount, int) and rowcount > 0)
        except Exception as exc:
            logger.error("Failed to update notification status in database (error_type=%s)", type(exc).__name__)
            raise JobTrackingNotificationError(f"Database error in update_notification_status: {type(exc).__name__}") from exc

    @classmethod
    def get_run(cls, conn: Connection, run_id: UUID) -> Optional[Dict[str, Any]]:
        """Fetch complete refresh_run record for verification and testing."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, job_name, operation_type, scope, provider_id,
                    trigger_type, target_year, started_at, finished_at,
                    coverage_start, coverage_end, status, sync_action, skip_reason,
                    fresh_event_count, final_event_count, warnings, metrics, error,
                    notification_status, notification_error, notified_at,
                    github_run_id, github_run_attempt, created_at, updated_at
                FROM public.refresh_runs
                WHERE id = %s;
                """,
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "job_name": row[1],
                "operation_type": row[2],
                "scope": row[3],
                "provider_id": row[4],
                "trigger_type": row[5],
                "target_year": row[6],
                "started_at": row[7],
                "finished_at": row[8],
                "coverage_start": row[9],
                "coverage_end": row[10],
                "status": row[11],
                "sync_action": row[12],
                "skip_reason": row[13],
                "fresh_event_count": row[14],
                "final_event_count": row[15],
                "warnings": row[16],
                "metrics": row[17],
                "error": row[18],
                "notification_status": row[19],
                "notification_error": row[20],
                "notified_at": row[21],
                "github_run_id": row[22],
                "github_run_attempt": row[23],
                "created_at": row[24],
                "updated_at": row[25],
            }
