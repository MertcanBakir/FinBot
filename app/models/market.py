from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.market_session import MarketSession


class Bist100Data(BaseModel):
    symbol: str = "XU100.IS"
    price: Optional[float] = None
    previous_close: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    change_percent: Optional[float] = None

class CurrencyData(BaseModel):
    price: Optional[float] = None

class GoldData(BaseModel):
    xauusd: Optional[float] = None
    gram_try: Optional[float] = None
    gram_try_type: str = "calculated_spot"

class BitcoinData(BaseModel):
    usd: Optional[float] = None
    try_price: Optional[float] = Field(default=None, alias="try", serialization_alias="try")

    model_config = {
        "populate_by_name": True
    }

class USIndexData(BaseModel):
    price: Optional[float] = None
    previous_close: Optional[float] = None
    change_percent: Optional[float] = None

class USMarketData(BaseModel):
    sp500: Optional[USIndexData] = None
    nasdaq: Optional[USIndexData] = None
    status: str = "CLOSED"  # OPEN, CLOSED, HOLIDAY

class MarketData(BaseModel):
    bist100: Bist100Data
    usdtry: CurrencyData
    eurtry: CurrencyData
    gold: GoldData
    bitcoin: BitcoinData
    us_market: Optional[USMarketData] = None

class MarketSnapshot(BaseModel):
    success: bool = True
    generated_at: str
    timezone: str = "Europe/Istanbul"
    session: MarketSession
    market: MarketData
    text: str
    warnings: List[str] = Field(default_factory=list)

# Movers Models
class MarketMover(BaseModel):
    symbol: str
    price: Optional[float] = None
    change_percent: Optional[float] = None

class BistMovers(BaseModel):
    gainers: List[MarketMover] = Field(default_factory=list)
    losers: List[MarketMover] = Field(default_factory=list)

class EconomicEvent(BaseModel):
    name: str
    country: str
    scheduled_at: str
    scheduled_at_istanbul: str
    source: str
    days_remaining: Optional[int] = None

    provider_id: Optional[str] = None
    event_code: Optional[str] = None
    reference_period: Optional[str] = None
    source_event_id: Optional[str] = None
    source_url: Optional[str] = None

    carried_forward: bool = False
    stale: bool = False
