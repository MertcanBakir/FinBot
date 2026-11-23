"""Shared final step for refresh jobs after state has been prepared in memory."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

RefreshResult = TypeVar("RefreshResult")


async def sync_prepared_refresh(
    sync_function: Callable[..., tuple[str, str | None]],
    result: RefreshResult,
    **sync_kwargs: Any,
) -> RefreshResult:
    """Persist a prepared refresh and complete its database status consistently."""
    try:
        database_status, database_error = await asyncio.to_thread(
            sync_function,
            result=result,
            **sync_kwargs,
        )
    except AssertionError:
        raise
    except Exception as exc:
        logger.error("Unexpected error during database sync (error_type=%s)", type(exc).__name__)
        database_status = "failed"
        database_error = f"{type(exc).__name__}: database synchronization error"

    result.database_status = database_status
    result.database_error = database_error
    if database_status == "failed":
        logger.error("Database synchronization failed: %s", database_error)
    elif database_status == "success":
        logger.info("Database synchronization completed successfully.")
    return result
