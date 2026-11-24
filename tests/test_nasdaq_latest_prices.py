import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional
from unittest.mock import MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from app.jobs.nasdaq_latest_prices_job import (
    run_job,
    sanitize_error_message,
)
from app.models.nasdaq_prices import (
    NasdaqDbInstrument,
    NasdaqNormalizedPrice,
    NasdaqPricesJobResult,
    NasdaqPriceSyncItem,
    NasdaqSessionInfo,
)
from app.providers.nasdaq.yahoo_nasdaq_provider import (
    YahooNasdaqCalendarError,
    YahooNasdaqPriceFetchError,
    YahooNasdaqPriceProvider,
)
from app.repositories.nasdaq_latest_prices_repository import (
    NasdaqLatestPricesRepository,
)
from app.services.nasdaq_prices_service import (
    NasdaqPricesService,
    compute_nasdaq_catalog_fingerprint,
    extract_ticker_dataframe,
    is_price_bar_fresh,
    plan_nasdaq_price_upserts,
    validate_nasdaq_catalog,
)


def _make_sample_nasdaq_instruments(count: int = 97, active_count: int = 97) -> List[NasdaqDbInstrument]:
    """Helper to generate a valid list of NasdaqDbInstrument objects."""
    instruments: List[NasdaqDbInstrument] = []
    for i in range(count):
        sym = f"NDQ{i:03d}"
        instruments.append(
            NasdaqDbInstrument(
                id=uuid4(),
                symbol=sym,
                company_name=f"Nasdaq Company {sym}",
                currency="USD",
                is_active=(i < active_count),
            )
        )
    return instruments


