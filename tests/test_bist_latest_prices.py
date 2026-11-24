import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List
from unittest.mock import MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from app.jobs.bist_latest_prices_job import (
    run_job,
    sanitize_error_message,
)
from app.models.bist_prices import (
    BistDbInstrument,
    BistNormalizedPrice,
    BistPriceSyncItem,
    XistSessionInfo,
)
from app.providers.bist.yahoo_bist_provider import (
    YahooBistCalendarError,
    YahooBistPriceFetchError,
    YahooBistPriceProvider,
)
from app.repositories.bist_latest_prices_repository import BistLatestPricesRepository
from app.services.bist_prices_service import (
    BistPricesService,
    compute_bist_catalog_fingerprint,
    extract_ticker_dataframe,
    is_price_bar_fresh,
    plan_bist_price_upserts,
    validate_bist_catalog,
)


def _make_sample_instruments(count: int = 100, active_count: int = 100) -> List[BistDbInstrument]:
    """Helper to generate a valid list of BistDbInstrument objects."""
    instruments: List[BistDbInstrument] = []
    for i in range(count):
        sym = f"STOCK{i:03d}"
        instruments.append(
            BistDbInstrument(
                id=uuid4(),
                symbol=sym,
                company_name=f"Company {sym}",
                currency="TRY",
                is_active=(i < active_count),
            )
        )
    return instruments


