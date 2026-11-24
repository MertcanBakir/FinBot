import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml

from app.jobs.binance_crypto_latest_prices_job import (
    run_job,
    sanitize_error_message,
)
from app.models.crypto_prices import (
    CryptoDbInstrument,
    CryptoNormalizedPrice,
    CryptoPriceSyncItem,
)
from app.providers.crypto.binance_price_provider import (
    BinancePriceFetchError,
    BinancePriceProvider,
)
from app.repositories.crypto_latest_prices_repository import (
    CryptoLatestPricesRepository,
)
from app.services.crypto_prices_service import (
    CryptoPricesService,
    compute_crypto_catalog_fingerprint,
    plan_crypto_price_upserts,
    validate_crypto_catalog,
)


def _make_sample_instruments(count: int = 100, active_count: int = 100) -> List[CryptoDbInstrument]:
    """Helper to generate a valid list of CryptoDbInstrument objects."""
    instruments: List[CryptoDbInstrument] = []
    for i in range(count):
        sym = f"COIN{i:03d}"
        instruments.append(
            CryptoDbInstrument(
                id=uuid4(),
                symbol=sym,
                company_name=f"Crypto {sym}",
                currency="USD",
                is_active=(i < active_count),
            )
        )
    return instruments


# ---------------------------------------------------------------------------
# 1. Catalog Validation Tests
# ---------------------------------------------------------------------------


def test_catalog_validation_requires_100_instruments():
    # 100 instruments -> Valid
    insts_100 = _make_sample_instruments(100)
    is_valid, errors = validate_crypto_catalog(insts_100)
    assert is_valid is True
    assert len(errors) == 0

    # 99 instruments -> Invalid
    insts_99 = _make_sample_instruments(99)
    is_valid, errors = validate_crypto_catalog(insts_99)
    assert is_valid is False
    assert any("Catalog count mismatch" in e for e in errors)

    # 101 instruments -> Invalid
    insts_101 = _make_sample_instruments(101)
    is_valid, errors = validate_crypto_catalog(insts_101)
    assert is_valid is False
    assert any("Catalog count mismatch" in e for e in errors)


def test_catalog_validation_rejects_duplicates_and_empty():
    insts = _make_sample_instruments(100)
    # Duplicate ID
    insts[1] = insts[1].model_copy(update={"id": insts[0].id})
    is_valid, errors = validate_crypto_catalog(insts)
    assert is_valid is False
    assert any("Duplicate instrument_id" in e for e in errors)

    # Duplicate Symbol
    insts = _make_sample_instruments(100)
    insts[1] = insts[1].model_copy(update={"symbol": insts[0].symbol})
    is_valid, errors = validate_crypto_catalog(insts)
    assert is_valid is False
    assert any("Duplicate symbol" in e for e in errors)

    # No active instruments
    insts_inactive = _make_sample_instruments(100, active_count=0)
    is_valid, errors = validate_crypto_catalog(insts_inactive)
    assert is_valid is False
    assert any("No active CRYPTO" in e for e in errors)


def test_catalog_validation_rejects_non_usd_currency():
    insts = _make_sample_instruments(100)
    insts[5] = insts[5].model_copy(update={"currency": "EUR"})
    is_valid, errors = validate_crypto_catalog(insts)
    assert is_valid is False
    assert any("Invalid currency 'EUR'" in e for e in errors)


def test_catalog_fingerprint_deterministic_and_sensitive_to_symbol_changes():
    insts1 = _make_sample_instruments(100)
    fp1 = compute_crypto_catalog_fingerprint(insts1)
    fp2 = compute_crypto_catalog_fingerprint(list(reversed(insts1)))
    # Reverse order produces exact same fingerprint because of deterministic sort
    assert fp1 == fp2

    # Changing a single symbol under the same UUID alters fingerprint
    insts_mutated = [i.model_copy() for i in insts1]
    insts_mutated[0] = insts_mutated[0].model_copy(update={"symbol": "MUTATED"})
    fp_mutated = compute_crypto_catalog_fingerprint(insts_mutated)
    assert fp1 != fp_mutated