def _make_sample_nasdaq_dataframes(
    instruments: List[NasdaqDbInstrument],
    trading_date: date,
    include_prev_close: bool = True,
    market_tz_name: str = "America/New_York",
    bar_time: Optional[datetime] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Helper to generate sample intraday and daily DataFrames for NASDAQ instruments."""
    ny_tz = ZoneInfo(market_tz_name)
    if bar_time is not None:
        dt_session = bar_time
    else:
        dt_session = datetime(trading_date.year, trading_date.month, trading_date.day, 10, 30, tzinfo=ny_tz)

    intraday_data = {}
    daily_data = {}

    for inst in instruments:
        ticker = inst.yahoo_ticker
        intraday_data[(ticker, "Open")] = ["150.0"]
        intraday_data[(ticker, "High")] = ["155.0"]
        intraday_data[(ticker, "Low")] = ["149.0"]
        intraday_data[(ticker, "Close")] = ["152.50"]
        intraday_data[(ticker, "Adj Close")] = ["152.50"]
        intraday_data[(ticker, "Volume")] = [20000]

        if include_prev_close:
            daily_data[(ticker, "Open")] = ["145.0", "148.0"]
            daily_data[(ticker, "High")] = ["150.0", "153.0"]
            daily_data[(ticker, "Low")] = ["144.0", "147.0"]
            daily_data[(ticker, "Close")] = ["147.50", "150.0"]
            daily_data[(ticker, "Adj Close")] = ["147.50", "150.0"]
            daily_data[(ticker, "Volume")] = [80000, 90000]
        else:
            daily_data[(ticker, "Open")] = ["150.0"]
            daily_data[(ticker, "High")] = ["155.0"]
            daily_data[(ticker, "Low")] = ["149.0"]
            daily_data[(ticker, "Close")] = ["152.50"]
            daily_data[(ticker, "Adj Close")] = ["152.50"]
            daily_data[(ticker, "Volume")] = [20000]

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
# 1. Catalog Validation & Drift Tests (97 instruments, USD currency)
# ---------------------------------------------------------------------------


def test_catalog_validation_supports_dynamic_counts():
    insts_97 = _make_sample_nasdaq_instruments(97)
    is_valid, errors = validate_nasdaq_catalog(insts_97)
    assert is_valid is True
    assert len(errors) == 0

    insts_96 = _make_sample_nasdaq_instruments(96)
    is_valid, errors = validate_nasdaq_catalog(insts_96)
    assert is_valid is True
    assert len(errors) == 0

    insts_120 = _make_sample_nasdaq_instruments(120)
    is_valid, errors = validate_nasdaq_catalog(insts_120)
    assert is_valid is True
    assert len(errors) == 0

    is_valid_empty, errors_empty = validate_nasdaq_catalog([])
    assert is_valid_empty is False
    assert any("Catalog is empty" in e for e in errors_empty)


def test_catalog_validation_rejects_duplicates_and_empty():
    insts = _make_sample_nasdaq_instruments(97)
    insts[1] = insts[1].model_copy(update={"id": insts[0].id})
    is_valid, errors = validate_nasdaq_catalog(insts)
    assert is_valid is False
    assert any("Duplicate instrument_id" in e for e in errors)

    insts = _make_sample_nasdaq_instruments(97)
    insts[1] = insts[1].model_copy(update={"symbol": insts[0].symbol})
    is_valid, errors = validate_nasdaq_catalog(insts)
    assert is_valid is False
    assert any("Duplicate symbol" in e for e in errors)

    insts_inactive = _make_sample_nasdaq_instruments(97, active_count=0)
    is_valid, errors = validate_nasdaq_catalog(insts_inactive)
    assert is_valid is False
    assert any("No active NASDAQ" in e for e in errors)


def test_catalog_validation_rejects_non_usd_currency():
    insts = _make_sample_nasdaq_instruments(97)
    insts[5] = insts[5].model_copy(update={"currency": "TRY"})
    is_valid, errors = validate_nasdaq_catalog(insts)
    assert is_valid is False
    assert any("Invalid currency 'TRY'" in e for e in errors)


def test_catalog_validation_nasdaq_symbol_format_accepts_and_rejects():
    insts = _make_sample_nasdaq_instruments(4)
    # 1. Accept valid symbols: AAPL, NVDA, BRK-B, BF.B
    valid_symbols = ["AAPL", "NVDA", "BRK-B", "BF.B"]
    for i, sym in enumerate(valid_symbols):
        insts[i] = insts[i].model_copy(update={"symbol": sym})
    is_valid, errors = validate_nasdaq_catalog(insts)
    assert is_valid is True
    assert len(errors) == 0

    # 2. Reject empty / whitespace
    for bad_empty in ("", "   "):
        insts_bad = [i.model_copy() for i in insts]
        insts_bad[0] = insts_bad[0].model_copy(update={"symbol": bad_empty})
        is_valid_b, errors_b = validate_nasdaq_catalog(insts_bad)
        assert is_valid_b is False
        assert any("Empty symbol" in e for e in errors_b)

    # 3. Reject 'BAD SYMBOL' (spaces)
    insts_bad_space = [i.model_copy() for i in insts]
    insts_bad_space[0] = insts_bad_space[0].model_copy(update={"symbol": "BAD SYMBOL"})
    is_valid_sp, errors_sp = validate_nasdaq_catalog(insts_bad_space)
    assert is_valid_sp is False
    assert any("Invalid NASDAQ symbol format 'BAD SYMBOL'" in e for e in errors_sp)

    # 4. Reject 'AAPL/US' (slash)
    insts_bad_slash = [i.model_copy() for i in insts]
    insts_bad_slash[0] = insts_bad_slash[0].model_copy(update={"symbol": "AAPL/US"})
    is_valid_sl, errors_sl = validate_nasdaq_catalog(insts_bad_slash)
    assert is_valid_sl is False
    assert any("Invalid NASDAQ symbol format 'AAPL/US'" in e for e in errors_sl)

    # 5. Reject leading/trailing punctuation: '-AAPL', 'AAPL-'
    for bad_punct in ("-AAPL", "AAPL-", ".AAPL", "AAPL."):
        insts_bad = [i.model_copy() for i in insts]
        insts_bad[0] = insts_bad[0].model_copy(update={"symbol": bad_punct})
        is_valid_p, errors_p = validate_nasdaq_catalog(insts_bad)
        assert is_valid_p is False
        assert any(f"Invalid NASDAQ symbol format '{bad_punct}'" in e for e in errors_p)

    # 6. Reject consecutive punctuation: 'AAPL..B', 'AAPL.-B'
    for bad_consec in ("AAPL..B", "AAPL.-B", "AAPL--B"):
        insts_bad = [i.model_copy() for i in insts]
        insts_bad[0] = insts_bad[0].model_copy(update={"symbol": bad_consec})
        is_valid_c, errors_c = validate_nasdaq_catalog(insts_bad)
        assert is_valid_c is False
        assert any(f"Invalid NASDAQ symbol format '{bad_consec}'" in e for e in errors_c)




def test_catalog_fingerprint_deterministic_and_sensitive_to_symbol_changes():
    insts1 = _make_sample_nasdaq_instruments(97)
    fp1 = compute_nasdaq_catalog_fingerprint(insts1)
    fp2 = compute_nasdaq_catalog_fingerprint(list(reversed(insts1)))
    assert fp1 == fp2

    insts_mutated = [i.model_copy() for i in insts1]
    insts_mutated[0] = insts_mutated[0].model_copy(update={"symbol": "MUTATED"})
    fp_mutated = compute_nasdaq_catalog_fingerprint(insts_mutated)
    assert fp1 != fp_mutated


# ---------------------------------------------------------------------------
# 2. NASDAQ Calendar, Session & Execution Window Resolution Tests
# ---------------------------------------------------------------------------


def test_nasdaq_market_open_plus_29m_is_market_not_ready():
    """market_open + 29m -> market_not_ready (waiting for initial 30m bars to form)."""
    provider = YahooNasdaqPriceProvider()
    # Aug 21, 2026: market_open = 09:30 EDT = 13:30 UTC. Open + 29m = 13:59 UTC
    t_ref_utc = datetime(2026, 8, 21, 13, 59, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_ref_utc)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 8, 21)
    assert window is None
    assert skip_reason == "market_not_ready"


def test_nasdaq_market_open_plus_30m_is_regular_session_provider_called():
    """market_open + 30m -> regular_session (inclusive window start)."""
    provider = YahooNasdaqPriceProvider()
    # Aug 21, 2026: market_open = 09:30 EDT = 13:30 UTC. Open + 30m = 14:00 UTC
    t_ref_utc = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_ref_utc)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 8, 21)
    assert skip_reason is None
    assert window == "regular_session"


def test_nasdaq_mid_session_is_regular_session_provider_called():
    """Mid-session (e.g. 12:30 EDT = 16:30 UTC) -> regular_session."""
    provider = YahooNasdaqPriceProvider()
    t_ref_utc = datetime(2026, 8, 21, 16, 30, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_ref_utc)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 8, 21)
    assert skip_reason is None
    assert window == "regular_session"


def test_nasdaq_exact_market_close_is_regular_session():
    """Exact market_close (16:00 EDT = 20:00 UTC) -> regular_session (boundary)."""
    provider = YahooNasdaqPriceProvider()
    t_ref_utc = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_ref_utc)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 8, 21)
    assert skip_reason is None
    assert window == "regular_session"


def test_nasdaq_market_close_plus_1m_is_post_close():
    """market_close + 1m (16:01 EDT = 20:01 UTC) -> post_close."""
    provider = YahooNasdaqPriceProvider()
    t_ref_utc = datetime(2026, 8, 21, 20, 1, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_ref_utc)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 8, 21)
    assert skip_reason is None
    assert window == "post_close"


def test_nasdaq_market_close_delayed_2h52m_is_post_close():
    """market_close + 2h52m (18:52 EDT = 22:52 UTC) -> post_close."""
    provider = YahooNasdaqPriceProvider()
    t_ref_utc = datetime(2026, 8, 21, 22, 52, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_ref_utc)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 8, 21)
    assert skip_reason is None
    assert window == "post_close"


def test_nasdaq_market_close_plus_6h_is_post_close():
    """market_close + 6h (22:00 EDT = 02:00 UTC Sat) -> post_close (inclusive boundary)."""
    provider = YahooNasdaqPriceProvider()
    t_ref_utc = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_ref_utc)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 8, 21)
    assert skip_reason is None
    assert window == "post_close"


def test_nasdaq_market_close_plus_6h01m_is_outside_market_window():
    """market_close + 6h01m (22:01 EDT = 02:01 UTC Sat) -> outside_market_window."""
    provider = YahooNasdaqPriceProvider()
    t_ref_utc = datetime(2026, 8, 22, 2, 1, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_ref_utc)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 8, 21)
    assert window is None
    assert skip_reason == "outside_market_window"


def test_nasdaq_calendar_weekend_and_holiday_skipped():
    provider = YahooNasdaqPriceProvider()

    # Saturday Aug 22, 2026 at 14:30 UTC
    t_sat_utc = datetime(2026, 8, 22, 14, 30, tzinfo=timezone.utc)
    sess_sat, window_sat, skip_sat = provider.resolve_nasdaq_session(t_sat_utc)
    assert sess_sat.is_trading_day is False
    assert window_sat is None
    assert skip_sat == "market_closed"

    # US Independence Day July 3, 2026
    t_holiday_utc = datetime(2026, 7, 3, 14, 30, tzinfo=timezone.utc)
    sess_hol, window_hol, skip_hol = provider.resolve_nasdaq_session(t_holiday_utc)
    assert sess_hol.is_trading_day is False
    assert window_hol is None
    assert skip_hol == "market_closed"


def test_nasdaq_calendar_turkey_0030_maps_to_us_friday_post_close_est():
    """Test US Winter Time (EST: UTC-5).

    Market hours: 09:30 - 16:00 EST (14:30 - 21:00 UTC).
    Turkey time Saturday 00:30 UTC+3 -> Friday 21:30 UTC (16:30 EST, 30m after close).
    """
    provider = YahooNasdaqPriceProvider()
    # Friday Dec 11, 2026: 21:35 UTC (Saturday 00:35 Turkey time)
    t_winter_ref = datetime(2026, 12, 11, 21, 35, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_winter_ref)

    assert sess.is_trading_day is True
    assert sess.trading_date == date(2026, 12, 11)  # US Friday date
    assert skip_reason is None
    assert window == "post_close"


def test_nasdaq_early_close_delayed_is_post_close():
    """Test early close day (e.g. day after Thanksgiving, close at 13:00 EST = 18:00 UTC).

    Execution window: 13:30 UTC (open + 30m) to 24:00 UTC (18:00 + 6h = 19:00 EST).
    At 19:35 UTC (1h35m after early close) -> post_close.
    """
    provider = YahooNasdaqPriceProvider()
    # Black Friday Nov 27, 2026 at 19:35 UTC (14:35 EST)
    t_early_ref = datetime(2026, 11, 27, 19, 35, tzinfo=timezone.utc)
    sess, window, skip_reason = provider.resolve_nasdaq_session(t_early_ref)

    assert sess.is_trading_day is True
    assert sess.early_close is True
    assert skip_reason is None
    assert window == "post_close"


def test_calendar_exception_fails_job_instead_of_skipped():
    insts = _make_sample_nasdaq_instruments(97)
    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.side_effect = YahooNasdaqCalendarError("Failed to query NASDAQ calendar")

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert res.status != "skipped"
        mock_email.send_email.assert_called_once()


def test_job_skipped_when_nasdaq_market_closed_or_outside_slot():
    insts = _make_sample_nasdaq_instruments(97)
    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=date(2026, 8, 22), is_trading_day=False),
        None,
        "market_closed",
    )

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts):

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

    provider = YahooNasdaqPriceProvider(download_fn=mock_download)
    tickers = ["AAPL", "MSFT"]
    intra = provider.fetch_intraday(tickers)

    mock_download.assert_called_once()
    c_args, c_kwargs = mock_download.call_args
    assert c_args[0] == ["AAPL", "MSFT"]
    assert c_kwargs["period"] == "1d"
    assert c_kwargs["interval"] == "5m"


def test_provider_handles_errors_safely(caplog):
    caplog.set_level(logging.ERROR)
    mock_download = MagicMock()
    mock_download.side_effect = RuntimeError("Failed downloading from https://user:SecretPass123@yahoo.com")

    provider = YahooNasdaqPriceProvider(download_fn=mock_download)
    with pytest.raises(YahooNasdaqPriceFetchError):
        provider.fetch_intraday(["AAPL"])

    assert "SecretPass123" not in caplog.text


def test_intraday_success_daily_failure_yields_partial_with_current_prices():
    insts = _make_sample_nasdaq_instruments(97)
    trading_date = date(2026, 8, 21)
    df_intra, _ = _make_sample_nasdaq_dataframes(insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.side_effect = RuntimeError("Daily fetch timeout")

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices", return_value=(97, 0, 0)) as mock_upsert:

        ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)

        assert res.status == "partial"
        assert res.matched_count == 97
        assert res.previous_close_missing_count == 97
        mock_upsert.assert_called_once()
        mock_email.send_email.assert_called_once()


# ---------------------------------------------------------------------------
# 4. MultiIndex, Duplicate & Metric Tests
# ---------------------------------------------------------------------------


def test_single_level_dataframe_rejected_for_multi_ticker_request():
    expected_tickers = {"AAPL", "MSFT"}
    df_single = pd.DataFrame({"Close": [150.0, 151.0], "Open": [149.0, 150.0]})

    sub_a, _ = extract_ticker_dataframe(df_single, "AAPL", expected_tickers_count=2)
    sub_b, _ = extract_ticker_dataframe(df_single, "MSFT", expected_tickers_count=2)

    assert sub_a is None
    assert sub_b is None


def test_multiindex_reversed_price_level_detection():
    trading_date = date(2026, 8, 21)
    ny_tz = ZoneInfo("America/New_York")
    dt_session = datetime(2026, 8, 21, 14, 30, tzinfo=ny_tz)

    data = {
        ("Close", "AAPL"): ["152.50"],
        ("Open", "AAPL"): ["150.00"],
    }
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([dt_session]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Price", "Ticker"])

    expected = {"AAPL"}
    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra,
        daily_df=pd.DataFrame(),
        expected_tickers=expected,
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert len(valid) == 1
    assert valid[0].price == Decimal("152.500000000000")
    assert unreq == []  # 'CLOSE' must NOT be counted as unrequested
    assert obs_count == 1


def test_duplicate_close_columns_treated_as_invalid_and_not_selected():
    trading_date = date(2026, 8, 21)
    dt_session = datetime(2026, 8, 21, 14, 30, tzinfo=ZoneInfo("America/New_York"))

    cols = pd.MultiIndex.from_tuples([("AAPL", "Close"), ("AAPL", "Close")])
    df_intra = pd.DataFrame([["150.0", "155.0"]], columns=cols, index=pd.DatetimeIndex([dt_session]))

    expected = {"AAPL"}
    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra,
        daily_df=pd.DataFrame(),
        expected_tickers=expected,
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert dup == 1
    assert inv == 1
    assert len(valid) == 0
    assert any("duplicate Close columns" in w for w in warns)


def test_observed_ticker_count_and_response_count_metrics():
    trading_date = date(2026, 8, 21)
    dt_session = datetime(2026, 8, 21, 14, 30, tzinfo=ZoneInfo("America/New_York"))

    all_97_insts = _make_sample_nasdaq_instruments(97)
    df_intra_80, _ = _make_sample_nasdaq_dataframes(all_97_insts[:80], trading_date=trading_date)
    expected_97 = {i.yahoo_ticker for i in all_97_insts}

    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra_80,
        daily_df=pd.DataFrame(),
        expected_tickers=expected_97,
        trading_date=trading_date,
        reference_time=dt_session,
    )

    assert obs_count == 80
    assert len(valid) == 80
    assert inv == 17
    assert len(unreq) == 0


# ---------------------------------------------------------------------------
# 5. Decimal Precision & Zero Float Tests
# ---------------------------------------------------------------------------


def test_high_precision_decimal_no_float_drift():
    trading_date = date(2026, 8, 21)
    ny_tz = ZoneInfo("America/New_York")
    dt_session = datetime(2026, 8, 21, 14, 30, tzinfo=ny_tz)

    exact_price_str = "12345.123456789012"
    exact_prev_str = "12000.000000000000"

    data = {("AAPL", "Close"): [exact_price_str]}
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([dt_session]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Ticker", "Price"])

    daily_data = {("AAPL", "Close"): [exact_prev_str]}
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
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra,
        daily_df=df_daily,
        expected_tickers={"AAPL"},
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


def test_freshness_session_window_rules_nasdaq():
    trading_date = date(2026, 8, 21)
    ny_tz = ZoneInfo("America/New_York")

    # Regular session: 09:30 - 16:00 EDT (13:30 - 20:00 UTC)
    m_open = datetime(2026, 8, 21, 9, 30, tzinfo=ny_tz).astimezone(timezone.utc)
    m_close = datetime(2026, 8, 21, 16, 0, tzinfo=ny_tz).astimezone(timezone.utc)
    session = NasdaqSessionInfo(
        trading_date=trading_date, is_trading_day=True, market_open=m_open, market_close=m_close
    )

    t_cron_ref = datetime(2026, 8, 21, 16, 30, tzinfo=ny_tz).astimezone(timezone.utc)

    # 1. 15:55 ET -> accept
    t_1555_bar = datetime(2026, 8, 21, 15, 55, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_1555_bar, session, t_cron_ref)
    assert fresh is True

    # 2. 16:00 ET (regular session market close) -> accept
    t_1600_bar = datetime(2026, 8, 21, 16, 0, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_1600_bar, session, t_cron_ref)
    assert fresh is True

    # 3. 16:01 ET -> reject (after-hours)
    t_1601_bar = datetime(2026, 8, 21, 16, 1, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1601_bar, session, t_cron_ref)
    assert fresh is False
    assert "after-hours" in msg

    # 4. 16:05 ET -> reject (after-hours)
    t_1605_bar = datetime(2026, 8, 21, 16, 5, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1605_bar, session, t_cron_ref)
    assert fresh is False
    assert "after-hours" in msg

    # 5. 16:15 ET -> reject (after-hours)
    t_1615_bar = datetime(2026, 8, 21, 16, 15, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1615_bar, session, t_cron_ref)
    assert fresh is False
    assert "after-hours" in msg

    # 6. 09:29 ET -> reject (pre-market)
    t_0929_bar = datetime(2026, 8, 21, 9, 29, tzinfo=ny_tz).astimezone(timezone.utc)
    t_open_ref = datetime(2026, 8, 21, 9, 35, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_0929_bar, session, t_open_ref)
    assert fresh is False
    assert "pre-market" in msg

    # 7. 09:30 ET -> accept (market open)
    t_0930_bar = datetime(2026, 8, 21, 9, 30, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_0930_bar, session, t_open_ref)
    assert fresh is True

    # 8. 14:00 ET on regular day -> rejected in closing slot (>60m before close)
    t_1400_bar = datetime(2026, 8, 21, 14, 0, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1400_bar, session, t_cron_ref)
    assert fresh is False
    assert "too early before market close" in msg

    # 9. Early close day (close at 13:00 ET)
    m_open_early = datetime(2026, 11, 27, 9, 30, tzinfo=ny_tz).astimezone(timezone.utc)
    m_close_early = datetime(2026, 11, 27, 13, 0, tzinfo=ny_tz).astimezone(timezone.utc)
    early_session = NasdaqSessionInfo(
        trading_date=date(2026, 11, 27),
        is_trading_day=True,
        market_open=m_open_early,
        market_close=m_close_early,
        early_close=True,
    )
    t_early_cron_ref = datetime(2026, 11, 27, 13, 30, tzinfo=ny_tz).astimezone(timezone.utc)

    # 12:55 ET -> accept
    t_1255_bar = datetime(2026, 11, 27, 12, 55, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_1255_bar, early_session, t_early_cron_ref)
    assert fresh is True

    # 13:00 ET -> accept
    t_1300_bar = datetime(2026, 11, 27, 13, 0, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, _ = is_price_bar_fresh(t_1300_bar, early_session, t_early_cron_ref)
    assert fresh is True

    # 13:05 ET -> reject (after-hours on early close day)
    t_1305_bar = datetime(2026, 11, 27, 13, 5, tzinfo=ny_tz).astimezone(timezone.utc)
    fresh, msg = is_price_bar_fresh(t_1305_bar, early_session, t_early_cron_ref)
    assert fresh is False
    assert "after-hours" in msg


def test_observed_ticker_count_with_unused_multiindex_levels():
    """Verify that unused categorical levels in MultiIndex do not inflate observed_ticker_count or unrequested tickers."""
    trading_date = date(2026, 8, 21)
    ny_tz = ZoneInfo("America/New_York")
    dt_session = datetime(2026, 8, 21, 10, 30, tzinfo=ny_tz)

    # 1. MultiIndex created with both AAA and BBB
    tuples = [("AAA", "Close"), ("BBB", "Close")]
    cols = pd.MultiIndex.from_tuples(tuples, names=["Ticker", "Price"])
    full_df = pd.DataFrame([[150.0, 200.0]], index=pd.DatetimeIndex([dt_session]), columns=cols)

    # 2. Slice to only AAA/Close
    filtered_df = full_df[[("AAA", "Close")]]
    # In pandas, filtered_df.columns.levels[0] still contains 'BBB', but get_level_values(0) only has 'AAA'
    assert "BBB" in filtered_df.columns.levels[0]
    assert list(filtered_df.columns.get_level_values(0)) == ["AAA"]

    expected = {"AAA", "BBB"}
    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=filtered_df,
        daily_df=pd.DataFrame(),
        expected_tickers=expected,
        trading_date=trading_date,
        reference_time=datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc),
    )

    # 3. BBB must NOT be counted as observed, must NOT inflate response_count, and must NOT be unrequested
    assert obs_count == 1
    assert len(valid) == 1
    assert valid[0].symbol == "AAA"
    assert inv == 1  # BBB is missing/invalid
    assert unreq == []


def test_observed_ticker_count_with_extra_unrequested_ticker():
    """Verify that 97 expected + 1 actual extra ticker produces response_count=98 and unrequested_tickers=['EXTRA']."""
    trading_date = date(2026, 8, 21)
    all_97_insts = _make_sample_nasdaq_instruments(97)
    extra_inst = NasdaqDbInstrument(
        id=uuid4(),
        symbol="EXTRA",
        company_name="Extra Co",
        currency="USD",
        is_active=True,
    )
    all_98 = all_97_insts + [extra_inst]

    df_intra_98, _ = _make_sample_nasdaq_dataframes(all_98, trading_date=trading_date)
    expected_97 = {i.yahoo_ticker for i in all_97_insts}

    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra_98,
        daily_df=pd.DataFrame(),
        expected_tickers=expected_97,
        trading_date=trading_date,
        reference_time=datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc),
    )

    assert obs_count == 98
    assert len(valid) == 97
    assert inv == 0
    assert unreq == ["EXTRA"]


def test_wrong_trading_date_bar_rejected():
    """Verify that an intraday bar with a calendar date different from session.trading_date is strictly rejected."""
    trading_date = date(2026, 8, 21)
    ny_tz = ZoneInfo("America/New_York")

    # Bar is from Aug 20 (yesterday)
    dt_yesterday = datetime(2026, 8, 20, 15, 30, tzinfo=ny_tz)

    data = {("AAPL", "Close"): ["150.0"]}
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([dt_yesterday]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Ticker", "Price"])

    m_open = datetime(2026, 8, 21, 9, 30, tzinfo=ny_tz).astimezone(timezone.utc)
    m_close = datetime(2026, 8, 21, 16, 0, tzinfo=ny_tz).astimezone(timezone.utc)
    session = NasdaqSessionInfo(
        trading_date=trading_date, is_trading_day=True, market_open=m_open, market_close=m_close
    )

    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra,
        daily_df=pd.DataFrame(),
        expected_tickers={"AAPL"},
        trading_date=trading_date,
        session_info=session,
        reference_time=datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc),
    )

    assert len(valid) == 0
    assert inv == 1
    assert any("does not match session trading date" in w or "no fresh intraday data" in w for w in warns)


def test_order_independent_intraday_and_daily_selection():
    trading_date = date(2026, 8, 21)
    ny_tz = ZoneInfo("America/New_York")

    t1 = datetime(2026, 8, 21, 13, 0, tzinfo=ny_tz)
    t2 = datetime(2026, 8, 21, 14, 0, tzinfo=ny_tz)
    t3 = datetime(2026, 8, 21, 12, 0, tzinfo=ny_tz)

    data = {("AAPL", "Close"): ["150.0", "155.0", "145.0"]}
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([t1, t2, t3]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Ticker", "Price"])

    d1 = datetime(2026, 8, 18)
    d2 = datetime(2026, 8, 20)
    d3 = datetime(2026, 8, 19)
    daily_data = {("AAPL", "Close"): ["146.0", "152.0", "149.0"]}
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
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra,
        daily_df=df_daily,
        expected_tickers={"AAPL"},
        trading_date=trading_date,
        reference_time=datetime(2026, 8, 21, 14, 10, tzinfo=ny_tz),
    )

    assert len(valid) == 1
    assert valid[0].price == Decimal("155.000000000000")
    assert valid[0].previous_close == Decimal("152.000000000000")


# ---------------------------------------------------------------------------
# 7. End-to-End Orchestration & Completeness Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "active_count, matched_count, expected_status",
    [
        (97, 97, "success"),
        (97, 78, "partial"),
        (97, 77, "failed"),
        (97, 0, "failed"),
    ],
)
def test_completeness_boundary_conditions(active_count, matched_count, expected_status):
    insts = _make_sample_nasdaq_instruments(97, active_count=active_count)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts[:matched_count], trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == expected_status
        assert res.matched_count == matched_count
        assert res.requested_active_count == active_count


def test_dry_run_executes_zero_writes_and_zero_emails():
    insts = _make_sample_nasdaq_instruments(97)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices") as mock_upsert, \
         patch.object(NasdaqLatestPricesRepository, "mark_stale_prices") as mock_stale:

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)

        assert res.status == "success"
        assert res.dry_run is True
        mock_upsert.assert_not_called()
        mock_stale.assert_not_called()
        mock_email.send_email.assert_not_called()


def test_apply_db_partial_sends_warning_email_and_marks_missing_stale():
    insts = _make_sample_nasdaq_instruments(97)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts[:85], trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices", return_value=(85, 0, 0)) as mock_upsert, \
         patch.object(NasdaqLatestPricesRepository, "mark_stale_prices", return_value=12) as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)

        assert res.status == "partial"
        assert res.matched_count == 85
        assert res.missing_count == 12
        assert res.email_status == "sent"
        mock_upsert.assert_called_once()
        mock_stale.assert_called_once()
        mock_email.send_email.assert_called_once()
        call_args = mock_email.send_email.call_args.kwargs
        assert "Kısmi güncelleme" in call_args["subject"]


def test_apply_db_fails_closed_on_catalog_drift():
    insts_before = _make_sample_nasdaq_instruments(97)
    insts_after = [i.model_copy() for i in insts_before]
    insts_after[0] = insts_after[0].model_copy(update={"symbol": "DRIFTED"})

    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts_before, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", side_effect=[insts_before, insts_after]), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices") as mock_upsert:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)

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
    insts = _make_sample_nasdaq_instruments(97)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts[:85], trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_email.send_email.return_value = True
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = stateful_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices", return_value=(85, 0, 0)), \
         patch.object(NasdaqLatestPricesRepository, "mark_stale_prices", side_effect=RuntimeError("SQL failure with token=SecretKey999")):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)

        assert res.status == "failed"
        assert any(tx.status == "rolled_back" for tx in stateful_conn.transactions)
        mock_email.send_email.assert_called_once()
        assert "SecretKey999" not in (res.error or "")


def test_sanitize_error_message_masks_secrets():
    exc1 = RuntimeError("Connection failed to postgresql://user:SecretDbPass123@supabase.com:5432/db")
    assert sanitize_error_message(exc1) == "Veritabanı veya servis erişim hatası."

    exc2 = TimeoutError("HTTP request timed out connecting to https://query1.finance.yahoo.com")
    assert sanitize_error_message(exc2) == "Veritabanı veya servis erişim hatası."

    exc3 = ValueError("Catalog validation failed: duplicate symbol AAPL")
    assert sanitize_error_message(exc3) == "Catalog validation failed: duplicate symbol AAPL"



def test_smtp_exception_handling_masks_secrets(caplog):
    caplog.set_level(logging.ERROR)
    insts = _make_sample_nasdaq_instruments(97)
    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=date(2026, 8, 21), is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.side_effect = RuntimeError("Yahoo API down")

    mock_email = MagicMock()
    mock_email.send_email.side_effect = RuntimeError("SMTP failed with token=SecretSmtpToken999")
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts):

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email)

        assert res.status == "failed"
        assert res.email_status == "failed"
        assert res.email_error == "E-posta gönderimi sırasında hata oluştu."
        assert "SecretSmtpToken999" not in caplog.text


def test_nasdaq_catalog_json_not_read_at_runtime():
    """Verify that data/nasdaq100_symbols.json is not accessed during job execution."""
    insts = _make_sample_nasdaq_instruments(97)
    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=date(2026, 8, 22), is_trading_day=False),
        None,
        "market_closed",
    )
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    with patch("builtins.open", side_effect=AssertionError("JSON file must not be opened at runtime!")) as mock_open, \
         patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts):

        res = run_job(["--dry-run"], provider=mock_provider)
        assert res.status == "skipped"


def test_service_handles_missing_previous_close_without_stale():
    insts = _make_sample_nasdaq_instruments(1)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts, trading_date=trading_date, include_prev_close=False)

    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra,
        daily_df=df_daily,
        expected_tickers={"NDQ000"},
        trading_date=trading_date,
        reference_time=datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc),
    )

    assert len(valid) == 1
    assert p_match == 0
    assert p_miss == 1
    assert valid[0].price == Decimal("152.500000000000")
    assert valid[0].previous_close is None
    assert valid[0].change_amount is None
    assert valid[0].change_percent is None


def test_service_rejects_nan_infinity_and_non_positive_prices():
    trading_date = date(2026, 8, 21)
    ny_tz = ZoneInfo("America/New_York")
    dt_session = datetime(2026, 8, 21, 10, 30, tzinfo=ny_tz)

    data = {
        ("AAPL", "Close"): [float("nan")],
        ("MSFT", "Close"): [0.0],
        ("NVDA", "Close"): [-10.0],
    }
    df_intra = pd.DataFrame(data, index=pd.DatetimeIndex([dt_session]))
    df_intra.columns = pd.MultiIndex.from_tuples(df_intra.columns, names=["Ticker", "Price"])

    (
        valid,
        warns,
        inv,
        dup,
        unreq,
        p_match,
        p_miss,
        obs_count,
    ) = NasdaqPricesService.normalize_nasdaq_prices(
        intraday_df=df_intra,
        daily_df=pd.DataFrame(),
        expected_tickers={"AAPL", "MSFT", "NVDA"},
        trading_date=trading_date,
    )

    assert len(valid) == 0
    assert inv == 3


def test_match_prices_with_active_instruments():
    inst1 = NasdaqDbInstrument(id=uuid4(), symbol="AAPL", company_name="Apple", is_active=True)
    inst2 = NasdaqDbInstrument(id=uuid4(), symbol="MSFT", company_name="Microsoft", is_active=True)
    inst3 = NasdaqDbInstrument(id=uuid4(), symbol="GOOGL", company_name="Google", is_active=True)

    t_now = datetime(2026, 8, 21, 14, 30, tzinfo=timezone.utc)
    prices = [
        NasdaqNormalizedPrice(
            symbol="AAPL",
            yahoo_ticker="AAPL",
            price=Decimal("180.000000000000"),
            price_at=t_now,
        ),
        NasdaqNormalizedPrice(
            symbol="MSFT",
            yahoo_ticker="MSFT",
            price=Decimal("350.000000000000"),
            price_at=t_now,
        ),
    ]

    matched, missing, unmatched = NasdaqPricesService.match_prices_with_instruments(
        [inst1, inst2, inst3], prices, unrequested_tickers=["EXTRA"]
    )

    assert len(matched) == 2
    assert matched[0].symbol == "AAPL"
    assert matched[1].symbol == "MSFT"
    assert len(missing) == 1
    assert missing[0].symbol == "GOOGL"
    assert unmatched == ["EXTRA"]


def test_plan_nasdaq_price_upserts_idempotent_merge_rules():
    uid1 = uuid4()
    t_stored = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)
    t_newer = datetime(2026, 8, 21, 14, 30, tzinfo=timezone.utc)
    t_older = datetime(2026, 8, 21, 13, 45, tzinfo=timezone.utc)

    stored_map = {
        uid1: {
            "price": Decimal("180.000000000000"),
            "currency": "USD",
            "previous_close": Decimal("175.000000000000"),
            "change_amount": Decimal("5.000000000000"),
            "change_percent": Decimal("2.85714286"),
            "price_at": t_stored,
            "provider_code": "YAHOO_FINANCE",
            "is_stale": False,
            "stale_reason": None,
        },
    }

    incoming_items = [
        NasdaqPriceSyncItem(
            instrument_id=uid1,
            symbol="AAPL",
            yahoo_ticker="AAPL",
            price=Decimal("182.000000000000"),
            currency="USD",
            previous_close=Decimal("175.000000000000"),
            change_amount=Decimal("7.000000000000"),
            change_percent=Decimal("4.00000000"),
            price_at=t_newer,
            provider_code="YAHOO_FINANCE",
        ),
    ]

    to_upsert, unchanged_cnt, stale_skip_cnt = plan_nasdaq_price_upserts(stored_map, incoming_items)
    assert len(to_upsert) == 1
    assert unchanged_cnt == 0
    assert stale_skip_cnt == 0

    older_item = NasdaqPriceSyncItem(
        instrument_id=uid1,
        symbol="AAPL",
        yahoo_ticker="AAPL",
        price=Decimal("178.000000000000"),
        currency="USD",
        price_at=t_older,
        provider_code="YAHOO_FINANCE",
    )
    to_up2, unch2, stale_skip2 = plan_nasdaq_price_upserts(stored_map, [older_item])
    assert len(to_up2) == 0
    assert unch2 == 0
    assert stale_skip2 == 1


def test_mark_stale_prices_rejects_invalid_reason_without_sql():
    mock_conn = MagicMock()
    with pytest.raises(ValueError) as exc_info:
        NasdaqLatestPricesRepository.mark_stale_prices(
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

    count = NasdaqLatestPricesRepository.mark_stale_prices(
        mock_conn,
        [uid_with_row, uid_without_row],
        reason="price_not_published",
        stored_map=stored_map,
    )

    mock_conn.cursor.assert_called_once()
    assert count == 1


def test_dry_run_db_read_error_fails_closed_safely():
    insts = _make_sample_nasdaq_instruments(97)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", side_effect=RuntimeError("DB error with token=Secret123")):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)

        assert res.status == "failed"
        assert res.dry_run is True
        assert "Secret123" not in (res.error or "")
        mock_email.send_email.assert_not_called()


def test_apply_db_success_sends_no_email():
    insts = _make_sample_nasdaq_instruments(97)
    trading_date = date(2026, 8, 21)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices", return_value=(97, 0, 0)) as mock_upsert:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)

        assert res.status == "success"
        assert res.dry_run is False
        assert res.email_status is None
        mock_upsert.assert_called_once()
        mock_email.send_email.assert_not_called()


def test_github_actions_cron_schedule():
    workflow_path = ".github/workflows/nasdaq-latest-prices.yml"
    with open(workflow_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    on_section = data.get("on") or data.get(True)
    cron_expr = on_section["schedule"][0]["cron"]
    assert cron_expr == "10,40 13-21 * * 1-5"


# ---------------------------------------------------------------------------
# 11. Real 23 Symbols & Dynamic 120 Instruments Universe Tests
# ---------------------------------------------------------------------------

REAL_23_NASDAQ_SYMBOLS = [
    "ALAB", "ALNY", "AXON", "CCEP", "CRWV", "FER", "HONA", "LIN", "LITE", "MPWR",
    "MSTR", "NBIS", "NVDA", "PLTR", "RKLB", "SHOP", "SNDK", "SPCX", "STX", "TER",
    "TRI", "WDC", "WMT"
]

ORIGINAL_97_SYMBOLS = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR",
    "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO", "CSGP",
    "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DLTR", "DXCM", "EA", "EXC", "FANG",
    "FAST", "FTNT", "GEHC", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "ILMN", "INTC",
    "INTU", "ISRG", "KDP", "KHC", "KLAC", "LRCX", "LULU", "MAR", "MCHP", "MDB",
    "MDLZ", "MELI", "META", "MNST", "MRNA", "MRVL", "MSFT", "MU", "NFLX", "NXPI",
    "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PYPL", "QCOM",
    "REGN", "ROP", "ROST", "SBUX", "SMCI", "SNPS", "TEAM", "TMUS", "TSLA", "TTWO",
    "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS"
]


def _build_real_120_nasdaq_catalog() -> List[NasdaqDbInstrument]:
    """Build the complete 120-instrument NASDAQ catalog representing live Supabase state."""
    catalog: List[NasdaqDbInstrument] = []
    # 97 original symbols (95 active, 2 inactive: ANSS, EA)
    for sym in ORIGINAL_97_SYMBOLS:
        is_active = sym not in ("ANSS", "EA")
        catalog.append(
            NasdaqDbInstrument(
                id=uuid4(),
                symbol=sym,
                company_name=f"Company {sym}",
                currency="USD",
                is_active=is_active,
            )
        )
    # 23 new symbols (all 23 active)
    for sym in REAL_23_NASDAQ_SYMBOLS:
        catalog.append(
            NasdaqDbInstrument(
                id=uuid4(),
                symbol=sym,
                company_name=f"Company {sym}",
                currency="USD",
                is_active=True,
            )
        )
    return catalog


def test_catalog_validation_with_real_120_catalog_and_dynamic_counts():
    """Verify that catalog validation accepts 120 instruments including all 23 real symbols."""
    catalog = _build_real_120_nasdaq_catalog()
    assert len(catalog) == 120
    active_symbols = {i.symbol for i in catalog if i.is_active}
    assert len(active_symbols) == 118
    # Confirm all 23 real symbols are present and active
    for sym in REAL_23_NASDAQ_SYMBOLS:
        assert sym in active_symbols

    is_valid, errors = validate_nasdaq_catalog(catalog)
    assert is_valid is True
    assert len(errors) == 0

    # Empty list rejected
    is_valid_empty, errors_empty = validate_nasdaq_catalog([])
    assert is_valid_empty is False
    assert any("Catalog is empty" in e for e in errors_empty)


def test_job_processes_all_23_new_symbols_cleanly():
    """Verify mock provider returns prices for all active instruments and all 23 new symbols are matched and priced."""
    catalog = _build_real_120_nasdaq_catalog()
    trading_date = date(2026, 8, 21)
    active_insts = [i for i in catalog if i.is_active]
    assert len(active_insts) == 118

    df_intra, df_daily = _make_sample_nasdaq_dataframes(active_insts, trading_date=trading_date)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=catalog), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices", return_value=(118, 0, 0)) as mock_upsert, \
         patch.object(NasdaqLatestPricesRepository, "mark_stale_prices", return_value=2) as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == "success"
        assert res.requested_active_count == 118
        assert res.matched_count == 118
        assert res.inserted_or_updated_count == 118
        mock_upsert.assert_called_once()
        mock_stale.assert_called_once()


def test_missing_single_symbol_price_does_not_drop_other_symbols():
    """Verify that if 1 symbol has no price data, the other 117 symbols succeed and missing symbol is marked stale."""
    catalog = _build_real_120_nasdaq_catalog()
    trading_date = date(2026, 8, 21)
    active_insts = [i for i in catalog if i.is_active]

    # Drop CRWV from intraday response
    df_intra, df_daily = _make_sample_nasdaq_dataframes(active_insts, trading_date=trading_date)
    crwv_cols = [c for c in df_intra.columns if c[0] == "CRWV"]
    df_intra_dropped = df_intra.drop(columns=crwv_cols)

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.return_value = df_intra_dropped
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=catalog), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices", return_value=(117, 0, 0)) as mock_upsert, \
         patch.object(NasdaqLatestPricesRepository, "mark_stale_prices", return_value=1) as mock_stale:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        # Completeness is 117/118 (99.1% >= 80%), so partial
        assert res.status == "partial"
        assert res.requested_active_count == 118
        assert res.matched_count == 117
        assert res.missing_count == 1
        assert res.inserted_or_updated_count == 117
        mock_upsert.assert_called_once()
        mock_stale.assert_called()


def test_job_fails_safely_when_catalog_is_completely_empty():
    """Verify that an empty catalog fails safely before calling provider or making database writes."""
    mock_provider = MagicMock()
    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=[]):

        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == "failed"
        assert "Catalog validation failed" in res.error
        mock_provider.resolve_nasdaq_session.assert_not_called()
        mock_provider.fetch_intraday.assert_not_called()


def test_nasdaq_provider_request_executed_lifecycle_paths():
    """Verify job-level provider_request_executed lifecycle paths for NASDAQ price job."""
    mock_provider = MagicMock()
    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__.return_value = MagicMock()

    ref_time = datetime(2026, 8, 21, 14, 35, tzinfo=timezone.utc)
    trading_date = date(2026, 8, 21)

    # 1. Pre-provider catalog error -> provider_request_executed is False
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=[]):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == "failed"
        assert res.provider_request_executed is False

    valid_catalog = [
        NasdaqDbInstrument(id=uuid4(), symbol="AAPL", company_name="Apple", currency="USD", is_active=True),
    ]

    # 2. Pre-provider session skip (outside_market_window) -> provider_request_executed is False
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        None,
        "outside_market_window",
    )
    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=valid_catalog):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == "skipped"
        assert res.skip_reason == "outside_market_window"
        assert res.provider_request_executed is False

    # 3. Provider fetch raises exception -> provider_request_executed is True
    mock_provider.resolve_nasdaq_session.return_value = (
        NasdaqSessionInfo(trading_date=trading_date, is_trading_day=True),
        "regular_session",
        None,
    )
    mock_provider.fetch_intraday.side_effect = RuntimeError("Yahoo NASDAQ network timeout")

    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=valid_catalog):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.status == "failed"
        assert res.provider_request_executed is True

    # 4. Provider fetch succeeds -> provider_request_executed is True
    df_intra, df_daily = _make_sample_nasdaq_dataframes(valid_catalog, trading_date=trading_date)
    mock_provider.fetch_intraday.side_effect = None
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=valid_catalog):
        res = run_job(["--dry-run"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)
        assert res.provider_request_executed is True


# ---------------------------------------------------------------------------
# 12. Wide Market Window & Delay Resilience Tests
# ---------------------------------------------------------------------------


def test_nasdaq_delayed_post_close_executes_and_applies_prices():
    """Verify delayed post-close execution (e.g. running 2h22m after close) fetches and writes prices cleanly."""
    insts = _make_sample_nasdaq_instruments(97)
    trading_date = date(2026, 8, 21)
    # Regular-session 16:00 EDT closing bar = 20:00 UTC
    closing_bar = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)
    df_intra, df_daily = _make_sample_nasdaq_dataframes(insts, trading_date=trading_date, bar_time=closing_bar)

    # 18:22 EDT = 22:22 UTC (market_close was 16:00 EDT = 20:00 UTC, so 2 hours 22 minutes delayed)
    ref_time = datetime(2026, 8, 21, 22, 22, tzinfo=timezone.utc)

    provider = YahooNasdaqPriceProvider()
    sess, window, skip_reason = provider.resolve_nasdaq_session(ref_time)
    assert sess.is_trading_day is True
    assert window == "post_close"
    assert skip_reason is None

    mock_provider = MagicMock()
    mock_provider.resolve_nasdaq_session.return_value = (sess, window, skip_reason)
    mock_provider.fetch_intraday.return_value = df_intra
    mock_provider.fetch_daily.return_value = df_daily

    mock_email = MagicMock()
    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__.return_value = mock_conn

    with patch("app.jobs.nasdaq_latest_prices_job.get_db_connection", return_value=mock_conn_ctx), \
         patch.object(NasdaqLatestPricesRepository, "get_all_nasdaq_instruments", return_value=insts), \
         patch.object(NasdaqLatestPricesRepository, "get_stored_latest_prices", return_value={}), \
         patch.object(NasdaqLatestPricesRepository, "upsert_prices", return_value=(97, 0, 0)) as mock_upsert:

        res = run_job(["--apply-db"], provider=mock_provider, email_service=mock_email, reference_time=ref_time)

        assert res.status == "success"
        assert res.execution_window == "post_close"
        assert res.provider_request_executed is True
        assert res.inserted_or_updated_count == 97
        mock_upsert.assert_called_once()
        mock_email.send_email.assert_not_called()


def test_nasdaq_delayed_post_close_regular_session_bar_is_accepted_as_fresh():
    """Verify that when a post-close run occurs hours after market close, the regular-session closing bar is accepted as fresh."""
    session = NasdaqSessionInfo(
        trading_date=date(2026, 8, 21),
        is_trading_day=True,
        market_open=datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc),   # 09:30 EDT
        market_close=datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc),   # 16:00 EDT
        market_window_start=datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc),
        market_window_end=datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc),
    )

    # Job executes at 22:52 UTC (18:52 EDT, 2h52m after close)
    ref_time_utc = datetime(2026, 8, 21, 22, 52, tzinfo=timezone.utc)

    # Regular session closing bar timestamp at 20:00 UTC (16:00 EDT)
    closing_bar_time = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)
    is_fresh, reason = is_price_bar_fresh(closing_bar_time, session, ref_time_utc)
    assert is_fresh is True
    assert reason == "OK"

    # Regular session bar at 15:30 EDT = 19:30 UTC (30m before close <= 60m max early)
    bar_30m_before_close = datetime(2026, 8, 21, 19, 30, tzinfo=timezone.utc)
    is_fresh, reason = is_price_bar_fresh(bar_30m_before_close, session, ref_time_utc)
    assert is_fresh is True
    assert reason == "OK"

    # Regular session bar too early (e.g. 14:00 EDT = 18:00 UTC, 2h before close > 60m)
    bar_too_early = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
    is_fresh_early, reason_early = is_price_bar_fresh(bar_too_early, session, ref_time_utc)
    assert is_fresh_early is False
    assert "Closing bar is too early" in reason_early


def test_nasdaq_pre_market_and_after_hours_bars_are_strictly_rejected():
    """Verify that bars timestamped before market_open (pre-market) or after market_close (after-hours) are rejected."""
    session = NasdaqSessionInfo(
        trading_date=date(2026, 8, 21),
        is_trading_day=True,
        market_open=datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc),   # 09:30 EDT
        market_close=datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc),   # 16:00 EDT
    )
    ref_time_utc = datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc)

    # Pre-market bar (09:15 EDT = 13:15 UTC)
    pre_market_bar = datetime(2026, 8, 21, 13, 15, tzinfo=timezone.utc)
    is_fresh, reason = is_price_bar_fresh(pre_market_bar, session, ref_time_utc)
    assert is_fresh is False
    assert "pre-market" in reason

    # After-hours bar (16:30 EDT = 20:30 UTC)
    after_hours_bar = datetime(2026, 8, 21, 20, 30, tzinfo=timezone.utc)
    is_fresh, reason = is_price_bar_fresh(after_hours_bar, session, ref_time_utc)
    assert is_fresh is False
    assert "after-hours" in reason


def test_nasdaq_adapter_produces_execution_window_metrics():
    """Verify NasdaqPricesResultAdapter produces execution_window and window boundary metrics."""
    from app.services.job_tracking.adapters.nasdaq_adapter import (
        NasdaqPricesResultAdapter,
    )

    adapter = NasdaqPricesResultAdapter()
    result = NasdaqPricesJobResult(
        status="success",
        started_at="2026-08-21T14:30:00+00:00",
        finished_at="2026-08-21T14:30:05+00:00",
        duration_seconds=5.0,
        trading_date="2026-08-21",
        session_open="2026-08-21T13:30:00+00:00",
        session_close="2026-08-21T20:00:00+00:00",
        execution_window="regular_session",
        market_window_start="2026-08-21T14:00:00+00:00",
        market_window_end="2026-08-22T02:00:00+00:00",
        requested_active_count=97,
        response_count=97,
        valid_count=97,
        matched_count=97,
        inserted_or_updated_count=97,
        dry_run=False,
        provider_request_executed=True,
    )

    outcome = adapter.to_outcome(result)
    assert outcome.status == "success"
    assert outcome.sync_action == "updated"
    assert outcome.metrics["execution_window"] == "regular_session"
    assert outcome.metrics["market_window_start"] == "2026-08-21T14:00:00+00:00"
    assert outcome.metrics["market_window_end"] == "2026-08-22T02:00:00+00:00"
    assert outcome.metrics["trading_date"] == "2026-08-21"
    assert outcome.metrics["session_open"] == "2026-08-21T13:30:00+00:00"
    assert outcome.metrics["session_close"] == "2026-08-21T20:00:00+00:00"
    assert outcome.metrics["provider_request_executed"] is True


def test_nasdaq_no_legacy_slot_dependency_or_outside_sync_slot():
    """Verify that old slot concepts and outside_sync_slot are completely removed from provider."""
    import app.providers.nasdaq.yahoo_nasdaq_provider as prov_mod

    assert not hasattr(prov_mod, "SLOT_MAX_DELAY_MINUTES")
    assert not hasattr(prov_mod, "NasdaqSyncSlot")

    provider = YahooNasdaqPriceProvider()
    # Check that arbitrary times between 14:00 UTC and 02:00 UTC (next day) on trading day Aug 21, 2026
    # are NEVER rejected with outside_sync_slot
    test_times = [
        datetime(2026, 8, 21, 14, 15, tzinfo=timezone.utc),  # 10:15 EDT
        datetime(2026, 8, 21, 15, 45, tzinfo=timezone.utc),  # 11:45 EDT (was previously outside_sync_slot)
        datetime(2026, 8, 21, 17, 20, tzinfo=timezone.utc),  # 13:20 EDT (was previously outside_sync_slot)
        datetime(2026, 8, 21, 19, 10, tzinfo=timezone.utc),  # 15:10 EDT (was previously outside_sync_slot)
        datetime(2026, 8, 21, 21, 15, tzinfo=timezone.utc),  # 17:15 EDT
        datetime(2026, 8, 21, 23, 30, tzinfo=timezone.utc),  # 19:30 EDT
        datetime(2026, 8, 22, 1, 45, tzinfo=timezone.utc),   # 21:45 EDT
    ]
    for t in test_times:
        sess, window, skip_reason = provider.resolve_nasdaq_session(t)
        assert sess.is_trading_day is True
        assert skip_reason is None
        assert window in ("regular_session", "post_close")
        assert skip_reason != "outside_sync_slot"


def test_nasdaq_production_sources_free_of_legacy_slot_terminology():
    """Verify that production NASDAQ code files and README contain zero legacy slot terms."""
    from pathlib import Path

    production_files = [
        Path("app/models/nasdaq_prices.py"),
        Path("app/providers/nasdaq/yahoo_nasdaq_provider.py"),
        Path("app/providers/nasdaq/__init__.py"),
        Path("app/jobs/nasdaq_latest_prices_job.py"),
        Path("app/services/nasdaq_prices_report.py"),
        Path("app/services/job_tracking/adapters/nasdaq_adapter.py"),
        Path("app/services/nasdaq_prices_service.py"),
        Path("README.md"),
    ]

    legacy_terms = [
        "selected_sync_slot",
        '"selected_slot"',
        "SLOT_MAX_DELAY_MINUTES",
        "NasdaqSyncSlot",
        "valid_slots",
        "active_slot",
        "outside_sync_slot",
        "open+60m",
        "open+180m",
        "open+300m",
        "close+30m",
    ]

    for file_path in production_files:
        assert file_path.is_file(), f"Expected production file {file_path} to exist."
        content = file_path.read_text(encoding="utf-8")
        for term in legacy_terms:
            assert term not in content, f"Legacy term '{term}' found in production file: {file_path}"
