import asyncio
import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import yfinance as yf

logger = logging.getLogger(__name__)

def _clean_val(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        fval = float(val)
        if math.isnan(fval) or math.isinf(fval) or fval <= 0:
            return None
        return round(fval, 4)
    except (ValueError, TypeError):
        return None

def get_us_market_status(dt: Optional[datetime] = None) -> str:
    ny_tz = ZoneInfo("America/New_York")
    if dt is None:
        dt = datetime.now(ny_tz)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=ny_tz)
    else:
        dt = dt.astimezone(ny_tz)

    # Weekend check
    if dt.weekday() in (5, 6):
        return "CLOSED"

    try:
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(start_date=dt.date(), end_date=dt.date())
        if schedule.empty:
            return "HOLIDAY"

        market_open = schedule.iloc[0]["market_open"].tz_convert(ny_tz)
        market_close = schedule.iloc[0]["market_close"].tz_convert(ny_tz)

        if market_open <= dt <= market_close:
            return "OPEN"
        else:
            return "CLOSED"
    except Exception as e:
        logger.warning(f"Error determining US market status: {e}")
        return "CLOSED"

def _fetch_sync_data() -> Tuple[Dict[str, Any], Optional[str]]:
    warnings = []
    bist_res = {
        "symbol": "XU100.IS",
        "price": None,
        "previous_close": None,
        "open": None,
        "high": None,
        "low": None,
        "change_percent": None
    }
    usdtry_price = None
    eurtry_price = None
    btc_usd = None
    sp500_res = {"price": None, "previous_close": None, "change_percent": None}
    nasdaq_res = {"price": None, "previous_close": None, "change_percent": None}

    # Fetch tickers
    symbols = ["XU100.IS", "USDTRY=X", "EURTRY=X", "BTC-USD", "^GSPC", "^IXIC"]
    try:
        tickers = yf.Tickers(" ".join(symbols))

        # 1. BIST 100
        try:
            bist_ticker = tickers.tickers.get("XU100.IS") or yf.Ticker("XU100.IS")
            fast_info = getattr(bist_ticker, "fast_info", {})
            info = getattr(bist_ticker, "info", {})

            price = _clean_val(fast_info.get("lastPrice") or fast_info.get("last_price") or info.get("regularMarketPrice"))
            prev_close = _clean_val(fast_info.get("previousClose") or fast_info.get("previous_close") or info.get("regularMarketPreviousClose") or info.get("previousClose"))
            open_price = _clean_val(fast_info.get("open") or info.get("regularMarketOpen") or info.get("open"))
            high = _clean_val(fast_info.get("dayHigh") or fast_info.get("day_high") or info.get("regularMarketDayHigh") or info.get("dayHigh"))
            low = _clean_val(fast_info.get("dayLow") or fast_info.get("day_low") or info.get("regularMarketDayLow") or info.get("dayLow"))

            if price is None or prev_close is None:
                hist = bist_ticker.history(period="5d")
                if not hist.empty:
                    last_row = hist.iloc[-1]
                    if price is None:
                        price = _clean_val(last_row.get("Close"))
                    if open_price is None:
                        open_price = _clean_val(last_row.get("Open"))
                    if high is None:
                        high = _clean_val(last_row.get("High"))
                    if low is None:
                        low = _clean_val(last_row.get("Low"))

                    if prev_close is None and len(hist) >= 2:
                        prev_close = _clean_val(hist.iloc[-2].get("Close"))
                    elif prev_close is None and "Open" in last_row:
                        prev_close = _clean_val(last_row.get("Open"))

            change_pct = None
            if price is not None and prev_close is not None:
                change_pct = round(((price - prev_close) / prev_close) * 100, 2)

            bist_res = {
                "symbol": "XU100.IS",
                "price": price,
                "previous_close": prev_close,
                "open": open_price,
                "high": high,
                "low": low,
                "change_percent": change_pct
            }

            if price is None:
                warnings.append("BIST 100 price data could not be retrieved from Yahoo Finance.")
        except Exception as e:
            logger.error(f"Error fetching BIST 100: {e}")
            warnings.append(f"Error fetching BIST 100: {str(e)}")

        # 2. USDTRY
        try:
            usd_ticker = tickers.tickers.get("USDTRY=X") or yf.Ticker("USDTRY=X")
            fast_info = getattr(usd_ticker, "fast_info", {})
            info = getattr(usd_ticker, "info", {})
            usdtry_price = _clean_val(fast_info.get("lastPrice") or fast_info.get("last_price") or info.get("regularMarketPrice"))
            if usdtry_price is None:
                hist = usd_ticker.history(period="2d")
                if not hist.empty:
                    usdtry_price = _clean_val(hist.iloc[-1].get("Close"))

            if usdtry_price is None:
                warnings.append("USD/TRY data could not be retrieved from Yahoo Finance.")
        except Exception as e:
            logger.error(f"Error fetching USDTRY: {e}")
            warnings.append(f"Error fetching USD/TRY: {str(e)}")

        # 3. EURTRY
        try:
            eur_ticker = tickers.tickers.get("EURTRY=X") or yf.Ticker("EURTRY=X")
            fast_info = getattr(eur_ticker, "fast_info", {})
            info = getattr(eur_ticker, "info", {})
            eurtry_price = _clean_val(fast_info.get("lastPrice") or fast_info.get("last_price") or info.get("regularMarketPrice"))
            if eurtry_price is None:
                hist = eur_ticker.history(period="2d")
                if not hist.empty:
                    eurtry_price = _clean_val(hist.iloc[-1].get("Close"))

            if eurtry_price is None:
                warnings.append("EUR/TRY data could not be retrieved from Yahoo Finance.")
        except Exception as e:
            logger.error(f"Error fetching EURTRY: {e}")
            warnings.append(f"Error fetching EUR/TRY: {str(e)}")

        # 4. BTC-USD
        try:
            btc_ticker = tickers.tickers.get("BTC-USD") or yf.Ticker("BTC-USD")
            fast_info = getattr(btc_ticker, "fast_info", {})
            info = getattr(btc_ticker, "info", {})
            btc_usd = _clean_val(fast_info.get("lastPrice") or fast_info.get("last_price") or info.get("regularMarketPrice"))
            if btc_usd is None:
                hist = btc_ticker.history(period="2d")
                if not hist.empty:
                    btc_usd = _clean_val(hist.iloc[-1].get("Close"))

            if btc_usd is None:
                warnings.append("Bitcoin (BTC-USD) data could not be retrieved from Yahoo Finance.")
        except Exception as e:
            logger.error(f"Error fetching BTC-USD: {e}")
            warnings.append(f"Error fetching Bitcoin (BTC-USD): {str(e)}")

        # 5. S&P 500 (^GSPC)
        try:
            sp_ticker = tickers.tickers.get("^GSPC") or yf.Ticker("^GSPC")
            fast_info = getattr(sp_ticker, "fast_info", {})
            info = getattr(sp_ticker, "info", {})
            sp_price = _clean_val(fast_info.get("lastPrice") or fast_info.get("last_price") or info.get("regularMarketPrice"))
            sp_prev_close = _clean_val(fast_info.get("previousClose") or fast_info.get("previous_close") or info.get("regularMarketPreviousClose") or info.get("previousClose"))

            if sp_price is None or sp_prev_close is None:
                hist = sp_ticker.history(period="5d")
                if not hist.empty:
                    if sp_price is None:
                        sp_price = _clean_val(hist.iloc[-1].get("Close"))
                    if sp_prev_close is None and len(hist) >= 2:
                        sp_prev_close = _clean_val(hist.iloc[-2].get("Close"))

            sp_change_pct = None
            if sp_price is not None and sp_prev_close is not None:
                sp_change_pct = round(((sp_price - sp_prev_close) / sp_prev_close) * 100, 2)

            sp500_res = {
                "price": sp_price,
                "previous_close": sp_prev_close,
                "change_percent": sp_change_pct
            }
            if sp_price is None:
                warnings.append("S&P 500 price data could not be retrieved from Yahoo Finance.")
        except Exception as e:
            logger.error(f"Error fetching S&P 500: {e}")
            warnings.append(f"Error fetching S&P 500: {str(e)}")

        # 6. Nasdaq (^IXIC)
        try:
            nas_ticker = tickers.tickers.get("^IXIC") or yf.Ticker("^IXIC")
            fast_info = getattr(nas_ticker, "fast_info", {})
            info = getattr(nas_ticker, "info", {})
            nas_price = _clean_val(fast_info.get("lastPrice") or fast_info.get("last_price") or info.get("regularMarketPrice"))
            nas_prev_close = _clean_val(fast_info.get("previousClose") or fast_info.get("previous_close") or info.get("regularMarketPreviousClose") or info.get("previousClose"))

            if nas_price is None or nas_prev_close is None:
                hist = nas_ticker.history(period="5d")
                if not hist.empty:
                    if nas_price is None:
                        nas_price = _clean_val(hist.iloc[-1].get("Close"))
                    if nas_prev_close is None and len(hist) >= 2:
                        nas_prev_close = _clean_val(hist.iloc[-2].get("Close"))

            nas_change_pct = None
            if nas_price is not None and nas_prev_close is not None:
                nas_change_pct = round(((nas_price - nas_prev_close) / nas_prev_close) * 100, 2)

            nasdaq_res = {
                "price": nas_price,
                "previous_close": nas_prev_close,
                "change_percent": nas_change_pct
            }
            if nas_price is None:
                warnings.append("Nasdaq price data could not be retrieved from Yahoo Finance.")
        except Exception as e:
            logger.error(f"Error fetching Nasdaq: {e}")
            warnings.append(f"Error fetching Nasdaq: {str(e)}")

    except Exception as e:
        logger.error(f"Yahoo Finance fetch error: {e}")
        warnings.append("Failed to fetch market data from Yahoo Finance.")

    us_status = get_us_market_status()

    return {
        "bist100": bist_res,
        "usdtry": {"price": usdtry_price},
        "eurtry": {"price": eurtry_price},
        "bitcoin": {"usd": btc_usd},
        "us_market": {
            "sp500": sp500_res,
            "nasdaq": nasdaq_res,
            "status": us_status
        },
        "warnings": warnings
    }

class YahooProvider:
    async def fetch_market_data(self) -> Dict[str, Any]:
        """Runs the synchronous yfinance data fetch in a thread pool."""
        return await asyncio.to_thread(_fetch_sync_data)
