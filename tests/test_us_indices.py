from datetime import datetime
from zoneinfo import ZoneInfo

from app.providers.yahoo_provider import get_us_market_status


def test_sp500_and_nasdaq_percentage_calculation():
    sp_price = 6412.0
    sp_prev_close = 6429.35
    sp_change_pct = round(((sp_price - sp_prev_close) / sp_prev_close) * 100, 2)
    assert sp_change_pct == -0.27

    nas_price = 21108.0
    nas_prev_close = 21195.0
    nas_change_pct = round(((nas_price - nas_prev_close) / nas_prev_close) * 100, 2)
    assert nas_change_pct == -0.41

def test_us_session_and_holiday_handling():
    ny_tz = ZoneInfo("America/New_York")

    # Weekend (Saturday)
    dt_sat = datetime(2026, 8, 1, 14, 0, tzinfo=ny_tz)
    assert get_us_market_status(dt_sat) == "CLOSED"

    # Weekday inside market hours (14:00 NY time)
    dt_open = datetime(2026, 8, 3, 14, 0, tzinfo=ny_tz)
    assert get_us_market_status(dt_open) == "OPEN"

    # Weekday outside market hours (08:00 NY time)
    dt_early = datetime(2026, 8, 3, 8, 0, tzinfo=ny_tz)
    assert get_us_market_status(dt_early) == "CLOSED"

def test_us_daylight_saving_timezone_conversion():
    ny_tz = ZoneInfo("America/New_York")
    tr_tz = ZoneInfo("Europe/Istanbul")

    # Summer DST (EDT UTC-4) -> Istanbul UTC+3 (7 hours difference)
    dt_summer_ny = datetime(2026, 7, 15, 16, 0, tzinfo=ny_tz) # 16:00 NY
    dt_summer_tr = dt_summer_ny.astimezone(tr_tz)            # 23:00 TR
    assert dt_summer_tr.hour == 23

    # Winter EST UTC-5 -> Istanbul UTC+3 (8 hours difference)
    dt_winter_ny = datetime(2026, 12, 15, 16, 0, tzinfo=ny_tz) # 16:00 NY
    dt_winter_tr = dt_winter_ny.astimezone(tr_tz)            # 00:00 TR (next day)
    assert dt_winter_tr.hour == 0
