from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.models.market import BistMovers, MarketMover
from app.services.bist_movers import (
    BistMoversService,
    _fetch_sync_movers,
    _load_symbols,
)
from app.services.formatter import fmt_change, format_closing_summary

# ----------------------------------------------------
# 1. SORTING AND FILTERING BASIC TEST
# ----------------------------------------------------

Q3_2026_BIST100_SYMBOLS = {
    f"{symbol}.IS"
    for symbol in """
        AEFES AKBNK AKSA AKSEN ALARK ALTNY ANSGR ARCLK ASELS ASTOR
        BALSU BERA BIMAS BRSAN BRYAT BSOKE BTCIM CANTE CCOLA CIMSA
        CVKMD CWENE DAPGM DOAS DOHOL DSTKF ECILC EFOR EKGYO ENERY
        ENJSA ENKAI EREGL ESEN EUPWR EUREN FENER FROTO GARAN GENIL
        GESAN GLRMK GRSEL GRTHO GSRAY GUBRF HALKB HEKTS IEYHO ISCTR
        ISMEN IZENR KCHOL KLRHO KRDMD KTLEV KUYAS MAGEN MAVI MGROS
        MIATK MPARK OBAMS ODAS ODINE OTKAR OYAKC PAHOL PASEU PATEK
        PETKM PGSUS PSGYO QUAGR RALYH REEDR SAHOL SARKY SASA SISE
        SKBNK SOKM TAVHL TCELL THYAO TKFEN TOASO TRALT TRENJ TRMET
        TSKB TTKOM TUKAS TUPRS TURSG ULKER VAKBN VESTL YKBNK ZOREN
    """.split()
}


def test_runtime_symbols_are_loaded_from_supabase_catalog():
    connection = MagicMock()
    with patch("app.services.bist_movers.get_db_connection") as get_connection, patch(
        "app.services.bist_movers.InstrumentCatalogRepository.get_active_symbols",
        return_value=sorted(Q3_2026_BIST100_SYMBOLS),
    ) as catalog_loader:
        get_connection.return_value.__enter__.return_value = connection
        symbols = _load_symbols()

    assert len(symbols) == 100
    assert len(set(symbols)) == 100
    assert set(symbols) == Q3_2026_BIST100_SYMBOLS
    catalog_loader.assert_called_once_with(
        connection,
        market="BIST100",
        asset_type="stock",
        yahoo_suffix=True,
    )


def test_bist_movers_sorting_and_filtering():
    stocks = [
        MarketMover(symbol="ASELS", price=100.0, change_percent=4.82),
        MarketMover(symbol="THYAO", price=200.0, change_percent=3.94),
        MarketMover(symbol="TUPRS", price=150.0, change_percent=3.26),
        MarketMover(symbol="AKBNK", price=50.0, change_percent=1.20),
        MarketMover(symbol="GARAN", price=60.0, change_percent=0.0),
        MarketMover(symbol="SISE",  price=40.0, change_percent=-1.91),
        MarketMover(symbol="BIMAS", price=300.0, change_percent=-2.23),
        MarketMover(symbol="SAHOL", price=80.0, change_percent=-2.84),
        MarketMover(symbol="EREGL", price=45.0, change_percent=-0.50),
    ]

    gainers = [m for m in stocks if m.change_percent is not None and m.change_percent > 0]
    gainers.sort(key=lambda x: x.change_percent, reverse=True)

    losers = [m for m in stocks if m.change_percent is not None and m.change_percent < 0]
    losers.sort(key=lambda x: x.change_percent)

    top_gainers = gainers[:3]
    assert len(top_gainers) == 3
    assert [g.symbol for g in top_gainers] == ["ASELS", "THYAO", "TUPRS"]

    top_losers = losers[:3]
    assert len(top_losers) == 3
    assert [loser.symbol for loser in top_losers] == ["SAHOL", "BIMAS", "SISE"]

@pytest.mark.asyncio
async def test_bist_movers_service_missing_ticker_handling():
    service = BistMoversService()
    with patch("app.services.bist_movers._fetch_sync_movers") as mock_fetch:
        mock_fetch.return_value = (
            movers_obj := type("Obj", (), {
                "gainers": [MarketMover(symbol="ASELS", price=100.0, change_percent=4.82)],
                "losers": [MarketMover(symbol="SISE", price=40.0, change_percent=-1.91)]
            })(),
            ["Some tickers failed to download"]
        )
        movers, warnings = await service.get_movers()
        assert len(movers.gainers) == 1
        assert movers.gainers[0].symbol == "ASELS"
        assert len(warnings) == 1

# ----------------------------------------------------
# 2. KTLEV REGRESSION FIXTURE & SANITY FILTERS
# ----------------------------------------------------

