import logging
from typing import Tuple

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class YahooFxProviderError(Exception):
    """Safe provider boundary for the public Yahoo Finance FX feed."""


class YahooFxProvider:
    ticker = "TRY=X"

    def fetch(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        try:
            intraday = yf.download(
                self.ticker,
                period="5d",
                interval="1h",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=False,
            )
            daily = yf.download(
                self.ticker,
                period="10d",
                interval="1d",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=False,
            )
        except Exception as exc:
            logger.warning("Yahoo FX fetch failed (error_type=%s)", type(exc).__name__)
            raise YahooFxProviderError("Yahoo FX market data could not be fetched") from None

        if not isinstance(intraday, pd.DataFrame) or intraday.empty:
            raise YahooFxProviderError("Yahoo FX intraday response was empty")
        if not isinstance(daily, pd.DataFrame):
            daily = pd.DataFrame()
        return intraday, daily
