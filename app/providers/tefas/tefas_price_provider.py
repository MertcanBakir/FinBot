import logging
from datetime import date, datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from pytefas import Crawler

from app.core.config import settings

logger = logging.getLogger(__name__)


class TefasPriceFetchError(RuntimeError):
    """Safe, structured failure raised when TEFAS cannot yield price data."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class TefasPriceProvider:
    """Fetch current and previous TEFAS YAT fund prices with date fallback up to 7 days."""

    def __init__(
        self,
        timeout: int = 60,
        max_retry: int = 5,
        max_lookback_days: int = 7,
        timezone_name: str = settings.TIMEZONE,
        crawler: Optional[Crawler] = None,
    ):
        self.timeout = timeout
        self.max_retry = max_retry
        self.max_lookback_days = max_lookback_days
        self.tz = ZoneInfo(timezone_name)
        self.crawler = crawler or Crawler(timeout=self.timeout, max_retry=self.max_retry)

    def fetch_latest_prices(
        self,
        reference_date: Optional[date] = None,
    ) -> Tuple[pd.DataFrame, date]:
        """Fetch latest non-empty TEFAS YAT prices DataFrame by probing backwards up to max_lookback_days."""
        if reference_date is None:
            reference_date = datetime.now(self.tz).date()

        last_error_type: Optional[str] = None
        for i in range(self.max_lookback_days):
            target_date = reference_date - timedelta(days=i)
            logger.info(
                "Attempting TEFAS current prices fetch for date %s (attempt %d/%d)",
                target_date,
                i + 1,
                self.max_lookback_days,
            )
            try:
                df = self.crawler.fetch(start=target_date, kind="YAT", columns="info")
                if df is not None and not df.empty and len(df) > 0:
                    logger.info(
                        "Successfully fetched %d TEFAS YAT price rows for current date %s",
                        len(df),
                        target_date,
                    )
                    return df, target_date
                logger.info(
                    "TEFAS returned empty price data for date %s, trying previous day...",
                    target_date,
                )
            except Exception as exc:
                last_error_type = type(exc).__name__
                logger.warning(
                    "TEFAS price fetch failed for date %s (error_type=%s)",
                    target_date,
                    last_error_type,
                )

        msg = (
            f"No TEFAS YAT fund price data found within the last {self.max_lookback_days} days "
            f"(from {reference_date})."
        )
        if last_error_type:
            msg += f" (last_error_type={last_error_type})"
        raise TefasPriceFetchError("no_data_found", msg)

    def fetch_previous_prices(
        self,
        current_date: date,
    ) -> Tuple[Optional[pd.DataFrame], Optional[date]]:
        """Fetch previous valid TEFAS YAT prices DataFrame by probing backwards starting from current_date - 1 day."""
        start_date = current_date - timedelta(days=1)
        for i in range(self.max_lookback_days):
            target_date = start_date - timedelta(days=i)
            logger.info(
                "Attempting TEFAS previous prices fetch for date %s (attempt %d/%d)",
                target_date,
                i + 1,
                self.max_lookback_days,
            )
            try:
                df = self.crawler.fetch(start=target_date, kind="YAT", columns="info")
                if df is not None and not df.empty and len(df) > 0:
                    logger.info(
                        "Successfully fetched %d TEFAS YAT price rows for previous date %s",
                        len(df),
                        target_date,
                    )
                    return df, target_date
                logger.info(
                    "TEFAS returned empty previous price data for date %s, trying earlier day...",
                    target_date,
                )
            except Exception as exc:
                logger.warning(
                    "TEFAS previous price fetch failed for date %s (error_type=%s)",
                    target_date,
                    type(exc).__name__,
                )

        logger.warning(
            "No previous TEFAS YAT fund price data found within %d days before %s.",
            self.max_lookback_days,
            current_date,
        )
        return None, None

    def fetch_current_and_previous_prices(
        self,
        reference_date: Optional[date] = None,
    ) -> Tuple[pd.DataFrame, date, Optional[pd.DataFrame], Optional[date]]:
        """Fetch both current and previous valid TEFAS YAT price DataFrames."""
        current_df, current_date = self.fetch_latest_prices(reference_date=reference_date)
        prev_df, prev_date = self.fetch_previous_prices(current_date)
        return current_df, current_date, prev_df, prev_date
