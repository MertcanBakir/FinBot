import asyncio
import logging
from typing import Optional, Tuple

import httpx
import yfinance as yf

logger = logging.getLogger(__name__)

def _clean_price(val: any) -> Optional[float]:
    if val is None:
        return None
    try:
        fval = float(val)
        if fval <= 0:
            return None
        return round(fval, 2)
    except (ValueError, TypeError):
        return None

class GoldProvider:
    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    async def fetch_xau_usd(self) -> Tuple[Optional[float], Optional[str]]:
        """
        Fetches XAU/USD spot price using free APIs with fallbacks.
        Returns (xauusd_price, warning_message).
        """
        # Try Primary API: gold-api.com
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get("https://api.gold-api.com/price/XAU")
                if res.status_code == 200:
                    data = res.json()
                    price = _clean_price(data.get("price"))
                    if price is not None:
                        return price, None
        except Exception as e:
            logger.warning(f"Gold API primary endpoint failed: {e}")

        # Try Secondary API: goldprice.org
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(
                    "https://data-asg.goldprice.org/dbXRates/USD",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("items", [])
                    if items and isinstance(items, list):
                        xau_price = items[0].get("xauPrice")
                        price = _clean_price(xau_price)
                        if price is not None:
                            return price, None
        except Exception as e:
            logger.warning(f"Gold API secondary endpoint failed: {e}")

        # Fallback to Yahoo Finance GC=F (Gold Futures)
        try:
            def _fetch_yf_gold():
                ticker = yf.Ticker("GC=F")
                fast_info = getattr(ticker, "fast_info", {})
                info = getattr(ticker, "info", {})
                val = fast_info.get("lastPrice") or fast_info.get("last_price") or info.get("regularMarketPrice")
                if val is None:
                    hist = ticker.history(period="1d")
                    if not hist.empty:
                        val = hist.iloc[-1].get("Close")
                return _clean_price(val)

            price = await asyncio.to_thread(_fetch_yf_gold)
            if price is not None:
                return price, None
        except Exception as e:
            logger.warning(f"Yahoo Finance gold fallback failed: {e}")

        return None, "XAUUSD data could not be retrieved."
