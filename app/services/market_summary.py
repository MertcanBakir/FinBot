import asyncio
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.market_session import get_market_session
from app.models.market import (
    Bist100Data,
    BitcoinData,
    CurrencyData,
    GoldData,
    MarketData,
    MarketSnapshot,
    USIndexData,
    USMarketData,
)
from app.providers.gold_provider import GoldProvider
from app.providers.yahoo_provider import YahooProvider
from app.services.formatter import format_instant_summary

OUNCE_TO_GRAM_FACTOR = 31.1034768

class MarketSummaryService:
    def __init__(
        self,
        yahoo_provider: Optional[YahooProvider] = None,
        gold_provider: Optional[GoldProvider] = None
    ):
        self.yahoo_provider = yahoo_provider or YahooProvider()
        self.gold_provider = gold_provider or GoldProvider()

    async def generate(self, dt: Optional[datetime] = None) -> MarketSnapshot:
        tz = ZoneInfo(settings.TIMEZONE)
        if dt is None:
            now = datetime.now(tz)
        elif dt.tzinfo is None:
            now = dt.replace(tzinfo=tz)
        else:
            now = dt.astimezone(tz)

        # 1. Fetch market data concurrently
        yahoo_task = self.yahoo_provider.fetch_market_data()
        gold_task = self.gold_provider.fetch_xau_usd()

        yahoo_res, (xauusd_price, gold_warning) = await asyncio.gather(yahoo_task, gold_task)

        warnings: List[str] = list(yahoo_res.get("warnings", []))
        if gold_warning:
            warnings.append(gold_warning)

        # 2. Build BIST data
        bist_raw = yahoo_res.get("bist100", {})
        bist_data = Bist100Data(
            symbol=bist_raw.get("symbol", "XU100.IS"),
            price=bist_raw.get("price"),
            previous_close=bist_raw.get("previous_close"),
            open=bist_raw.get("open"),
            high=bist_raw.get("high"),
            low=bist_raw.get("low"),
            change_percent=bist_raw.get("change_percent")
        )

        # 3. Build Currencies
        usdtry_price = yahoo_res.get("usdtry", {}).get("price")
        eurtry_price = yahoo_res.get("eurtry", {}).get("price")

        usd_data = CurrencyData(price=usdtry_price)
        eur_data = CurrencyData(price=eurtry_price)

        # 4. Calculate Gram Gold: xau_usd * usd_try / 31.1034768
        gram_try: Optional[float] = None
        if xauusd_price is not None and usdtry_price is not None:
            calc_gram = (xauusd_price * usdtry_price) / OUNCE_TO_GRAM_FACTOR
            gram_try = round(calc_gram, 2)

        gold_data = GoldData(
            xauusd=xauusd_price,
            gram_try=gram_try,
            gram_try_type="calculated_spot"
        )

        # 5. Calculate Bitcoin / TRY: bitcoin_usd * usd_try
        btc_usd = yahoo_res.get("bitcoin", {}).get("usd")
        btc_try: Optional[float] = None
        if btc_usd is not None and usdtry_price is not None:
            btc_try = round(btc_usd * usdtry_price, 2)

        bitcoin_data = BitcoinData(
            usd=btc_usd,
            try_price=btc_try
        )

        # 6. Build US Market Data
        us_raw = yahoo_res.get("us_market", {})
        sp_data = USIndexData(**us_raw.get("sp500", {})) if us_raw.get("sp500") else None
        nas_data = USIndexData(**us_raw.get("nasdaq", {})) if us_raw.get("nasdaq") else None
        us_market_data = USMarketData(
            sp500=sp_data,
            nasdaq=nas_data,
            status=us_raw.get("status", "CLOSED")
        )

        market_data = MarketData(
            bist100=bist_data,
            usdtry=usd_data,
            eurtry=eur_data,
            gold=gold_data,
            bitcoin=bitcoin_data,
            us_market=us_market_data
        )

        # 7. Determine Session
        session = get_market_session(now)

        # 8. Format time & Turkish summary text
        time_str = now.strftime("%H:%M")
        turkish_text = format_instant_summary(market_data, session, time_str)

        # 9. Construct Snapshot
        snapshot = MarketSnapshot(
            success=True,
            generated_at=now.isoformat(),
            timezone=settings.TIMEZONE,
            session=session,
            market=market_data,
            text=turkish_text,
            warnings=warnings
        )

        return snapshot

market_summary_service = MarketSummaryService()