# ---------------------------------------------------------------------------
# 2. Binance Price Provider Tests
# ---------------------------------------------------------------------------


def test_provider_formats_symbols_with_compact_json_and_symbol_status_trading():
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"symbol": "BTCUSDT", "lastPrice": "70000.0", "openPrice": "68000.0", "closeTime": 1787310000000},
        {"symbol": "币安人生USDT", "lastPrice": "0.50", "openPrice": "0.45", "closeTime": 1787310000000},
    ]
    mock_client.get.return_value = mock_resp

    provider = BinancePriceProvider(client=mock_client)
    pairs = ["BTCUSDT", "币安人生USDT"]
    res = provider.fetch_24hr_tickers(pairs)

    assert len(res) == 2
    mock_client.get.assert_called_once()
    called_params = mock_client.get.call_args.kwargs["params"]

    # Verify compact JSON without space after comma and unicode characters preserved
    assert called_params["symbols"] == '["BTCUSDT","币安人生USDT"]'
    assert called_params["type"] == "FULL"
    assert called_params["symbolStatus"] == "TRADING"

    # Verify NO auth/API keys are sent
    headers = mock_client.get.call_args.kwargs.get("headers") or {}
    assert "X-MBX-APIKEY" not in headers
    assert "Authorization" not in headers


def test_provider_single_batch_100_symbols():
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []
    mock_client.get.return_value = mock_resp

    provider = BinancePriceProvider(client=mock_client)
    pairs = [f"COIN{i:03d}USDT" for i in range(100)]
    provider.fetch_24hr_tickers(pairs)

    mock_client.get.assert_called_once()
    called_params = mock_client.get.call_args.kwargs["params"]
    assert "COIN000USDT" in called_params["symbols"]
    assert "COIN099USDT" in called_params["symbols"]


def test_provider_handles_errors_and_masks_secrets(caplog):
    caplog.set_level(logging.WARNING)

    # 1. HTTP 500 error
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_client.get.return_value = mock_resp

    provider = BinancePriceProvider(client=mock_client)
    with pytest.raises(BinancePriceFetchError) as exc_info:
        provider.fetch_24hr_tickers(["BTCUSDT"])
    assert "500" in str(exc_info.value)

    # 2. Network exception
    mock_client.get.side_effect = RuntimeError("Connection to https://user:SecretPass123@api.binance.com failed")
    with pytest.raises(BinancePriceFetchError):
        provider.fetch_24hr_tickers(["BTCUSDT"])

    assert "SecretPass123" not in caplog.text


# ---------------------------------------------------------------------------
# 3. Service Price Normalization & Matching Tests
# ---------------------------------------------------------------------------


def test_service_normalizes_tickers_and_calculates_decimal_changes():
    raw_tickers = [
        {
            "symbol": "BTCUSDT",
            "lastPrice": "77782.01000000",
            "openPrice": "71914.79000000",
            "closeTime": 1787310692009,
        },
        {
            "symbol": "ETHUSDT",
            "lastPrice": "2391.11000000",
            "openPrice": "2287.75000000",
            "closeTime": 1787310692001,
        },
    ]

    expected_pairs = {"BTCUSDT", "ETHUSDT"}
    valid_prices, warnings, inv_cnt, dup_cnt, unreq = CryptoPricesService.normalize_and_validate_tickers(
        raw_tickers, expected_pairs=expected_pairs
    )

    assert len(valid_prices) == 2
    assert inv_cnt == 0
    assert dup_cnt == 0
    assert len(unreq) == 0

    btc = valid_prices[0]
    assert btc.symbol == "BTC"
    assert btc.source_pair == "BTCUSDT"
    assert btc.price == Decimal("77782.010000000000")
    assert btc.previous_close == Decimal("71914.790000000000")
    assert btc.change_amount == Decimal("5867.220000000000")
    assert btc.change_percent == Decimal("8.15857211")
    assert btc.price_at.tzinfo == timezone.utc
    assert btc.currency == "USD"
    assert btc.provider_code == "BINANCE"


