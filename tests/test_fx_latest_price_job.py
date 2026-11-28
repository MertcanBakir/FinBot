from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID

from app.jobs import fx_latest_price_job
from app.models.fx_prices import FxPrice, FxPriceJobResult
from app.services.job_tracking.adapters.fx_adapter import FxPriceResultAdapter

INSTRUMENT_ID = UUID("20000000-0000-0000-0000-000000000099")
INSTRUMENT = {
    "id": INSTRUMENT_ID,
    "market": "FX",
    "symbol": "USDTRY",
    "company_name": "USD/TRY",
    "is_active": True,
    "asset_type": "fx",
    "currency": "TRY",
}


def _connection() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.transaction.return_value.__enter__.return_value = conn
    return conn


def _price(previous_close: Decimal | None = Decimal("40")) -> FxPrice:
    return FxPrice(
        instrument_id=INSTRUMENT_ID,
        price=Decimal("40.50"),
        previous_close=previous_close,
        change_amount=Decimal("0.50") if previous_close is not None else None,
        change_percent=Decimal("1.25") if previous_close is not None else None,
        price_at=datetime(2026, 8, 25, 20, tzinfo=timezone.utc),
    )


def test_fx_job_dry_run_fetches_and_normalizes_without_writing(monkeypatch):
    conn = _connection()
    provider = MagicMock()
    provider.fetch.return_value = (MagicMock(), MagicMock())
    service = MagicMock()
    service.normalize.return_value = _price()

    monkeypatch.setattr(fx_latest_price_job, "get_db_connection", lambda: conn)
    monkeypatch.setattr(
        fx_latest_price_job.FxLatestPriceRepository,
        "get_instrument",
        lambda _conn: INSTRUMENT,
    )
    monkeypatch.setattr(
        fx_latest_price_job.FxLatestPriceRepository,
        "get_stored",
        lambda _conn, _instrument_id: None,
    )
    monkeypatch.setattr(
        fx_latest_price_job.FxLatestPriceRepository,
        "is_unchanged",
        lambda _stored, _item: False,
    )
    upsert = MagicMock()
    monkeypatch.setattr(fx_latest_price_job.FxLatestPriceRepository, "upsert", upsert)

    result = fx_latest_price_job._execute_fx_price_job(
        dry_run=True,
        provider=provider,
        service=service,
    )

    assert result.status == "success"
    assert result.dry_run is True
    assert result.provider_request_executed is True
    assert result.database_write_executed is False
    assert result.database_writes == 0
    upsert.assert_not_called()


def test_fx_job_apply_writes_only_after_catalog_recheck(monkeypatch):
    read_conn = _connection()
    write_conn = _connection()
    connections = iter([read_conn, write_conn])
    provider = MagicMock()
    provider.fetch.return_value = (MagicMock(), MagicMock())
    service = MagicMock()
    service.normalize.return_value = _price(previous_close=None)

    monkeypatch.setattr(
        fx_latest_price_job, "get_db_connection", lambda: next(connections)
    )
    monkeypatch.setattr(
        fx_latest_price_job.FxLatestPriceRepository,
        "get_instrument",
        lambda _conn: INSTRUMENT,
    )
    monkeypatch.setattr(
        fx_latest_price_job.FxLatestPriceRepository,
        "get_stored",
        lambda _conn, _instrument_id: None,
    )
    monkeypatch.setattr(
        fx_latest_price_job.FxLatestPriceRepository,
        "is_unchanged",
        lambda _stored, _item: False,
    )
    monkeypatch.setattr(
        fx_latest_price_job.FxLatestPriceRepository,
        "upsert",
        lambda _conn, _item: 1,
    )

    result = fx_latest_price_job._execute_fx_price_job(
        dry_run=False,
        provider=provider,
        service=service,
    )

    assert result.status == "partial"
    assert result.dry_run is False
    assert result.database_writes == 1
    assert result.database_write_executed is True
    write_conn.transaction.assert_called_once_with()


def test_fx_job_fails_closed_before_provider_when_catalog_is_invalid(monkeypatch):
    conn = _connection()
    provider = MagicMock()

    monkeypatch.setattr(fx_latest_price_job, "get_db_connection", lambda: conn)
    monkeypatch.setattr(
        fx_latest_price_job.FxLatestPriceRepository,
        "get_instrument",
        lambda _conn: {**INSTRUMENT, "is_active": False},
    )

    result = fx_latest_price_job._execute_fx_price_job(
        dry_run=False,
        provider=provider,
        service=MagicMock(),
    )

    assert result.status == "failed"
    assert result.provider_request_executed is False
    assert result.database_write_executed is False
    assert result.error == "USDTRY fiyatı güvenli biçimde güncellenemedi."
    provider.fetch.assert_not_called()


def test_fx_adapter_reports_dry_run_noop_update_and_failure_actions():
    adapter = FxPriceResultAdapter()
    common = {
        "instrument_id": INSTRUMENT_ID,
        "price": Decimal("40.50"),
        "previous_close": Decimal("40"),
        "price_at": datetime(2026, 8, 25, 20, tzinfo=timezone.utc),
    }

    dry_run = adapter.to_outcome(
        FxPriceJobResult(status="success", dry_run=True, **common)
    )
    no_op = adapter.to_outcome(
        FxPriceJobResult(status="success", dry_run=False, was_unchanged=True, **common)
    )
    updated = adapter.to_outcome(
        FxPriceJobResult(status="success", dry_run=False, database_writes=1, **common)
    )
    failed = adapter.to_outcome(
        FxPriceJobResult(status="failed", dry_run=False, error="safe failure")
    )

    assert dry_run.sync_action == "dry_run"
    assert no_op.sync_action == "no_op"
    assert updated.sync_action == "updated"
    assert updated.metrics["database_write_executed"] is True
    assert updated.metrics["inserted_or_updated_count"] == 1
    assert failed.sync_action == "failed_no_write"
    assert failed.metrics["missing_count"] == 1