def _make_sample_dataframes(
    instruments: List[BistDbInstrument],
    trading_date: date,
    include_prev_close: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Helper to generate sample intraday and daily DataFrames for BIST instruments."""
    ist_tz = ZoneInfo("Europe/Istanbul")
    dt_session = datetime(trading_date.year, trading_date.month, trading_date.day, 15, 30, tzinfo=ist_tz)

    intraday_data = {}
    daily_data = {}

    for inst in instruments:
        ticker = inst.yahoo_ticker
        # Intraday 5m row
        intraday_data[(ticker, "Open")] = ["100.0"]
        intraday_data[(ticker, "High")] = ["105.0"]
        intraday_data[(ticker, "Low")] = ["99.0"]
        intraday_data[(ticker, "Close")] = ["102.50"]
        intraday_data[(ticker, "Adj Close")] = ["102.50"]
        intraday_data[(ticker, "Volume")] = [10000]

        # Daily rows
        if include_prev_close:
            daily_data[(ticker, "Open")] = ["95.0", "98.0"]
            daily_data[(ticker, "High")] = ["100.0", "103.0"]
            daily_data[(ticker, "Low")] = ["94.0", "97.0"]
            daily_data[(ticker, "Close")] = ["97.50", "100.0"]
            daily_data[(ticker, "Adj Close")] = ["97.50", "100.0"]
            daily_data[(ticker, "Volume")] = [50000, 60000]
        else:
            daily_data[(ticker, "Open")] = ["100.0"]
            daily_data[(ticker, "High")] = ["105.0"]
            daily_data[(ticker, "Low")] = ["99.0"]
            daily_data[(ticker, "Close")] = ["102.50"]
            daily_data[(ticker, "Adj Close")] = ["102.50"]
            daily_data[(ticker, "Volume")] = [10000]

    intra_idx = pd.DatetimeIndex([dt_session])
    df_intra = pd.DataFrame(intraday_data, index=intra_idx)
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Ticker", "Price"])

    if include_prev_close:
        daily_idx = pd.DatetimeIndex([datetime(2026, 8, 19), datetime(trading_date.year, trading_date.month, trading_date.day)])
    else:
        daily_idx = pd.DatetimeIndex([datetime(trading_date.year, trading_date.month, trading_date.day)])
    df_daily = pd.DataFrame(daily_data, index=daily_idx)
    df_daily.columns = pd.MultiIndex.from_tuples(df_daily.columns, names=["Ticker", "Price"])

    return df_intra, df_daily


# ---------------------------------------------------------------------------
# 1. Catalog Validation & Drift Tests
# ---------------------------------------------------------------------------


def test_catalog_validation_supports_dynamic_counts():
    insts_100 = _make_sample_instruments(100)
    is_valid, errors = validate_bist_catalog(insts_100)
    assert is_valid is True
    assert len(errors) == 0

    insts_101 = _make_sample_instruments(101)
    is_valid, errors = validate_bist_catalog(insts_101)
    assert is_valid is True
    assert len(errors) == 0

    insts_120 = _make_sample_instruments(120)
    is_valid, errors = validate_bist_catalog(insts_120)
    assert is_valid is True
    assert len(errors) == 0

    is_valid_empty, errors_empty = validate_bist_catalog([])
    assert is_valid_empty is False
    assert any("Catalog is empty" in e for e in errors_empty)


def test_catalog_validation_rejects_duplicates_and_empty():
    insts = _make_sample_instruments(100)
    insts[1] = insts[1].model_copy(update={"id": insts[0].id})
    is_valid, errors = validate_bist_catalog(insts)
    assert is_valid is False
    assert any("Duplicate instrument_id" in e for e in errors)

    insts = _make_sample_instruments(100)
    insts[1] = insts[1].model_copy(update={"symbol": insts[0].symbol})
    is_valid, errors = validate_bist_catalog(insts)
    assert is_valid is False
    assert any("Duplicate symbol" in e for e in errors)

    insts_inactive = _make_sample_instruments(100, active_count=0)
    is_valid, errors = validate_bist_catalog(insts_inactive)
    assert is_valid is False
    assert any("No active BIST" in e for e in errors)


def test_catalog_validation_rejects_non_try_currency():
    insts = _make_sample_instruments(100)
    insts[5] = insts[5].model_copy(update={"currency": "USD"})
    is_valid, errors = validate_bist_catalog(insts)
    assert is_valid is False
    assert any("Invalid currency 'USD'" in e for e in errors)


def test_catalog_validation_bist_symbol_format_accepts_and_rejects():
    insts = _make_sample_instruments(5)
    # 1. Valid symbols (accept)
    valid_symbols = ["THYAO", "ASELS", "GARAN", "EREGL", "PATEK"]
    for i, sym in enumerate(valid_symbols):
        insts[i] = insts[i].model_copy(update={"symbol": sym})
    is_valid, errors = validate_bist_catalog(insts)
    assert is_valid is True
    assert len(errors) == 0

    # 2. Reject 'BAD SYMBOL'
    insts_bad_space = [i.model_copy() for i in insts]
    insts_bad_space[0] = insts_bad_space[0].model_copy(update={"symbol": "BAD SYMBOL"})
    is_valid_sp, errors_sp = validate_bist_catalog(insts_bad_space)
    assert is_valid_sp is False
    assert any("Invalid BIST symbol format 'BAD SYMBOL'" in e for e in errors_sp)

    # 3. Reject 'THYAO.IS' (database symbol must not have provider suffix)
    insts_bad_suffix = [i.model_copy() for i in insts]
    insts_bad_suffix[0] = insts_bad_suffix[0].model_copy(update={"symbol": "THYAO.IS"})
    is_valid_suf, errors_suf = validate_bist_catalog(insts_bad_suffix)
    assert is_valid_suf is False
    assert any("Invalid BIST symbol format 'THYAO.IS'" in e for e in errors_suf)

    # 4. Reject 'GARAN/IS' (slash not allowed)
    insts_bad_slash = [i.model_copy() for i in insts]
    insts_bad_slash[0] = insts_bad_slash[0].model_copy(update={"symbol": "GARAN/IS"})
    is_valid_sl, errors_sl = validate_bist_catalog(insts_bad_slash)
    assert is_valid_sl is False
    assert any("Invalid BIST symbol format 'GARAN/IS'" in e for e in errors_sl)

    # 5. Reject dot/dash in BIST symbol (e.g. 'THY.AO', 'THY-AO')
    for bad_sym in ("THY.AO", "THY-AO"):
        insts_bad = [i.model_copy() for i in insts]
        insts_bad[0] = insts_bad[0].model_copy(update={"symbol": bad_sym})
        is_valid_b, errors_b = validate_bist_catalog(insts_bad)
        assert is_valid_b is False
        assert any(f"Invalid BIST symbol format '{bad_sym}'" in e for e in errors_b)



def test_catalog_fingerprint_deterministic_and_sensitive_to_symbol_changes():
    insts1 = _make_sample_instruments(100)
    fp1 = compute_bist_catalog_fingerprint(insts1)
    fp2 = compute_bist_catalog_fingerprint(list(reversed(insts1)))
    assert fp1 == fp2

    insts_mutated = [i.model_copy() for i in insts1]
    insts_mutated[0] = insts_mutated[0].model_copy(update={"symbol": "MUTATED"})
    fp_mutated = compute_bist_catalog_fingerprint(insts_mutated)
    assert fp1 != fp_mutated



# ---------------------------------------------------------------------------
# 2. XIST Calendar & Error Distinction Tests
# ---------------------------------------------------------------------------


def test_xist_calendar_trading_day_logic():
    provider = YahooBistPriceProvider()

    # Friday Aug 21, 2026 -> Trading day
    assert provider.is_xist_trading_day(date(2026, 8, 21)) is True
    # Saturday Aug 22, 2026 -> Closed
    assert provider.is_xist_trading_day(date(2026, 8, 22)) is False
    # Sunday Aug 23, 2026 -> Closed
    assert provider.is_xist_trading_day(date(2026, 8, 23)) is False
    # Republic Day Oct 29, 2026 -> Holiday
    assert provider.is_xist_trading_day(date(2026, 10, 29)) is False


def test_calendar_exception_fails_job_instead_of_skipped():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    mock_provider.get_xist_session.side_effect = YahooBistCalendarError("Failed to query XIST calendar: crash")

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert res.status != "skipped"
        mock_email.send_email.assert_called_once()


def test_job_skipped_when_xist_market_closed():
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=date(2026, 8, 22), is_trading_day=False
    )

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)

        assert res.status == "skipped"
        assert res.skip_reason == "market_closed"
        mock_provider.fetch_intraday.assert_not_called()
        mock_email.send_email.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Provider & Isolated Fetch Tests
# ---------------------------------------------------------------------------


def test_provider_formats_and_downloads_tickers():
    mock_download = MagicMock()
    mock_df_intra = MagicMock()
    mock_df_intra.empty = False
    mock_download.return_value = mock_df_intra

    provider = YahooBistPriceProvider(download_fn=mock_download)
    tickers = ["THYAO.IS", "ASELS.IS"]
    intra = provider.fetch_intraday(tickers)

    mock_download.assert_called_once()
    c_args, c_kwargs = mock_download.call_args
    assert c_args[0] == ["ASELS.IS", "THYAO.IS"]
    assert c_kwargs["period"] == "1d"
    assert c_kwargs["interval"] == "5m"


def test_provider_handles_errors_safely(caplog):
    caplog.set_level(logging.ERROR)
    mock_download = MagicMock()
    mock_download.side_effect = RuntimeError("Failed downloading from https://user:SecretPass123@yahoo.com")

    provider = YahooBistPriceProvider(download_fn=mock_download)
    with pytest.raises(YahooBistPriceFetchError):
        provider.fetch_intraday(["THYAO.IS"])

    assert "SecretPass123" not in caplog.text


def test_intraday_success_daily_failure_yields_partial_with_current_prices():
    insts = _make_sample_instruments(100)
    trading_date = date(2026, 8, 21)
    df_intra, _ = _make_sample_dataframes(insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.side_effect = RuntimeError("Daily fetch timeout")

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts), \
         patch.object(BistLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(BistLatestPricesRepository, "upsert_prices", return_value=(100, 0, 0)) as mock_upsert:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "partial"
        assert res.matched_count == 100
        assert res.previous_close_missing_count == 100
        mock_upsert.assert_called_once()
        mock_email.send_email.assert_called_once()


# ---------------------------------------------------------------------------
# 4. MultiIndex, Single-Level Security, Duplicate & Metric Tests
# ---------------------------------------------------------------------------


def test_single_level_dataframe_rejected_for_multi_ticker_request():
    expected_tickers = {"AAA.IS", "BBB.IS"}
    df_single = pd.DataFrame({"Close": [10.0, 11.0], "Open": [9.0, 10.0]})

    sub_a, _ = extract_ticker_dataframe(df_single, "AAA.IS", expected_tickers_count=2)
    sub_b, _ = extract_ticker_dataframe(df_single, "BBB.IS", expected_tickers_count=2)

    assert sub_a is None
    assert sub_b is None


def test_multiindex_reversed_price_level_detection():
    trading_date = date(2026, 8, 21)
    ist_tz = ZoneInfo("Europe/Istanbul")
    dt_session = datetime(2026, 8, 21, 15, 30, tzinfo=ist_tz)

    data = {
        ("Close", "THYAO.IS"): ["302.50"],
        ("Open", "THYAO.IS"): ["300.00"],
    }
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([dt_session]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Price", "Ticker"])

    expected = {"THYAO.IS"}
    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra,
        daily_df=pd.DataFrame(),
        expected_tickers=expected,
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert len(valid) == 1
    assert valid[0].price == Decimal("302.500000000000")
    assert unreq == []  # 'CLOSE' must NOT be counted as unrequested
    assert obs_count == 1


def test_multiindex_unrequested_ticker_detection():
    trading_date = date(2026, 8, 21)
    ist_tz = ZoneInfo("Europe/Istanbul")
    dt_session = datetime(2026, 8, 21, 15, 30, tzinfo=ist_tz)

    data = {
        ("THYAO.IS", "Close"): ["302.50"],
        ("EXTRA.IS", "Close"): ["50.00"],
    }
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([dt_session]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Ticker", "Price"])

    expected = {"THYAO.IS"}
    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra,
        daily_df=pd.DataFrame(),
        expected_tickers=expected,
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert len(valid) == 1
    assert unreq == ["EXTRA.IS"]
    assert obs_count == 2


def test_duplicate_close_columns_treated_as_invalid_and_not_selected():
    """Verify that when a ticker has duplicate Close columns (e.g. 300 and 305), neither is chosen."""
    trading_date = date(2026, 8, 21)
    dt_session = datetime(2026, 8, 21, 15, 30, tzinfo=ZoneInfo("Europe/Istanbul"))

    cols = pd.MultiIndex.from_tuples([("THYAO.IS", "Close"), ("THYAO.IS", "Close")])
    df_intra = pd.DataFrame([["300.0", "305.0"]], columns=cols, index=pd.DatetimeIndex([dt_session]))

    expected = {"THYAO.IS"}
    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra,
        daily_df=pd.DataFrame(),
        expected_tickers=expected,
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert dup == 1
    assert inv == 1
    assert len(valid) == 0  # Neither 300 nor 305 is parsed or chosen!
    assert any("duplicate Close columns" in w for w in warns)


def test_observed_ticker_count_and_response_count_metrics():
    """Test realistic response metrics: 80 tickers present gives response_count=80; 100+1 unrequested gives 101."""
    trading_date = date(2026, 8, 21)
    dt_session = datetime(2026, 8, 21, 15, 30, tzinfo=ZoneInfo("Europe/Istanbul"))

    # Case A: 80 tickers in response out of 100 expected
    all_100_insts = _make_sample_instruments(100)
    df_intra_80, _ = _make_sample_dataframes(all_100_insts[:80], trading_date=trading_date)
    expected_100 = {i.yahoo_ticker for i in all_100_insts}

    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra_80,
        daily_df=pd.DataFrame(),
        expected_tickers=expected_100,
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert obs_count == 80
    assert len(valid) == 80
    assert inv == 20
    assert len(unreq) == 0

    # Case B: 100 expected + 1 unrequested
    df_intra_101, _ = _make_sample_dataframes(all_100_insts, trading_date=trading_date)
    df_intra_101[("EXTRA.IS", "Close")] = ["50.0"]

    (
        valid101,
        warns101,
        inv101,
        dup101,
        unreq101,
        p_match101,
        p_miss101,
        obs_count101,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra_101,
        daily_df=pd.DataFrame(),
        expected_tickers=expected_100,
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert obs_count101 == 101
    assert unreq101 == ["EXTRA.IS"]


# ---------------------------------------------------------------------------
# 5. Decimal Precision & Zero Float Tests
# ---------------------------------------------------------------------------


def test_high_precision_decimal_no_float_drift():
    trading_date = date(2026, 8, 21)
    ist_tz = ZoneInfo("Europe/Istanbul")
    dt_session = datetime(2026, 8, 21, 15, 30, tzinfo=ist_tz)

    exact_price_str = "12345.123456789012"
    exact_prev_str = "12000.000000000000"

    data = {
        ("THYAO.IS", "Close"): [exact_price_str],
    }
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([dt_session]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Ticker", "Price"])

    daily_data = {
        ("THYAO.IS", "Close"): [exact_prev_str],
    }
    df_daily = pd.DataFrame(daily_data, index=pd.DatetimeIndex([datetime(2026, 8, 20)]))
    df_daily.columns = pd.MultiIndex.from_tuples(df_daily.columns, names=["Ticker", "Price"])

    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra,
        daily_df=df_daily,
        expected_tickers={"THYAO.IS"},
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert len(valid) == 1
    assert valid[0].price == Decimal("12345.123456789012")
    assert valid[0].previous_close == Decimal("12000.000000000000")
    assert valid[0].change_amount == Decimal("345.123456789012")


# ---------------------------------------------------------------------------
# 6. Freshness & Order-Independent Selection Tests
# ---------------------------------------------------------------------------


def test_freshness_session_window_rules_active_and_closed():
    trading_date = date(2026, 8, 21)
    ist_tz = ZoneInfo("Europe/Istanbul")

    m_open = datetime(2026, 8, 21, 10, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    m_close = datetime(2026, 8, 21, 18, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    session = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True, market_open=m_open, market_close=m_close
    )

    # 1. 20 min old quote during active session (14:20 bar, 14:40 ref) -> fresh
    t_bar = datetime(2026, 8, 21, 14, 20, tzinfo=ist_tz).astimezone(timezone.utc)
    t_ref = datetime(2026, 8, 21, 14, 40, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_bar, session, t_ref)
    assert fresh is True

    # 2. 2 hours old quote during active session (12:00 bar, 14:40 ref) -> stale
    t_stale = datetime(2026, 8, 21, 12, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_stale, session, t_ref)
    assert fresh is False
    assert "Bar is stale" in msg

    # 3. Future bar (+10 min) -> rejected
    t_future = datetime(2026, 8, 21, 14, 50, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_future, session, t_ref)
    assert fresh is False
    assert "in the future" in msg

    # 4. 18:30 cron with 18:05 closing auction bar -> fresh
    t_close_bar = datetime(2026, 8, 21, 18, 5, tzinfo=ist_tz).astimezone(timezone.utc)
    t_cron_ref = datetime(2026, 8, 21, 18, 30, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_close_bar, session, t_cron_ref)
    assert fresh is True

    # 5. 18:30 cron with 17:30 bar (within 60m before close) -> fresh
    t_1730_bar = datetime(2026, 8, 21, 17, 30, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_1730_bar, session, t_cron_ref)
    assert fresh is True

    # 6. 18:30 cron with 15:00 bar -> rejected (too far before close, >60m)
    t_1500_bar = datetime(2026, 8, 21, 15, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1500_bar, session, t_cron_ref)
    assert fresh is False
    assert "too early before market close" in msg

    # 7. 18:30 cron with 18:25 bar -> rejected (too late after close, >15m)
    t_1825_bar = datetime(2026, 8, 21, 18, 25, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1825_bar, session, t_cron_ref)
    assert fresh is False
    assert "too late after market close" in msg


def test_freshness_half_day_session_rules():
    """Verify half-day session handling with market_close=13:00."""
    trading_date = date(2026, 10, 28)  # Republic Day Eve (Half day in Turkey)
    ist_tz = ZoneInfo("Europe/Istanbul")

    m_open = datetime(2026, 10, 28, 10, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    m_close = datetime(2026, 10, 28, 13, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    session = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True, market_open=m_open, market_close=m_close
    )

    t_cron_ref = datetime(2026, 10, 28, 14, 30, tzinfo=ist_tz).astimezone(timezone.utc)

    # 12:40 bar -> fresh (within 60m of 13:00 close)
    t_1240 = datetime(2026, 10, 28, 12, 40, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_1240, session, t_cron_ref)
    assert fresh is True

    # 13:05 bar -> fresh (within 15m of 13:00 close)
    t_1305 = datetime(2026, 10, 28, 13, 5, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_1305, session, t_cron_ref)
    assert fresh is True

    # 11:30 bar -> rejected (>60m before 13:00 close)
    t_1130 = datetime(2026, 10, 28, 11, 30, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1130, session, t_cron_ref)
    assert fresh is False
    assert "too early before market close" in msg

    # 13:25 bar -> rejected (>15m after 13:00 close)
    t_1325 = datetime(2026, 10, 28, 13, 25, tzinfo=ist_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1325, session, t_cron_ref)
    assert fresh is False
    assert "too late after market close" in msg


def test_order_independent_intraday_and_daily_selection():
    trading_date = date(2026, 8, 21)
    ist_tz = ZoneInfo("Europe/Istanbul")

    t1 = datetime(2026, 8, 21, 14, 0, tzinfo=ist_tz)
    t2 = datetime(2026, 8, 21, 15, 0, tzinfo=ist_tz)
    t3 = datetime(2026, 8, 21, 13, 0, tzinfo=ist_tz)

    data = {
        ("THYAO.IS", "Close"): ["100.0", "110.0", "90.0"],
    }
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([t1, t2, t3]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Ticker", "Price"])

    d1 = datetime(2026, 8, 18)
    d2 = datetime(2026, 8, 20)
    d3 = datetime(2026, 8, 19)
    daily_data = {
        ("THYAO.IS", "Close"): ["92.0", "98.0", "95.0"],
    }
    df_daily = pd.DataFrame(daily_data, index=pd.DatetimeIndex([d1, d2, d3]))
    df_daily.columns = pd.MultiIndex.from_tuples(df_daily.columns, names=["Ticker", "Price"])

    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra,
        daily_df=df_daily,
        expected_tickers={"THYAO.IS"},
        trading_date=trading_date,
        reference_time=datetime(2026, 8, 21, 15, 10, tzinfo=ist_tz),
    )

    assert len(valid) == 1
    # Max intraday timestamp (t2 -> 110.0)
    assert valid[0].price == Decimal("110.000000000000")
    # Max daily date before trading date (d2 = 2026-08-20 -> 98.0)
    assert valid[0].previous_close == Decimal("98.000000000000")


# ---------------------------------------------------------------------------
# 7. Service Normalization & Matching Regression Tests
# ---------------------------------------------------------------------------


def test_service_normalizes_bist_intraday_and_daily_prices():
    insts = _make_sample_instruments(2)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts, trading_date=trading_date, include_prev_close=True)

    expected_tickers = {i.yahoo_ticker for i in insts}
    (
        valid_prices,
        warnings,
        inv_cnt,
        dup_cnt,
        unreq,
        prev_matched,
        prev_missing,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra,
        daily_df=df_daily,
        expected_tickers=expected_tickers,
        trading_date=trading_date,
        reference_time=datetime(2026, 8, 21, 15, 40, tzinfo=ZoneInfo("Europe/Istanbul")),
    )

    assert len(valid_prices) == 2
    assert inv_cnt == 0
    assert dup_cnt == 0
    assert len(unreq) == 0
    assert prev_matched == 2
    assert prev_missing == 0
    assert obs_count == 2

    p1 = valid_prices[0]
    assert p1.price == Decimal("102.500000000000")
    assert p1.previous_close == Decimal("97.500000000000")
    assert p1.change_amount == Decimal("5.000000000000")
    assert p1.change_percent == Decimal("5.12820513")
    assert p1.currency == "TRY"
    assert p1.provider_code == "YAHOO_FINANCE"
    assert p1.price_at.tzinfo == timezone.utc


def test_service_handles_missing_previous_close_without_stale():
    insts = _make_sample_instruments(1)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts, trading_date=trading_date, include_prev_close=False)

    expected_tickers = {i.yahoo_ticker for i in insts}
    (
        valid_prices,
        warnings,
        inv_cnt,
        dup_cnt,
        unreq,
        prev_matched,
        prev_missing,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra,
        daily_df=df_daily,
        expected_tickers=expected_tickers,
        trading_date=trading_date,
        reference_time=datetime(2026, 8, 21, 15, 40, tzinfo=ZoneInfo("Europe/Istanbul")),
    )

    assert len(valid_prices) == 1
    assert prev_matched == 0
    assert prev_missing == 1

    p1 = valid_prices[0]
    assert p1.price == Decimal("102.500000000000")
    assert p1.previous_close is None
    assert p1.change_amount is None
    assert p1.change_percent is None


def test_service_rejects_wrong_date_intraday_data():
    insts = _make_sample_instruments(1)
    trading_date = date(2026, 8, 21)
    wrong_date = date(2026, 8, 20)
    df_intra, df_daily = _make_sample_dataframes(insts, trading_date=wrong_date, include_prev_close=True)

    expected_tickers = {i.yahoo_ticker for i in insts}
    (
        valid_prices,
        warnings,
        inv_cnt,
        dup_cnt,
        unreq,
        prev_matched,
        prev_missing,
        obs_count,
    ) = BistPricesService.normalize_bist_prices(
        intraday_df=df_intra,
        daily_df=df_daily,
        expected_tickers=expected_tickers,
        trading_date=trading_date,
    )

    assert len(valid_prices) == 0
    assert inv_cnt == 1
    assert any("no fresh intraday data" in w for w in warnings)


def test_match_prices_with_active_instruments():
    inst1 = BistDbInstrument(id=uuid4(), symbol="THYAO", company_name="THY", is_active=True)
    inst2 = BistDbInstrument(id=uuid4(), symbol="ASELS", company_name="Aselsan", is_active=True)
    inst3 = BistDbInstrument(id=uuid4(), symbol="AKBNK", company_name="Akbank", is_active=True)

    t_now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    prices = [
        BistNormalizedPrice(
            symbol="THYAO",
            yahoo_ticker="THYAO.IS",
            price=Decimal("300.000000000000"),
            previous_close=Decimal("290.000000000000"),
            change_amount=Decimal("10.000000000000"),
            change_percent=Decimal("3.44827586"),
            price_at=t_now,
            currency="TRY",
            provider_code="YAHOO_FINANCE",
        ),
        BistNormalizedPrice(
            symbol="ASELS",
            yahoo_ticker="ASELS.IS",
            price=Decimal("60.000000000000"),
            previous_close=Decimal("58.000000000000"),
            change_amount=Decimal("2.000000000000"),
            change_percent=Decimal("3.44827586"),
            price_at=t_now,
            currency="TRY",
            provider_code="YAHOO_FINANCE",
        ),
    ]

    matched, missing, unmatched = BistPricesService.match_prices_with_instruments(
        [inst1, inst2, inst3], prices, unrequested_tickers=["EXTRA.IS"]
    )

    assert len(matched) == 2
    assert matched[0].symbol == "THYAO"
    assert matched[1].symbol == "ASELS"
    assert len(missing) == 1
    assert missing[0].symbol == "AKBNK"
    assert unmatched == ["EXTRA.IS"]


# ---------------------------------------------------------------------------
# 8. Plan BIST Price Upserts & Repository Tests
# ---------------------------------------------------------------------------


def test_plan_bist_price_upserts_idempotent_merge_rules():
    uid1 = uuid4()
    t_stored = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    t_newer = datetime(2026, 8, 21, 12, 30, tzinfo=timezone.utc)
    t_older = datetime(2026, 8, 21, 11, 45, tzinfo=timezone.utc)

    stored_map = {
        uid1: {
            "price": Decimal("300.000000000000"),
            "currency": "TRY",
            "previous_close": Decimal("290.000000000000"),
            "change_amount": Decimal("10.000000000000"),
            "change_percent": Decimal("3.44827586"),
            "price_at": t_stored,
            "provider_code": "YAHOO_FINANCE",
            "is_stale": False,
            "stale_reason": None,
        },
    }

    incoming_items = [
        BistPriceSyncItem(
            instrument_id=uid1,
            symbol="THYAO",
            yahoo_ticker="THYAO.IS",
            price=Decimal("305.000000000000"),
            currency="TRY",
            previous_close=Decimal("290.000000000000"),
            change_amount=Decimal("15.000000000000"),
            change_percent=Decimal("5.17241379"),
            price_at=t_newer,
            provider_code="YAHOO_FINANCE",
        ),
    ]

    to_upsert, unchanged_cnt, stale_skip_cnt = plan_bist_price_upserts(stored_map, incoming_items)
    assert len(to_upsert) == 1
    assert unchanged_cnt == 0
    assert stale_skip_cnt == 0

    older_item = BistPriceSyncItem(
        instrument_id=uid1,
        symbol="THYAO",
        yahoo_ticker="THYAO.IS",
        price=Decimal("295.000000000000"),
        currency="TRY",
        previous_close=Decimal("290.000000000000"),
        change_amount=Decimal("5.000000000000"),
        change_percent=Decimal("1.72413793"),
        price_at=t_older,
        provider_code="YAHOO_FINANCE",
    )
    to_up2, unch2, stale_skip2 = plan_bist_price_upserts(stored_map, [older_item])
    assert len(to_up2) == 0
    assert unch2 == 0
    assert stale_skip2 == 1


def test_mark_stale_prices_rejects_invalid_reason_without_sql():
    mock_conn = MagicMock()
    with pytest.raises(ValueError) as exc_info:
        BistLatestPricesRepository.mark_stale_prices(
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

    count = BistLatestPricesRepository.mark_stale_prices(
        mock_conn,
        [uid_with_row, uid_without_row],
        reason="price_not_published",
        stored_map=stored_map,
    )

    mock_conn.cursor.assert_called_once()
    assert count == 1


# ---------------------------------------------------------------------------
# 9. End-to-End Orchestration & Completeness Tests
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
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts[:matched_count], trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts), \
         patch.object(BistLatestPricesRepository, "get_stored_latest_prices", return_value={}):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
        assert res.status == expected_status
        assert res.matched_count == matched_count
        assert res.requested_active_count == active_count


def test_dry_run_executes_zero_writes_and_zero_emails():
    insts = _make_sample_instruments(100)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts), \
         patch.object(BistLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(BistLatestPricesRepository, "upsert_prices") as mock_upsert, \
         patch.object(BistLatestPricesRepository, "mark_stale_prices") as mock_stale:

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)

        assert res.status == "success"
        assert res.dry_run is True
        mock_upsert.assert_not_called()
        mock_stale.assert_not_called()
        mock_email.send_email.assert_not_called()


def test_dry_run_db_read_error_fails_closed_safely():
    insts = _make_sample_instruments(100)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts), \
         patch.object(BistLatestPricesRepository, "get_stored_latest_prices", side_effect=RuntimeError("DB error with token=Secret123")):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert res.dry_run is True
        assert "Secret123" not in (res.error or "")
        mock_email.send_email.assert_not_called()


def test_apply_db_success_sends_no_email():
    insts = _make_sample_instruments(100)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts), \
         patch.object(BistLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(BistLatestPricesRepository, "upsert_prices", return_value=(100, 0, 0)) as mock_upsert:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "success"
        assert res.dry_run is False
        assert res.email_status is None
        mock_upsert.assert_called_once()
        mock_email.send_email.assert_not_called()


def test_apply_db_partial_sends_warning_email_and_marks_missing_stale():
    insts = _make_sample_instruments(100)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts[:85], trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts), \
         patch.object(BistLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(BistLatestPricesRepository, "upsert_prices", return_value=(85, 0, 0)) as mock_upsert, \
         patch.object(BistLatestPricesRepository, "mark_stale_prices", return_value=15) as mock_stale:

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


def test_apply_db_fails_closed_on_catalog_drift_with_symbol_change():
    insts_before = _make_sample_instruments(100)
    insts_after = [i.model_copy() for i in insts_before]
    insts_after[0] = insts_after[0].model_copy(update={"symbol": "DRIFTED"})

    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts_before, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", side_effect=[insts_before, insts_after]), \
         patch.object(BistLatestPricesRepository, "upsert_prices") as mock_upsert:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert "Safety check failed" in (res.error or "")
        mock_upsert.assert_not_called()
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
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_dataframes(insts[:85], trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=trading_date, is_trading_day=True
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = stateful_conn

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts), \
         patch.object(BistLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(BistLatestPricesRepository, "upsert_prices", return_value=(85, 0, 0)), \
         patch.object(BistLatestPricesRepository, "mark_stale_prices", side_effect=RuntimeError("SQL failure with token=SecretKey999")):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert any(tx.status == "rolled_back" for tx in stateful_conn.transactions)
        mock_email.send_email.assert_called_once()
        assert "SecretKey999" not in (res.error or "")


def test_sanitize_error_message_masks_secrets():
    exc1 = RuntimeError("Connection failed to postgresql://user:SecretDbPass123@supabase.com:5432/db")
    assert sanitize_error_message(exc1) == "Veritabanı veya servis erişim hatası."

    exc2 = TimeoutError("HTTP request timed out connecting to https://query1.finance.yahoo.com")
    assert sanitize_error_message(exc2) == "Veritabanı veya servis erişim hatası."

    exc3 = ValueError("Catalog validation failed: duplicate symbol THYAO")
    assert sanitize_error_message(exc3) == "Catalog validation failed: duplicate symbol THYAO"


def test_smtp_exception_handling_masks_secrets(caplog):
    caplog.set_level(logging.ERROR)
    insts = _make_sample_instruments(100)
    mock_provider = MagicMock()
    mock_provider.get_xist_session.return_value = XistSessionInfo(
        trading_date=date(2026, 8, 21), is_trading_day=True
    )
    mock_provider.fetch_intraday.side_effect = RuntimeError("Yahoo API down")

    mock_email = MagicMock()
    mock_email.send_email.side_effect = RuntimeError("SMTP failed with token=SecretSmtpToken999")
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert res.email_status == "failed"
        assert res.email_error == "E-posta gönderimi sırasında hata oluştu."
        assert "SecretSmtpToken999" not in caplog.text


def test_github_actions_cron_schedule():
    workflow_path = ".github/workflows/bist-latest-prices.yml"
    with open(workflow_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    on_section = data.get("on") or data.get(True)
    cron_expr = on_section["schedule"][0]["cron"]
    assert cron_expr == "10,40 7-15 * * 1-5"


# ---------------------------------------------------------------------------
# 11. Dynamic BIST Catalog Expansion Tests (101, 120 Instruments)
# ---------------------------------------------------------------------------


def test_bist_job_handles_101_and_120_instruments_successfully():
    """Verify that BIST latest prices job runs smoothly with 101 and 120 instruments deterministically."""
    tz = ZoneInfo("Europe/Istanbul")
    fixed_ref_time = datetime(2026, 8, 21, 15, 35, tzinfo=tz)
    fixed_trading_date = date(2026, 8, 21)
    market_open = datetime(2026, 8, 21, 10, 0, tzinfo=tz)
    market_close = datetime(2026, 8, 21, 18, 0, tzinfo=tz)

    for count in (101, 120):
        insts = _make_sample_instruments(count, active_count=count)
        df_intra, df_daily = _make_sample_dataframes(insts, trading_date=fixed_trading_date)

        mock_provider = MagicMock()
        mock_provider.get_xist_session.return_value = XistSessionInfo(
            trading_date=fixed_trading_date,
            is_trading_day=True,
            market_open=market_open,
            market_close=market_close,
        )
        mock_provider.fetch_intraday.return_value = df_intra
        mock_provider.fetch_daily.return_value = df_daily

        mock_email = MagicMock()
        mock_conn_ctx = MagicMock()
        mock_conn = MagicMock()
        mock_conn_ctx.__enter__.return_value = mock_conn

        with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
             patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=insts), \
             patch.object(BistLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
             patch.object(BistLatestPricesRepository, "upsert_prices", return_value=(count, 0, 0)) as mock_upsert:

            # 1. Dry-run
            res_dry = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=fixed_ref_time)
            assert res_dry.status == "success"
            assert res_dry.requested_active_count == count
            assert res_dry.matched_count == count
            mock_upsert.assert_not_called()

            # 2. Apply-db
            res_apply = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=fixed_ref_time)
            assert res_apply.status == "success"
            assert res_apply.requested_active_count == count
            assert res_apply.matched_count == count
            assert res_apply.inserted_or_updated_count == count
            mock_upsert.assert_called_once()



def test_bist_job_fails_safely_on_empty_catalog():
    """Verify that an empty BIST catalog fails safely before provider or DB operations."""
    mock_provider = MagicMock()
    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=[]):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email)
        assert res.status == "failed"
        assert "Catalog validation failed" in res.error
        mock_provider.get_xist_session.assert_not_called()


def test_bist_provider_request_executed_lifecycle_paths():
    """Verify job-level provider_request_executed lifecycle paths for BIST price job."""
    mock_provider = MagicMock()
    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    ist_tz = ZoneInfo("Europe/Istanbul")
    ref_time = datetime(2026, 8, 21, 14, 0, tzinfo=ist_tz).astimezone(timezone.utc)

    # 1. Pre-provider catalog error -> provider_request_executed is False
    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=[]):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == "failed"
        assert res.provider_request_executed is False

    # Valid catalog fixtures
    valid_instruments = [
        BistDbInstrument(id=uuid4(), symbol="THYAO", company_name="THY", is_active=True),
    ]

    # 2. Pre-provider market closed -> provider_request_executed is False
    closed_session = XistSessionInfo(
        trading_date=date(2026, 8, 21),
        is_trading_day=False,
    )
    mock_provider.get_xist_session.return_value = closed_session
    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=valid_instruments):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == "skipped"
        assert res.skip_reason == "market_closed"
        assert res.provider_request_executed is False

    # 3. Provider request raises exception -> provider_request_executed is True
    open_session = XistSessionInfo(
        trading_date=date(2026, 8, 21),
        is_trading_day=True,
        market_open=datetime(2026, 8, 21, 10, 0, tzinfo=ist_tz).astimezone(timezone.utc),
        market_close=datetime(2026, 8, 21, 18, 0, tzinfo=ist_tz).astimezone(timezone.utc),
    )
    mock_provider.get_xist_session.return_value = open_session
    mock_provider.fetch_intraday.side_effect = RuntimeError("Yahoo Finance network timeout")

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=valid_instruments):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == "failed"
        assert res.provider_request_executed is True

    # 4. Provider request succeeds -> provider_request_executed is True
    import pandas as pd
    cols = pd.MultiIndex.from_tuples([("Close", "THYAO.IS")])
    mock_provider.fetch_intraday.side_effect = None
    mock_provider.fetch_intraday.return_value = pd.DataFrame([[100.0]], index=[ref_time], columns=cols)
    mock_provider.fetch_daily_previous_close.return_value = pd.DataFrame([
        {"ticker": "THYAO.IS", "previous_close": 95.0}
    ])

    with patch("app.jobs.bist_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(BistLatestPricesRepository, "get_all_bist_instruments", return_value=valid_instruments):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.provider_request_executed is True
