from unittest.mock import MagicMock, Mock

import pytest

from app.services.price_sync_transaction import apply_price_sync_transaction


def _transaction_context():
    conn = MagicMock()
    conn_context = MagicMock()
    conn_context.__enter__.return_value = conn
    conn_context.__exit__.return_value = False
    transaction_context = MagicMock()
    transaction_context.__exit__.return_value = False
    conn.transaction.return_value = transaction_context
    factory = Mock(return_value=conn_context)
    return factory, conn, transaction_context


def test_price_sync_apply_revalidates_and_writes_atomically():
    factory, conn, transaction_context = _transaction_context()
    catalog = [MagicMock(id="active"), MagicMock(id="inactive")]
    stored_prices = {"active": {"price": 10}}
    catalog_reader = Mock(return_value=catalog)
    catalog_validator = Mock(return_value=(True, []))
    fingerprint = Mock(return_value="unchanged")
    stored_reader = Mock(return_value=stored_prices)
    upsert = Mock(return_value=(2, 3, 4))
    mark_stale = Mock(side_effect=[1, 1])

    result = apply_price_sync_transaction(
        connection_factory=factory,
        catalog_name="BIST",
        catalog_reader=catalog_reader,
        catalog_validator=catalog_validator,
        catalog_fingerprint=fingerprint,
        expected_fingerprint="unchanged",
        stored_price_reader=stored_reader,
        upsert_prices=upsert,
        mark_stale_prices=mark_stale,
        sync_items=["price"],
        missing_instruments=[catalog[0]],
        inactive_instruments=[catalog[1]],
        status="partial",
    )

    assert result.inserted_or_updated_count == 2
    assert result.unchanged_count == 3
    assert result.stale_skipped_count == 4
    assert result.price_not_published_stale_count == 1
    assert result.instrument_inactive_stale_count == 1
    transaction_context.__enter__.assert_called_once_with()
    transaction_context.__exit__.assert_called_once_with(None, None, None)
    upsert.assert_called_once_with(conn, ["price"], stored_prices)
    assert mark_stale.call_args_list[0].args == (conn, ["active"], "price_not_published", stored_prices)
    assert mark_stale.call_args_list[1].args == (conn, ["inactive"], "instrument_inactive", stored_prices)


def test_price_sync_catalog_drift_aborts_before_any_write():
    factory, _conn, transaction_context = _transaction_context()
    upsert = Mock()
    mark_stale = Mock()

    with pytest.raises(RuntimeError, match="catalog drifted"):
        apply_price_sync_transaction(
            connection_factory=factory,
            catalog_name="NASDAQ",
            catalog_reader=Mock(return_value=[]),
            catalog_validator=Mock(return_value=(True, [])),
            catalog_fingerprint=Mock(return_value="changed"),
            expected_fingerprint="original",
            stored_price_reader=Mock(),
            upsert_prices=upsert,
            mark_stale_prices=mark_stale,
            sync_items=["price"],
            missing_instruments=[],
            inactive_instruments=[],
            status="success",
        )

    upsert.assert_not_called()
    mark_stale.assert_not_called()
    transaction_context.__exit__.assert_called_once()
    assert transaction_context.__exit__.call_args.args[0] is RuntimeError
