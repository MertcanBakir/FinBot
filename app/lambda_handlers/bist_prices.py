"""Lambda entry point for bist_latest_prices_job."""

from app.core.database import get_db_connection
from app.jobs.bist_latest_prices_job import execute_tracked_job
from app.lambda_handlers.common import _execution_key as _execution_key
from app.lambda_handlers.common import run_tracked_lambda_job


def lambda_handler(event, context):
    return run_tracked_lambda_job(
        event, job_name="bist_latest_prices_job", mode_name="BIST_JOB_MODE",
        busy_message="BIST job is already running; retry later",
        failure_message="BIST price job failed; inspect the job logs.",
        connection_factory=get_db_connection, execute_job=execute_tracked_job, lambda_context=context,
    )
