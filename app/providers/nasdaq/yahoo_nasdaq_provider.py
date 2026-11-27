import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf

from app.models.nasdaq_prices import NasdaqExecutionWindow, NasdaqSessionInfo

logger = logging.getLogger(__name__)


class YahooNasdaqPriceFetchError(Exception):
    """Raised when Yahoo Finance NASDAQ batch download fails or returns invalid data."""
    pass


class YahooNasdaqCalendarError(Exception):
    """Raised when querying the NASDAQ market calendar fails due to unexpected errors."""
    pass


class YahooNasdaqPriceProvider:
    """Provider for NASDAQ market calendar checks, execution window resolution, and Yahoo Finance batch price downloads."""

    def __init__(
        self,
        download_fn: Optional[Callable[..., pd.DataFrame]] = None,
        calendar_name: str = "NASDAQ",
        market_tz_name: str = "America/New_York",
    ):
        self._download_fn = download_fn or yf.download
        self._calendar_name = calendar_name
        self._market_tz_name = market_tz_name

    def resolve_nasdaq_session(
        self,
        reference_time: Optional[datetime] = None,
    ) -> Tuple[NasdaqSessionInfo, Optional[NasdaqExecutionWindow], Optional[str]]:
        """Resolve the relevant NASDAQ session and determine if reference_time falls within a valid market window.

        Returns:
            (session_info, execution_window, skip_reason)

        Where execution_window is:
        - "regular_session" if between market_open + 30m and market_close (inclusive)
        - "post_close" if between market_close and market_close + 6h (inclusive)
        - None if outside market execution window

        Where skip_reason is:
        - None if reference_time falls within the valid execution window [market_open + 30m, market_close + 6h]
        - "market_not_ready" if market has opened but 30 minutes have not yet elapsed (or before market open)
        - "outside_market_window" if more than 6 hours have elapsed after market close
        - "market_closed" if weekend or holiday
        """
        ref_utc = reference_time or datetime.now(timezone.utc)
        if ref_utc.tzinfo is None:
            ref_utc = ref_utc.replace(tzinfo=timezone.utc)

        ref_ny = ref_utc.astimezone(ZoneInfo(self._market_tz_name))
        search_start = (ref_ny - timedelta(days=2)).date()
        search_end = (ref_ny + timedelta(days=1)).date()

        try:
            cal = mcal.get_calendar(self._calendar_name)
            schedule = cal.schedule(start_date=search_start, end_date=search_end)
        except Exception as exc:
            logger.error(
                "Failed to query NASDAQ market calendar for search range %s to %s (error_type=%s)",
                search_start,
                search_end,
                type(exc).__name__,
            )
            raise YahooNasdaqCalendarError(
                f"Failed to query NASDAQ calendar: {type(exc).__name__}"
            ) from exc

        if schedule.empty:
            empty_session = NasdaqSessionInfo(
                trading_date=ref_ny.date(),
                is_trading_day=False,
            )
            return empty_session, None, "market_closed"

        candidate_sessions: List[NasdaqSessionInfo] = []
        for _, row in schedule.iterrows():
            m_open = row["market_open"].to_pydatetime()
            m_close = row["market_close"].to_pydatetime()
            if m_open.tzinfo is None:
                m_open = m_open.replace(tzinfo=timezone.utc)
            if m_close.tzinfo is None:
                m_close = m_close.replace(tzinfo=timezone.utc)

            t_date = m_open.astimezone(ZoneInfo(self._market_tz_name)).date()
            is_early = (m_close - m_open) < timedelta(hours=6)
            w_start = m_open + timedelta(minutes=30)
            w_end = m_close + timedelta(hours=6)

            candidate_sessions.append(
                NasdaqSessionInfo(
                    trading_date=t_date,
                    is_trading_day=True,
                    market_open=m_open,
                    market_close=m_close,
                    early_close=is_early,
                    market_window_start=w_start,
                    market_window_end=w_end,
                )
            )

        # 1. Search for a matching session whose [market_window_start, market_window_end] covers ref_utc (inclusive)
        for sess in candidate_sessions:
            if sess.market_window_start and sess.market_window_end:
                if sess.market_window_start <= ref_utc <= sess.market_window_end:
                    if sess.market_close and ref_utc <= sess.market_close:
                        return sess, "regular_session", None
                    else:
                        return sess, "post_close", None

        # 2. If not within any session's execution window, determine appropriate skip reason
        matching_sess = next((s for s in candidate_sessions if s.trading_date == ref_ny.date()), None)
        if matching_sess is not None:
            if matching_sess.market_window_start and ref_utc < matching_sess.market_window_start:
                return matching_sess, None, "market_not_ready"
            if matching_sess.market_window_end and ref_utc > matching_sess.market_window_end:
                return matching_sess, None, "outside_market_window"

        # 3. Market is closed (weekend / holiday on ref_ny date)
        closed_session = NasdaqSessionInfo(
            trading_date=ref_ny.date(),
            is_trading_day=False,
        )
        return closed_session, None, "market_closed"

    def fetch_intraday(self, yahoo_tickers: Sequence[str]) -> pd.DataFrame:
        """Fetch batch intraday (5m) seans data for all specified Yahoo tickers."""
        if not yahoo_tickers:
            return pd.DataFrame()

        ticker_list = sorted(list(set(yahoo_tickers)))
        try:
            df_intraday = self._download_fn(
                ticker_list,
                period="5d",
                interval="5m",
                auto_adjust=False,
                actions=False,
                prepost=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
            if df_intraday is None or df_intraday.empty:
                logger.warning("Yahoo Finance NASDAQ intraday download returned empty DataFrame.")
                return pd.DataFrame()
            return df_intraday
        except Exception as exc:
            logger.error(
                "Yahoo Finance NASDAQ intraday download exception (error_type=%s, ticker_count=%d)",
                type(exc).__name__,
                len(ticker_list),
            )
            raise YahooNasdaqPriceFetchError(
                f"Failed to download NASDAQ intraday prices: {type(exc).__name__}"
            ) from exc

    def fetch_daily(self, yahoo_tickers: Sequence[str]) -> pd.DataFrame:
        """Fetch batch daily (1d) history data for all specified Yahoo tickers (for previous close)."""
        if not yahoo_tickers:
            return pd.DataFrame()

        ticker_list = sorted(list(set(yahoo_tickers)))
        try:
            df_daily = self._download_fn(
                ticker_list,
                period="1mo",
                interval="1d",
                auto_adjust=False,
                actions=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
            if df_daily is None or df_daily.empty:
                logger.warning("Yahoo Finance NASDAQ daily download returned empty DataFrame.")
                return pd.DataFrame()
            return df_daily
        except Exception as exc:
            logger.error(
                "Yahoo Finance NASDAQ daily download exception (error_type=%s, ticker_count=%d)",
                type(exc).__name__,
                len(ticker_list),
            )
            raise YahooNasdaqPriceFetchError(
                f"Failed to download NASDAQ daily prices: {type(exc).__name__}"
            ) from exc

    def fetch_intraday_and_daily(
        self,
        yahoo_tickers: Sequence[str],
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Fetch both intraday and daily data with isolated error handling."""
        df_intraday = self.fetch_intraday(yahoo_tickers)
        try:
            df_daily = self.fetch_daily(yahoo_tickers)
        except Exception:
            df_daily = pd.DataFrame()
        return df_intraday, df_daily