def test_service_rejects_non_positive_and_nan_prices():
    bad_tickers = [
        {"symbol": "ZERO_LAST", "lastPrice": "0.0", "openPrice": "10.0", "closeTime": 1787310000000},
        {"symbol": "NEG_OPEN", "lastPrice": "10.0", "openPrice": "-5.0", "closeTime": 1787310000000},
        {"symbol": "NAN_PRICE", "lastPrice": "NaN", "openPrice": "10.0", "closeTime": 1787310000000},
        {"symbol": "NEG_TIME", "lastPrice": "10.0", "openPrice": "10.0", "closeTime": -100},
    ]

    valid_prices, warnings, inv_cnt, dup_cnt, unreq = CryptoPricesService.normalize_and_validate_tickers(
        bad_tickers, expected_pairs={"ZERO_LAST", "NEG_OPEN", "NAN_PRICE", "NEG_TIME"}
    )

    assert len(valid_prices) == 0
    assert inv_cnt == 4
    assert dup_cnt == 0


def test_service_handles_extreme_overflow_close_time():
    overflow_ticker = [
        {
            "symbol": "BTCUSDT",
            "lastPrice": "70000.0",
            "openPrice": "68000.0",
            "closeTime": 10**100,  # Extreme overflow timestamp
        }
    ]

    valid_prices, warnings, inv_cnt, dup_cnt, unreq = CryptoPricesService.normalize_and_validate_tickers(
        overflow_ticker, expected_pairs={"BTCUSDT"}
    )

    assert len(valid_prices) == 0
    assert inv_cnt == 1
    assert any("invalid price or timestamp data" in w for w in warnings)


def test_service_handles_unrequested_and_duplicate_tickers():
    raw_tickers = [
        {"symbol": "BTCUSDT", "lastPrice": "70000.0", "openPrice": "68000.0", "closeTime": 1787310000000},
        {"symbol": "EXTRAUSDT", "lastPrice": "1.0", "openPrice": "1.0", "closeTime": 1787310000000},
        {"symbol": "BTCUSDT", "lastPrice": "71000.0", "openPrice": "68000.0", "closeTime": 1787310000000},
    ]

    valid_prices, warnings, inv_cnt, dup_cnt, unreq = CryptoPricesService.normalize_and_validate_tickers(
        raw_tickers, expected_pairs={"BTCUSDT"}
    )

    assert len(valid_prices) == 1
    assert valid_prices[0].symbol == "BTC"
    assert valid_prices[0].price == Decimal("70000.000000000000")
    assert dup_cnt == 1
    assert unreq == ["EXTRAUSDT"]
    assert any("Unrequested ticker" in w for w in warnings)


def test_match_prices_with_active_instruments():
    inst1 = CryptoDbInstrument(id=uuid4(), symbol="BTC", company_name="Bitcoin", is_active=True)
    inst2 = CryptoDbInstrument(id=uuid4(), symbol="ETH", company_name="Ethereum", is_active=True)
    inst3 = CryptoDbInstrument(id=uuid4(), symbol="SOL", company_name="Solana", is_active=True)

    t_now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    prices = [
        CryptoNormalizedPrice(
            symbol="BTC",
            source_pair="BTCUSDT",
            price=Decimal("70000.000000000000"),
            previous_close=Decimal("68000.000000000000"),
            change_amount=Decimal("2000.000000000000"),
            change_percent=Decimal("2.94117647"),
            price_at=t_now,
            currency="USD",
            provider_code="BINANCE",
        ),
        CryptoNormalizedPrice(
            symbol="ETH",
            source_pair="ETHUSDT",
            price=Decimal("2500.000000000000"),
            previous_close=Decimal("2400.000000000000"),
            change_amount=Decimal("100.000000000000"),
            change_percent=Decimal("4.16666667"),
            price_at=t_now,
            currency="USD",
            provider_code="BINANCE",
        ),
    ]

    matched, missing, unmatched = CryptoPricesService.match_prices_with_instruments(
        [inst1, inst2, inst3], prices, unrequested_pairs=["EXTRAUSDT"]
    )

    assert len(matched) == 2
    assert matched[0].symbol == "BTC"
    assert matched[1].symbol == "ETH"
    assert len(missing) == 1
    assert missing[0].symbol == "SOL"
    assert unmatched == ["EXTRAUSDT"]


