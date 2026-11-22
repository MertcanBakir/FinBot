from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.market_session import MarketSession, get_market_session

TZ = ZoneInfo("Europe/Istanbul")

def test_pre_market_session():
    dt = datetime(2026, 8, 3, 9, 30, tzinfo=TZ)  # Monday
    assert get_market_session(dt) == MarketSession.PRE_MARKET

def test_intraday_session():
    dt = datetime(2026, 8, 3, 10, 15, tzinfo=TZ)  # Monday
    assert get_market_session(dt) == MarketSession.INTRADAY

    dt_late = datetime(2026, 8, 7, 18, 5, tzinfo=TZ)  # Friday
    assert get_market_session(dt_late) == MarketSession.INTRADAY

def test_closed_session():
    dt = datetime(2026, 8, 3, 18, 30, tzinfo=TZ)  # Monday
    assert get_market_session(dt) == MarketSession.CLOSED

def test_weekend_session():
    dt_sat = datetime(2026, 8, 1, 14, 0, tzinfo=TZ)  # Saturday
    assert get_market_session(dt_sat) == MarketSession.WEEKEND

    dt_sun = datetime(2026, 8, 2, 21, 0, tzinfo=TZ)  # Sunday
    assert get_market_session(dt_sun) == MarketSession.WEEKEND
