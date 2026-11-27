"""Lambda entry point for the tracked Binance price refresh."""

import os
from datetime import datetime, timezone
from uuid import UUID

from app.core.database import get_db_connection
from app.jobs.binance_crypto_latest_prices_job import run_job
from app.models.job_tracking import JobRunContext


def _execution_key(event):
    if event.get("source") == "finbot.scheduler":
        schedule = event.get("schedule_arn")
        scheduled_time = event.get("scheduled_time")
        if not isinstance(schedule, str) or not schedule.startswith("arn:") or len(schedule) > 512:
            raise ValueError("A valid schedule ARN is required")
        if not isinstance(scheduled_time, str):
            raise ValueError("A scheduled time is required")
        try:
            timestamp = datetime.fromisoformat(scheduled_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Invalid scheduled time") from exc
        if timestamp.tzinfo is None:
            raise ValueError("Scheduled time requires a timezone")
        return f"schedule:{schedule}:{timestamp.astimezone(timezone.utc).isoformat()}"
    try:
        return f"manual:{UUID(str(event['request_id']))}"
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Live manual execution requires a UUID request_id") from exc


def lambda_handler(event, context):
    if not isinstance(event, dict):
        raise ValueError("Event must be an object")
    mode = os.environ.get("CRYPTO_JOB_MODE", "dry-run")
    if mode not in ("dry-run", "apply-db"):
        raise ValueError("Invalid CRYPTO_JOB_MODE")
    scheduled = event.get("source") == "finbot.scheduler"
    dry_run = mode == "dry-run"
    key = None if dry_run else _execution_key(event)
    run_context = JobRunContext(
        started_at=datetime.now(timezone.utc),
        trigger_type="scheduled" if scheduled else "manual",
        dry_run=dry_run,
    )
    # The transaction-scoped lock works with the transaction pooler. If the
    # invocation terminates, PostgreSQL rolls back its claim and releases it.
    with get_db_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", ("finbot:crypto-prices",))
                if not cur.fetchone()[0]:
                    raise RuntimeError("Crypto price job is already running; retry later")
                if key:
                    cur.execute(
                        """INSERT INTO private.job_execution_keys (job_name, execution_key, status)
                           VALUES (%s, %s, 'running')
                           ON CONFLICT (job_name, execution_key) DO UPDATE
                           SET status = 'running', attempt_count = private.job_execution_keys.attempt_count + 1,
                               started_at = clock_timestamp(), finished_at = NULL
                           WHERE private.job_execution_keys.status = 'failed'
                           RETURNING status""",
                        ("binance_crypto_latest_prices_job", key),
                    )
                    if cur.fetchone() is None:
                        return {"status": "skipped", "reason": "already_completed", "dry_run": False}
            result = run_job(["--dry-run" if dry_run else "--apply-db"], run_context=run_context)
            if key:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE private.job_execution_keys SET status = %s, finished_at = clock_timestamp() "
                        "WHERE job_name = %s AND execution_key = %s",
                        (result.status, "binance_crypto_latest_prices_job", key),
                    )
    if result.status == "failed":
        raise RuntimeError("Crypto price job failed; inspect the job logs.")
    return {"status": result.status, "dry_run": dry_run}