# ---------------------------------------------------------------------------
# 4. Plan Crypto Price Upserts (Idempotent Merge Rules) Tests
# ---------------------------------------------------------------------------


def test_plan_crypto_price_upserts_idempotent_merge_rules():
    uid1 = uuid4()
    uid2 = uuid4()
    uid3 = uuid4()
    uid4 = uuid4()

    t_stored = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    t_newer = datetime(2026, 8, 21, 12, 17, tzinfo=timezone.utc)
    t_older = datetime(2026, 8, 21, 11, 45, tzinfo=timezone.utc)

    stored_map = {
        uid1: {
            "price": Decimal("70000.000000000000"),
            "currency": "USD",
            "previous_close": Decimal("68000.000000000000"),
            "change_amount": Decimal("2000.000000000000"),
            "change_percent": Decimal("2.94117647"),
            "price_at": t_stored,
            "provider_code": "BINANCE",
            "is_stale": False,
            "stale_reason": None,
        },
        uid2: {
            "price": Decimal("2500.000000000000"),
            "currency": "USD",
            "previous_close": Decimal("2400.000000000000"),
            "change_amount": Decimal("100.000000000000"),
            "change_percent": Decimal("4.16666667"),
            "price_at": t_stored,
            "provider_code": "BINANCE",
            "is_stale": False,
            "stale_reason": None,
        },
        uid3: {
            "price": Decimal("150.000000000000"),
            "currency": "USD",
            "previous_close": Decimal("140.000000000000"),
            "change_amount": Decimal("10.000000000000"),
            "change_percent": Decimal("7.14285714"),
            "price_at": t_stored,
            "provider_code": "BINANCE",
            "is_stale": True,
            "stale_reason": "price_not_published",
        },
    }

    incoming_items = [
        # 1. uid1: newer timestamp -> to_upsert
        CryptoPriceSyncItem(
            instrument_id=uid1,
            symbol="BTC",
            source_pair="BTCUSDT",
            price=Decimal("70500.000000000000"),
            currency="USD",
            previous_close=Decimal("68000.000000000000"),
            change_amount=Decimal("2500.000000000000"),
            change_percent=Decimal("3.67647059"),
            price_at=t_newer,
            provider_code="BINANCE",
        ),
        # 2. uid2: same timestamp, identical values -> unchanged (no-op)
        CryptoPriceSyncItem(
            instrument_id=uid2,
            symbol="ETH",
            source_pair="ETHUSDT",
            price=Decimal("2500.000000000000"),
            currency="USD",
            previous_close=Decimal("2400.000000000000"),
            change_amount=Decimal("100.000000000000"),
            change_percent=Decimal("4.16666667"),
            price_at=t_stored,
            provider_code="BINANCE",
        ),
        # 3. uid3: same timestamp, identical values BUT stored is stale -> to_upsert (clears stale)
        CryptoPriceSyncItem(
            instrument_id=uid3,
            symbol="SOL",
            source_pair="SOLUSDT",
            price=Decimal("150.000000000000"),
            currency="USD",
            previous_close=Decimal("140.000000000000"),
            change_amount=Decimal("10.000000000000"),
            change_percent=Decimal("7.14285714"),
            price_at=t_stored,
            provider_code="BINANCE",
        ),
        # 4. uid4: no stored row -> to_upsert
        CryptoPriceSyncItem(
            instrument_id=uid4,
            symbol="XRP",
            source_pair="XRPUSDT",
            price=Decimal("0.550000000000"),
            currency="USD",
            previous_close=Decimal("0.500000000000"),
            change_amount=Decimal("0.050000000000"),
            change_percent=Decimal("10.00000000"),
            price_at=t_stored,
            provider_code="BINANCE",
        ),
    ]

    to_upsert, unchanged_cnt, stale_skip_cnt = plan_crypto_price_upserts(stored_map, incoming_items)

    assert len(to_upsert) == 3  # uid1 (newer), uid3 (clears stale), uid4 (new)
    assert unchanged_cnt == 1   # uid2 (identical)
    assert stale_skip_cnt == 0

    # 5. Older timestamp test
    older_item = CryptoPriceSyncItem(
        instrument_id=uid1,
        symbol="BTC",
        source_pair="BTCUSDT",
        price=Decimal("69000.000000000000"),
        currency="USD",
        previous_close=Decimal("68000.000000000000"),
        change_amount=Decimal("1000.000000000000"),
        change_percent=Decimal("1.47058824"),
        price_at=t_older,
        provider_code="BINANCE",
    )
    to_up2, unch2, stale_skip2 = plan_crypto_price_upserts(stored_map, [older_item])
    assert len(to_up2) == 0
    assert unch2 == 0
    assert stale_skip2 == 1


