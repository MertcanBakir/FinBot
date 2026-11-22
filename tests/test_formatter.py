from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.market_session import MarketSession
from app.models.market import (
    Bist100Data,
    BistMovers,
    BitcoinData,
    CurrencyData,
    EconomicEvent,
    GoldData,
    MarketData,
    MarketMover,
    USIndexData,
    USMarketData,
)
from app.services.formatter import (
    fmt_change,
    fmt_num,
    format_closing_summary,
    format_instant_summary,
    format_morning_summary,
)


def test_number_formatting():
    assert fmt_num(14220.42) == "14.220,42"
    assert fmt_num(46.84) == "46,84"
    assert fmt_num(6098.14, decimals=0, suffix=" TL") == "6.098 TL"
    assert fmt_num(None) == "Veri yok"

def test_change_direction_formatting():
    assert fmt_change(1.02) == "▲ %1,02"
    assert fmt_change(-0.50) == "▼ %0,50"
    assert fmt_change(0.0) == "%0,00"
    assert fmt_change(None) == ""


@pytest.mark.parametrize(
    ("change_percent", "expected_direction"),
    [
        (1.34, "%1,34 yükselişle"),
        (-1.34, "%1,34 düşüşle"),
        (0.0, "%0,00 yatay"),
    ],
)
def test_instant_closed_summary_is_independent_from_current_session(
    change_percent,
    expected_direction,
):
    market = MarketData(
        bist100=Bist100Data(
            price=13458.10,
            change_percent=change_percent,
            low=13216.16,
            high=13458.10,
        ),
        usdtry=CurrencyData(price=47.52),
        eurtry=CurrencyData(price=54.84),
        gold=GoldData(xauusd=4051.0, gram_try=6189.0),
        bitcoin=BitcoinData(usd=62986.0),
    )

    text = format_instant_summary(market, MarketSession.CLOSED, "18:10")

    assert expected_direction in text
    assert "Gün içi: 13.216,16 — 13.458,10" in text
    assert "Bitcoin: $62.986" in text


def test_instant_closed_summary_handles_missing_optional_values():
    market = MarketData(
        bist100=Bist100Data(price=13458.10, change_percent=None),
        usdtry=CurrencyData(price=47.52),
        eurtry=CurrencyData(price=54.84),
        gold=GoldData(xauusd=4051.0, gram_try=6189.0),
        bitcoin=BitcoinData(),
    )

    text = format_instant_summary(market, MarketSession.CLOSED, "18:10")

    assert "Borsa İstanbul günü yükselişle tamamladı." in text
    assert "Gün içi:" not in text
    assert "Bitcoin:" not in text

def test_new_morning_summary_formatting():
    market = MarketData(
        bist100=Bist100Data(price=13458.0, change_percent=1.34),
        usdtry=CurrencyData(price=47.52),
        eurtry=CurrencyData(price=54.84),
        gold=GoldData(xauusd=4051.0, gram_try=6189.0),
        bitcoin=BitcoinData(usd=62986.0),
        us_market=USMarketData(
            sp500=USIndexData(price=6412.0, change_percent=-0.27),
            nasdaq=USIndexData(price=21108.0, change_percent=-0.41),
            status="CLOSED"
        )
    )

    events = [
        EconomicEvent(name="Türkiye TÜFE", country="TR", scheduled_at="", scheduled_at_istanbul="2026-08-03T10:00:00+03:00", source="TÜİK"),
        EconomicEvent(name="ABD Tarım Dışı İstihdam", country="US", scheduled_at="", scheduled_at_istanbul="2026-08-03T15:30:00+03:00", source="BLS")
    ]

    ref_dt = datetime(2026, 8, 3, 9, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    morning_text = format_morning_summary(market, events, ref_dt)

    assert "Güne Başlarken | Piyasalar" in morning_text
    assert "BIST 100: 13.458 ▲ %1,34" in morning_text
    assert "ABD önceki kapanış:" in morning_text
    assert "S&P 500: 6.412 ▼ %0,27" in morning_text
    assert "Bugün takip edilecekler:" in morning_text
    assert "• 10:00 — Türkiye TÜFE" in morning_text
    assert "• 15:30 — ABD Tarım Dışı İstihdam" in morning_text

def test_new_closing_summary_formatting():
    market = MarketData(
        bist100=Bist100Data(price=13458.10, change_percent=1.34, low=13216.16, high=13458.10),
        usdtry=CurrencyData(price=47.52),
        eurtry=CurrencyData(price=54.84),
        gold=GoldData(xauusd=4051.0, gram_try=6189.0),
        bitcoin=BitcoinData(usd=62986.0)
    )

    movers = BistMovers(
        gainers=[
            MarketMover(symbol="ASELS", price=100.0, change_percent=4.82),
            MarketMover(symbol="THYAO", price=200.0, change_percent=3.94),
            MarketMover(symbol="TUPRS", price=150.0, change_percent=3.26)
        ],
        losers=[
            MarketMover(symbol="SAHOL", price=80.0, change_percent=-2.84),
            MarketMover(symbol="BIMAS", price=300.0, change_percent=-2.23),
            MarketMover(symbol="SISE", price=40.0, change_percent=-1.91)
        ]
    )

    closing_text = format_closing_summary(market, movers)

    assert "Günün Kapanışı | Borsa İstanbul" in closing_text
    assert "Gün içi: 13.216,16 — 13.458,10" in closing_text
    assert "En çok yükselenler:" in closing_text
    assert "▲ ASELS %4,82" in closing_text
    assert "En çok düşenler:" in closing_text
    assert "▼ SAHOL %2,84" in closing_text
