from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

from app.core.config import settings


class MarketSession(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    INTRADAY = "INTRADAY"
    CLOSED = "CLOSED"
    WEEKEND = "WEEKEND"

def get_market_session(dt: datetime | None = None) -> MarketSession:
    tz = ZoneInfo(settings.TIMEZONE)
    if dt is None:
        dt = datetime.now(tz)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)

    # Weekend check: Monday=0, ..., Saturday=5, Sunday=6
    if dt.weekday() in (5, 6):
        return MarketSession.WEEKEND

    current_time = dt.time()
    market_open = time(10, 0)
    market_close = time(18, 10)

    if current_time < market_open:
        return MarketSession.PRE_MARKET
    elif market_open <= current_time <= market_close:
        return MarketSession.INTRADAY
    else:
        return MarketSession.CLOSED