# ---------------------------------------------------------------------------
# 5. Repository Stale Marking Tests
# ---------------------------------------------------------------------------


def test_mark_stale_prices_rejects_invalid_reason_without_sql():
    mock_conn = MagicMock()
    with pytest.raises(ValueError) as exc_info:
        CryptoLatestPricesRepository.mark_stale_prices(
            mock_conn, [uuid4()], reason="invalid_reason"  # type: ignore
        )
    assert "Invalid stale_reason" in str(exc_info.value)
    mock_conn.cursor.assert_not_called()


def test_mark_stale_prices_skips_instruments_without_existing_row():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    uid_with_row = uuid4()
    uid_without_row = uuid4()

    stored_map = {
        uid_with_row: {"is_stale": False, "stale_reason": None},
    }

    count = CryptoLatestPricesRepository.mark_stale_prices(
        mock_conn,
        [uid_with_row, uid_without_row],
        reason="price_not_published",
        stored_map=stored_map,
    )

    mock_conn.cursor.assert_called_once()
    assert count == 1  # Only uid_with_row was updated


# ---------------------------------------------------------------------------
# 6. End-to-End Orchestration & Completeness Boundary Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "active_count, matched_count, expected_status",
    [
        (100, 100, "success"),
        (100, 80, "partial"),
        (100, 79, "failed"),
        (100, 0, "failed"),
    ],
)
def test_completeness_boundary_conditions(active_count, matched_count, expected_status):
    insts = _make_sample_instruments(100, active_count=active_count)
    mock_provider = MagicMock()

    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": insts[i].source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for i in range(matched_count)
    ]

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", return_value={}):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
        assert res.status == expected_status
        assert res.matched_count == matched_count
        assert res.requested_active_count == active_count


