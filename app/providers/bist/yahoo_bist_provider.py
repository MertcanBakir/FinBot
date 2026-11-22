import logging
from datetime import date, datetime, timezone
from typing import Callable, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf

from app.core.config import settings
from app.models.bist_prices import XistSessionInfo

logger = logging.getLogger(__name__)


class YahooBistPriceFetchError(Exception):
    """Raised when Yahoo Finance batch download fails or returns invalid data."""
    pass


class YahooBistCalendarError(Exception):
    """Raised when querying the Borsa Istanbul market calendar fails due to unexpected errors."""
    pass


class YahooBistPriceProvider:
    """Dedicated provider for Borsa Istanbul calendar checks and Yahoo Finance batch price downloads."""

    def __init__(
        self,
        download_fn: Optional[Callable[..., pd.DataFrame]] = None,
        calendar_name: str = "XIST",
        timezone_name: str = settings.TIMEZONE,
    ):
        self._download_fn = download_fn or yf.download
        self._calendar_name = calendar_name
        self._timezone_name = timezone_name

    def get_xist_session(self, target_date: Optional[date] = None) -> XistSessionInfo:
        """Get Borsa Istanbul (XIST) session details including market_open and market_close times.

        Raises:
            YahooBistCalendarError: If calendar querying fails with an exception (distinguishing from a real holiday/weekend).
        """
        if target_date is None:
            now_dt = datetime.now(ZoneInfo(self._timezone_name))
            target_date = now_dt.date()

        # Weekend check
        if target_date.weekday() in (5, 6):
            return XistSessionInfo(trading_date=target_date, is_trading_day=False)

        try:
            cal = mcal.get_calendar(self._calendar_name)
            schedule = cal.schedule(start_date=target_date, end_date=target_date)
            if schedule.empty:
                return XistSessionInfo(trading_date=target_date, is_trading_day=False)

            row = schedule.iloc[0]
            market_open = row["market_open"].to_pydatetime()
            market_close = row["market_close"].to_pydatetime()

            if market_open.tzinfo is None:
                market_open = market_open.replace(tzinfo=timezone.utc)
            if market_close.tzinfo is None:
                market_close = market_close.replace(tzinfo=timezone.utc)

            return XistSessionInfo(
                trading_date=target_date,
                is_trading_day=True,
                market_open=market_open,
                market_close=market_close,
            )
        except Exception as exc:
            logger.error(
                "Failed to query XIST market calendar for date %s (error_type=%s)",
                target_date,
                type(exc).__name__,
            )
            raise YahooBistCalendarError(
                f"Failed to query XIST calendar: {type(exc).__name__}"
            ) from exc

    def is_xist_trading_day(self, target_date: Optional[date] = None) -> bool:
        """Convenience method to check if a date is a valid XIST trading day."""
        session = self.get_xist_session(target_date)
        return session.is_trading_day

    def fetch_intraday(self, yahoo_tickers: Sequence[str]) -> pd.DataFrame:
        """Fetch batch intraday (5m) seans data for all specified Yahoo tickers."""
        if not yahoo_tickers:
            return pd.DataFrame()

        ticker_list = sorted(list(set(yahoo_tickers)))
        try:
            df_intraday = self._download_fn(
                ticker_list,
                period="1d",
                interval="5m",
                auto_adjust=False,
                actions=False,
                prepost=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
            if df_intraday is None or df_intraday.empty:
                logger.warning("Yahoo Finance intraday download returned empty DataFrame.")
                return pd.DataFrame()
            return df_intraday
        except Exception as exc:
            logger.error(
                "Yahoo Finance intraday download exception (error_type=%s, ticker_count=%d)",
                type(exc).__name__,
                len(ticker_list),
            )
            raise YahooBistPriceFetchError(
                f"Failed to download BIST intraday prices: {type(exc).__name__}"
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
                logger.warning("Yahoo Finance daily download returned empty DataFrame.")
                return pd.DataFrame()
            return df_daily
        except Exception as exc:
            logger.error(
                "Yahoo Finance daily download exception (error_type=%s, ticker_count=%d)",
                type(exc).__name__,
                len(ticker_list),
            )
            raise YahooBistPriceFetchError(
                f"Failed to download BIST daily prices: {type(exc).__name__}"
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
