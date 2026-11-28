"""Shared Lambda execution, concurrency, and idempotency handling."""

import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.models.job_tracking import JobRunContext


def _execution_key(event: dict) -> str:
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


def run_tracked_lambda_job(event, *, job_name, mode_name, busy_message, failure_message, connection_factory, execute_job, lambda_context=None):
    if not isinstance(event, dict):
        raise ValueError("Event must be an object")
    mode = os.environ.get(mode_name, "dry-run")
    if mode not in ("dry-run", "apply-db"):
        raise ValueError(f"Invalid {mode_name}")
    dry_run = mode == "dry-run"
    key = None if dry_run else _execution_key(event)
    run_context = JobRunContext(
        started_at=datetime.now(timezone.utc),
        trigger_type="scheduled" if event.get("source") == "finbot.scheduler" else "manual",
        dry_run=dry_run,
    )

    # AWS supplies the invocation deadline; retain a safety margin so a new
    # owner cannot overlap a Lambda that is still allowed to execute.
    remaining = lambda_context.get_remaining_time_in_millis() / 1000 if lambda_context else 7200
    lease_seconds = max(1, remaining) + 120
    owner = uuid4()
    with connection_factory() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO private.lambda_job_leases (job_name, owner_id, expires_at)
                       VALUES (%s, %s, clock_timestamp() + %s * interval '1 second')
                       ON CONFLICT (job_name) DO UPDATE
                       SET owner_id = EXCLUDED.owner_id, expires_at = EXCLUDED.expires_at
                       WHERE private.lambda_job_leases.expires_at <= clock_timestamp()
                       RETURNING owner_id""", (job_name, owner, lease_seconds),
                )
                if cur.fetchone() is None:
                    raise RuntimeError(busy_message)
                # Any remaining running record belongs to a previous owner that
                # timed out before recording its outcome.
                cur.execute(
                    "UPDATE private.job_execution_keys SET status = 'failed', finished_at = clock_timestamp() "
                    "WHERE job_name = %s AND status = 'running'", (job_name,),
                )
                if key:
                    cur.execute(
                        """INSERT INTO private.job_execution_keys (job_name, execution_key, status)
                           VALUES (%s, %s, 'running')
                           ON CONFLICT (job_name, execution_key) DO UPDATE
                           SET status = 'running', attempt_count = private.job_execution_keys.attempt_count + 1,
                               started_at = clock_timestamp(), finished_at = NULL
                           WHERE private.job_execution_keys.status = 'failed'
                           RETURNING status""", (job_name, key),
                    )
                    if cur.fetchone() is None:
                        cur.execute("DELETE FROM private.lambda_job_leases WHERE job_name = %s AND owner_id = %s", (job_name, owner))
                        return {"status": "skipped", "reason": "already_completed", "dry_run": False}

    try:
        result = execute_job(dry_run=dry_run, run_context=run_context)
    except BaseException:
        _finish_lease(connection_factory, job_name, key, owner, "failed")
        raise
    _finish_lease(connection_factory, job_name, key, owner, result.status)
    if result.status == "failed":
        raise RuntimeError(failure_message)
    response = {"status": result.status, "dry_run": dry_run}
    if result.status == "skipped" and getattr(result, "skip_reason", None) is not None:
        response["reason"] = result.skip_reason
    return response


def _finish_lease(connection_factory, job_name, key, owner, status):
    with connection_factory() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # Lock and verify the owner before changing either record.
                cur.execute(
                    "SELECT owner_id FROM private.lambda_job_leases WHERE job_name = %s AND owner_id = %s FOR UPDATE",
                    (job_name, owner),
                )
                if cur.fetchone() is None:
                    raise RuntimeError("Lambda job lease ownership was lost")
                if key:
                    cur.execute(
                        "UPDATE private.job_execution_keys SET status = %s, finished_at = clock_timestamp() "
                        "WHERE job_name = %s AND execution_key = %s AND status = 'running'",
                        (status, job_name, key),
                    )
                cur.execute("DELETE FROM private.lambda_job_leases WHERE job_name = %s AND owner_id = %s", (job_name, owner))