def test_response_with_100_matched_plus_duplicate_becomes_partial():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    tickers = [
        {
            "symbol": inst.source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for inst in insts
    ]
    # Add a duplicate ticker
    tickers.append(tickers[0])
    mock_provider.fetch_24hr_tickers.return_value = tickers

    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", return_value={}):

        res = run_job(["--dry-run"], provider=mock_provider)
        assert res.matched_count == 100
        assert res.duplicate_count == 1
        assert res.status == "partial"


def test_response_with_100_matched_plus_unrequested_becomes_partial():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    tickers = [
        {
            "symbol": inst.source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for inst in insts
    ]
    # Add an unrequested ticker
    tickers.append({"symbol": "EXTRA_UNKNOWN_USDT", "lastPrice": "5.0", "openPrice": "4.0", "closeTime": t_now})
    mock_provider.fetch_24hr_tickers.return_value = tickers

    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", return_value={}):

        res = run_job(["--dry-run"], provider=mock_provider)
        assert res.matched_count == 100
        assert res.unmatched_count == 1
        assert res.status == "partial"


def test_dry_run_executes_zero_writes_and_zero_emails():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": inst.source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for inst in insts
    ]

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(CryptoLatestPricesRepository, "upsert_prices") as mock_upsert, \
         patch.object(CryptoLatestPricesRepository, "mark_stale_prices") as mock_stale:

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)

        assert res.status == "success"
        assert res.dry_run is True
        mock_upsert.assert_not_called()
        mock_stale.assert_not_called()
        mock_email.send_email.assert_not_called()


def test_dry_run_db_read_error_fails_closed_safely():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": inst.source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for inst in insts
    ]

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", side_effect=RuntimeError("DB read failed with token=Secret123")):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert res.dry_run is True
        assert "Secret123" not in (res.error or "")
        mock_email.send_email.assert_not_called()


def test_apply_db_success_sends_no_email():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": inst.source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for inst in insts
    ]

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(CryptoLatestPricesRepository, "upsert_prices", return_value=(100, 0, 0)) as mock_upsert:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "success"
        assert res.dry_run is False
        assert res.email_status is None
        mock_upsert.assert_called_once()
        mock_email.send_email.assert_not_called()


def test_apply_db_partial_sends_warning_email_and_marks_missing_stale():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    # 85 matched out of 100 -> partial (85%)
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": insts[i].source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for i in range(85)
    ]

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(CryptoLatestPricesRepository, "upsert_prices", return_value=(85, 0, 0)) as mock_upsert, \
         patch.object(CryptoLatestPricesRepository, "mark_stale_prices", return_value=15) as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "partial"
        assert res.matched_count == 85
        assert res.missing_count == 15
        assert res.email_status == "sent"
        mock_upsert.assert_called_once()
        mock_stale.assert_called_once()
        mock_email.send_email.assert_called_once()
        call_args = mock_email.send_email.call_args.kwargs
        assert "Kısmi güncelleme" in call_args["subject"]


def test_apply_db_fails_closed_below_80_percent_and_sends_error_email():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    # 70 matched out of 100 -> failed (70% < 80%)
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": insts[i].source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for i in range(70)
    ]

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(CryptoLatestPricesRepository, "upsert_prices") as mock_upsert, \
         patch.object(CryptoLatestPricesRepository, "mark_stale_prices") as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert res.matched_count == 70
        assert res.email_status == "sent"
        mock_upsert.assert_not_called()
        mock_stale.assert_not_called()
        mock_email.send_email.assert_called_once()
        call_args = mock_email.send_email.call_args.kwargs
        assert "Güncelleme başarısız" in call_args["subject"]


def test_apply_db_fails_closed_on_catalog_drift_with_symbol_change():
    insts_before = _make_sample_instruments(100)
    # Same UUIDs, but 1 symbol modified
    insts_after = [i.model_copy() for i in insts_before]
    insts_after[0] = insts_after[0].model_copy(update={"symbol": "DRIFTED_COIN"})

    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": inst.source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for inst in insts_before
    ]

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", side_effect=[insts_before, insts_after]), \
         patch.object(CryptoLatestPricesRepository, "upsert_prices") as mock_upsert, \
         patch.object(CryptoLatestPricesRepository, "mark_stale_prices") as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert "Safety check failed" in (res.error or "")
        mock_upsert.assert_not_called()
        mock_stale.assert_not_called()
        mock_email.send_email.assert_called_once()


