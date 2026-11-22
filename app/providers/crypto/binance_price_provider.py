import json
import logging
from typing import Any, Dict, List, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

BINANCE_24HR_TICKER_URL = "https://data-api.binance.vision/api/v3/ticker/24hr"


class BinancePriceFetchError(Exception):
    """Raised when fetching prices from Binance public market data fails."""
    pass


class BinancePriceProvider:
    """Public, unauthenticated market data provider for Binance 24hr tickers."""

    def __init__(
        self,
        base_url: str = BINANCE_24HR_TICKER_URL,
        timeout: float = 15.0,
        client: Optional[httpx.Client] = None,
    ):
        self.base_url = base_url
        self.timeout = timeout
        self._client = client

    def fetch_24hr_tickers(self, source_pairs: Sequence[str]) -> List[Dict[str, Any]]:
        """Fetch 24hr ticker data for a batch of Binance trading pairs.

        Args:
            source_pairs: Sequence of trading pair symbols (e.g. ['BTCUSDT', 'ETHUSDT', '币安人生USDT']).

        Returns:
            List of raw 24hr ticker dictionaries returned by Binance.

        Raises:
            BinancePriceFetchError: If the request fails, times out, or returns an error.
        """
        if not source_pairs:
            return []

        # Use compact JSON without spaces and allow Unicode characters
        symbols_param = json.dumps(list(source_pairs), ensure_ascii=False, separators=(",", ":"))
        params = {
            "symbols": symbols_param,
            "type": "FULL",
            "symbolStatus": "TRADING",
        }

        try:
            if self._client is not None:
                resp = self._client.get(self.base_url, params=params, timeout=self.timeout)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(self.base_url, params=params)

            if resp.status_code != 200:
                logger.warning(
                    "Binance 24hr ticker fetch returned HTTP status %d",
                    resp.status_code,
                )
                raise BinancePriceFetchError(
                    f"Binance ticker request failed with HTTP {resp.status_code}"
                )

            data = resp.json()
            if not isinstance(data, list):
                logger.warning("Binance 24hr ticker response format is invalid (expected list)")
                raise BinancePriceFetchError("Invalid response structure from Binance")

            return data

        except httpx.TimeoutException as exc:
            logger.warning("Binance 24hr ticker request timed out (error_type=%s)", type(exc).__name__)
            raise BinancePriceFetchError("Binance request timed out") from None
        except httpx.RequestError as exc:
            logger.warning("Binance 24hr ticker network error (error_type=%s)", type(exc).__name__)
            raise BinancePriceFetchError("Binance network request error") from None
        except json.JSONDecodeError as exc:
            logger.warning("Binance 24hr ticker response JSON parse error (error_type=%s)", type(exc).__name__)
            raise BinancePriceFetchError("Failed to decode Binance JSON response") from None
        except BinancePriceFetchError:
            raise
        except Exception as exc:
            logger.warning("Unexpected error during Binance ticker fetch (error_type=%s)", type(exc).__name__)
            raise BinancePriceFetchError("Unexpected Binance price fetch error") from None