def test_ktlev_regression_and_suspicious_change_exclusion():
    symbols = ["KTLEV.IS", "ASELS.IS", "THYAO.IS", "TUPRS.IS", "SISE.IS", "SAHOL.IS", "BIMAS.IS"]
    dates = pd.date_range("2026-08-01", periods=2, freq="D")
    
    data_dict = {
        ("KTLEV.IS", "Close"): [100.0, 26.4],   # -73.60% drop
        ("ASELS.IS", "Close"): [100.0, 104.82], # +4.82%
        ("THYAO.IS", "Close"): [200.0, 207.88], # +3.94%
        ("TUPRS.IS", "Close"): [150.0, 154.89], # +3.26%
        ("SISE.IS",  "Close"): [40.0, 39.24],   # -1.91%
        ("BIMAS.IS", "Close"): [300.0, 293.31], # -2.23%
        ("SAHOL.IS", "Close"): [80.0, 77.73],   # -2.84%
    }
    df = pd.DataFrame(data_dict, index=dates)

    with patch("app.services.bist_movers._load_symbols", return_value=symbols), \
         patch("app.services.bist_movers.yf.download", return_value=df), \
         patch("app.services.bist_movers._check_corporate_action_per_symbol", return_value=True) as mock_ca_check:
        
        movers, warnings = _fetch_sync_movers()

        all_symbols = [m.symbol for m in movers.gainers] + [m.symbol for m in movers.losers]
        assert "KTLEV" not in all_symbols
        assert "KTLEV.IS" not in all_symbols

        mock_ca_check.assert_called()

        assert [g.symbol for g in movers.gainers] == ["ASELS", "THYAO", "TUPRS"]
        assert [loser.symbol for loser in movers.losers] == ["SAHOL", "BIMAS", "SISE"]

def test_sanity_threshold_and_boundary_cases():
    symbols = ["HIGHGAIN.IS", "HIGHLOSS.IS", "NORMALG.IS", "NORMALL.IS", "NANPRICE.IS", "ZEROPRICE.IS"]
    dates = pd.date_range("2026-08-01", periods=2, freq="D")

    data_dict = {
        ("HIGHGAIN.IS", "Close"): [100.0, 120.0],  # +20% -> Excluded (>15%)
        ("HIGHLOSS.IS", "Close"): [100.0, 80.0],   # -20% -> Excluded (>15%)
        ("NORMALG.IS",  "Close"): [100.0, 114.5],  # +14.5% -> Accepted
        ("NORMALL.IS",  "Close"): [100.0, 85.5],   # -14.5% -> Accepted
        ("NANPRICE.IS", "Close"): [float("nan"), 100.0], # NaN -> Excluded
        ("ZEROPRICE.IS", "Close"): [0.0, 100.0],   # 0.0 -> Excluded
    }
    df = pd.DataFrame(data_dict, index=dates)

    with patch("app.services.bist_movers._load_symbols", return_value=symbols), \
         patch("app.services.bist_movers.yf.download", return_value=df), \
         patch("app.services.bist_movers._check_corporate_action_per_symbol", return_value=False):

        movers, warnings = _fetch_sync_movers()

        gainers = [m.symbol for m in movers.gainers]
        losers = [m.symbol for m in movers.losers]

        assert "NORMALG" in gainers
        assert "HIGHGAIN" not in gainers
        assert "NORMALL" in losers
        assert "HIGHLOSS" not in losers
        assert "NANPRICE" not in gainers and "NANPRICE" not in losers
        assert "ZEROPRICE" not in gainers and "ZEROPRICE" not in losers

def test_top3_fill_when_suspicious_mover_is_excluded():
    symbols = ["BADLOSER.IS", "L1.IS", "L2.IS", "L3.IS", "L4.IS"]
    dates = pd.date_range("2026-08-01", periods=2, freq="D")

    data_dict = {
        ("BADLOSER.IS", "Close"): [100.0, 30.0], # -70% -> Excluded
        ("L1.IS",       "Close"): [100.0, 90.0], # -10%
        ("L2.IS",       "Close"): [100.0, 92.0], # -8%
        ("L3.IS",       "Close"): [100.0, 94.0], # -6%
        ("L4.IS",       "Close"): [100.0, 96.0], # -4%
    }
    df = pd.DataFrame(data_dict, index=dates)

    with patch("app.services.bist_movers._load_symbols", return_value=symbols), \
         patch("app.services.bist_movers.yf.download", return_value=df), \
         patch("app.services.bist_movers._check_corporate_action_per_symbol", return_value=True):

        movers, _ = _fetch_sync_movers()

        assert len(movers.losers) == 3
        assert [loser.symbol for loser in movers.losers] == ["L1", "L2", "L3"]

# ----------------------------------------------------
# 3. FORMATTER DOUBLE-NEGATIVE TEST
# ----------------------------------------------------

def test_formatter_no_double_negative():
    assert fmt_change(-2.84) == "▼ %2,84"
    assert fmt_change(4.82) == "▲ %4,82"

    market = MagicMock()
    market.bist100 = type("Bist", (), {"price": 10000.0, "change_percent": 1.0, "low": 9900.0, "high": 10100.0})()
    market.usdtry = type("Curr", (), {"price": 33.0})()
    market.eurtry = type("Curr", (), {"price": 36.0})()
    market.gold = type("Gold", (), {"xauusd": 2400.0, "gram_try": 2600.0})()
    market.bitcoin = type("Btc", (), {"usd": 60000.0})()

    movers = BistMovers(
        gainers=[MarketMover(symbol="ASELS", price=100.0, change_percent=4.82)],
        losers=[MarketMover(symbol="SAHOL", price=80.0, change_percent=-2.84)]
    )

    closing_str = format_closing_summary(market, movers)
    assert "▼ SAHOL %2,84" in closing_str
    assert "%-2,84" not in closing_str