def test_stateful_transaction_rollback_when_stale_marking_fails():
    class StatefulMockConn:
        def __init__(self):
            self.transactions = []
            self.in_tx = False

        def cursor(self):
            from tests.conftest import configure_mock_db_cursor
            return configure_mock_db_cursor(MagicMock())

        def transaction(self):
            conn = self
            class Tx:
                def __init__(self):
                    self.status = "open"
                def __enter__(self):
                    conn.in_tx = True
                    conn.transactions.append(self)
                    return conn
                def __exit__(self, exc_type, exc_val, exc_tb):
                    conn.in_tx = False
                    if exc_type is not None:
                        self.status = "rolled_back"
                    else:
                        self.status = "committed"
                    return False
            return Tx()

    stateful_conn = StatefulMockConn()
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    # 85 matched -> partial -> requires mark_stale_prices
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": insts[i].source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for i in range(85)
    ]

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = stateful_conn

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts), \
         patch.object(CryptoLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(CryptoLatestPricesRepository, "upsert_prices", return_value=(85, 0, 0)), \
         patch.object(CryptoLatestPricesRepository, "mark_stale_prices", side_effect=RuntimeError("SQL stale update failed with token=SecretKey789")):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert any(tx.status == "rolled_back" for tx in stateful_conn.transactions)
        mock_email.send_email.assert_called_once()
        assert "SecretKey789" not in (res.error or "")


def test_sanitize_error_message_masks_secrets():
    exc1 = RuntimeError("Connection failed to postgresql://user:SecretDbPass123@supabase.com:5432/db")
    assert sanitize_error_message(exc1) == "Veritabanı veya servis erişim hatası."

    exc2 = TimeoutError("HTTP request timed out connecting to https://data-api.binance.vision")
    assert sanitize_error_message(exc2) == "Veritabanı veya servis erişim hatası."

    exc3 = ValueError("Catalog count mismatch: expected 100 instruments, found 90")
    assert sanitize_error_message(exc3) == "Catalog count mismatch: expected 100 instruments, found 90"


def test_smtp_exception_handling_masks_secrets(caplog):
    caplog.set_level(logging.ERROR)
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    mock_provider.fetch_24hr_tickers.side_effect = RuntimeError("Binance API down")

    mock_email = MagicMock()
    mock_email.send_email.side_effect = RuntimeError("SMTP failed with token=SecretSmtpToken999")
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=insts):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert res.email_status == "failed"
        assert res.email_error == "E-posta gönderimi sırasında hata oluştu."
        assert "SecretSmtpToken999" not in caplog.text


def test_github_actions_cron_schedule():
    workflow_path = ".github/workflows/binance-crypto-latest-prices.yml"
    with open(workflow_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Validate schedule cron expression
    on_section = data.get("on") or data.get(True)
    cron_expr = on_section["schedule"][0]["cron"]
    assert cron_expr == "17 * * * *"


def test_crypto_provider_request_executed_lifecycle_paths():
    """Verify job-level provider_request_executed lifecycle paths for Binance Crypto price job."""
    mock_provider = MagicMock()
    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    # 1. Pre-provider catalog error -> provider_request_executed is False
    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=[]):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
        assert res.status == "failed"
        assert res.provider_request_executed is False

    valid_catalog = _make_sample_instruments(100)

    # 2. Provider fetch raises exception -> provider_request_executed is True
    mock_provider.fetch_24hr_tickers.side_effect = RuntimeError("Binance API network drop")

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=valid_catalog):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
        assert res.status == "failed"
        assert res.provider_request_executed is True

    # 3. Provider fetch succeeds -> provider_request_executed is True
    t_now = int(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    mock_provider.fetch_24hr_tickers.side_effect = None
    mock_provider.fetch_24hr_tickers.return_value = [
        {
            "symbol": inst.source_pair,
            "lastPrice": "100.0",
            "openPrice": "95.0",
            "closeTime": t_now,
        }
        for inst in valid_catalog
    ]

    with patch("app.jobs.binance_crypto_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(CryptoLatestPricesRepository, "get_all_crypto_instruments", return_value=valid_catalog):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
        assert res.status == "success"
        assert res.provider_request_executed is True
