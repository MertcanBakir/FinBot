"""Lambda entry point for fx_latest_price_job."""

from app.core.database import get_db_connection
from app.jobs.fx_latest_price_job import execute_tracked_job
from app.lambda_handlers.common import _execution_key as _execution_key
from app.lambda_handlers.common import run_tracked_lambda_job


def lambda_handler(event, context):
    return run_tracked_lambda_job(
        event, job_name="fx_latest_price_job", mode_name="FX_JOB_MODE",
        lock_key="finbot:fx-prices",
        busy_message="USDTRY job is already running; retry later",
        failure_message="USDTRY price job failed; inspect the job logs.",
        connection_factory=get_db_connection, execute_job=execute_tracked_job, lambda_context=context,
    )
