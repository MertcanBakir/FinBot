from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.prepared_refresh_sync import sync_prepared_refresh


@pytest.mark.asyncio
async def test_sync_prepared_refresh_persists_then_completes_result():
    result = SimpleNamespace(database_status="pending", database_error=None)
    sync_function = Mock(return_value=("success", None))

    completed = await sync_prepared_refresh(
        sync_function,
        result,
        final_calendar="prepared-state",
        refresh_run_id="run-id",
    )

    assert completed is result
    assert result.database_status == "success"
    assert result.database_error is None
    sync_function.assert_called_once_with(
        result=result,
        final_calendar="prepared-state",
        refresh_run_id="run-id",
    )


@pytest.mark.asyncio
async def test_sync_prepared_refresh_records_unexpected_sync_failure():
    result = SimpleNamespace(database_status="pending", database_error=None)
    sync_function = Mock(side_effect=OSError("database unavailable"))

    completed = await sync_prepared_refresh(sync_function, result)

    assert completed is result
    assert result.database_status == "failed"
    assert result.database_error == "OSError: database synchronization error"
