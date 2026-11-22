from datetime import date, datetime
from urllib.parse import unquote
from zoneinfo import ZoneInfo

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
    MarketSnapshot,
    USIndexData,
    USMarketData,
)
from app.services.market_email_report import (
    build_closing_email_content,
    build_closing_social_text,
    build_morning_email_content,
    build_morning_social_text,
    build_x_share_url,
    deduplicate_warnings,
    format_signed_percent,
    format_turkish_date,
    format_turkish_number,
    get_countdown_label,
    x_weighted_length,
)


def test_turkish_formatters_and_helpers():
    # Date formatting
    d = date(2026, 8, 18)
    assert format_turkish_date(d) == "18 Ağustos 2026"
    assert format_turkish_date("2026-08-18") == "18 Ağustos 2026"
    assert format_turkish_date("2026-08-18T10:00:00+03:00") == "18 Ağustos 2026"
    assert format_turkish_date(None) == ""

    # Number formatting
    assert format_turkish_number(13721.29, decimals=2) == "13.721,29"
    assert format_turkish_number(47.7578, decimals=4) == "47,7578"
    assert format_turkish_number(4400.83, decimals=2) == "4.400,83"
    assert format_turkish_number(63741.0, decimals=0) == "63.741"
    assert format_turkish_number(None) == ""

    # Percent formatting
    assert format_signed_percent(0.12, mode="badge") == "▲ %0,12"
    assert format_signed_percent(-0.49, mode="badge") == "▼ %0,49"
    assert format_signed_percent(0.0, mode="badge") == "%0,00"
    assert format_signed_percent(None, mode="badge") == ""

    assert format_signed_percent(0.12, mode="social") == "(+%0,12)"
    assert format_signed_percent(-0.49, mode="social") == "(-%0,49)"
    assert format_signed_percent(0.0, mode="social") == "(%0,00)"

    assert format_signed_percent(4.82, mode="mover") == "+%4,82"
    assert format_signed_percent(-2.84, mode="mover") == "-%2,84"

    # Countdown label
    tz = ZoneInfo("Europe/Istanbul")
    ref = datetime(2026, 8, 18, 8, 0, tzinfo=tz)
    assert get_countdown_label(datetime(2026, 8, 18, 10, 0, tzinfo=tz), ref) == "Bugün 10:00"
    assert get_countdown_label(datetime(2026, 8, 19, 15, 30, tzinfo=tz), ref) == "Yarın 15:30"
    assert get_countdown_label(datetime(2026, 8, 20, 14, 0, tzinfo=tz), ref) == "2 gün kaldı • 14:00"


def test_x_weighted_length_calculation():
    # Regular ASCII & Turkish characters: weight 1 each
    assert x_weighted_length("BIST100") == 7
    assert x_weighted_length("Türkiye TÜFE — 18 Ağustos 2026") == 30
    assert x_weighted_length("–") == 1
    assert x_weighted_length("—") == 1

    # Emojis, bullet, and Bitcoin symbol: weight 2 each
    assert x_weighted_length("•") == 2
    assert x_weighted_length("📌") == 2
    assert x_weighted_length("📈") == 2
    assert x_weighted_length("🥇") == 2
    assert x_weighted_length("💵") == 2
    assert x_weighted_length("₿") == 2
    assert x_weighted_length("🗓") == 2
    assert x_weighted_length("🚀") == 2
    assert x_weighted_length("📉") == 2
    assert x_weighted_length("↕") == 2

    # Mixed text check
    # "📈 BIST100" -> emoji(2) + space(1) + BIST100(7) = 10 (Python len is 9)
    sample = "📈 BIST100"
    assert len(sample) == 9
    assert x_weighted_length(sample) == 10


def test_warning_deduplication():
    raw_warnings = [
        "Rolling cache missing",
        "  Rolling cache missing  ",
        "API warning 1",
        "Rolling cache missing",
        "API warning 2",
        "",
        "API warning 1",
    ]
    unique = deduplicate_warnings(raw_warnings)
    assert unique == ["Rolling cache missing", "API warning 1", "API warning 2"]


def test_morning_email_golden_content():
    tz = ZoneInfo("Europe/Istanbul")
    as_of = datetime(2026, 8, 18, 8, 0, tzinfo=tz)

    market = MarketData(
        bist100=Bist100Data(price=13721.29, change_percent=0.12),
        usdtry=CurrencyData(price=47.7578),
        eurtry=CurrencyData(price=54.9600),
        gold=GoldData(xauusd=4400.83, gram_try=6189.50),
        bitcoin=BitcoinData(usd=63741.0),
        us_market=USMarketData(
            sp500=USIndexData(price=6412.50, change_percent=-0.27),
            nasdaq=USIndexData(price=21108.20, change_percent=0.41),
            status="CLOSED",
        ),
    )

    snapshot = MarketSnapshot(
        success=True,
        generated_at=as_of.isoformat(),
        timezone="Europe/Istanbul",
        session=MarketSession.PRE_MARKET,
        market=market,
        text="Sample text",
        warnings=["Cache is up to date", "Duplicate warning test"],
    )

    events = [
        EconomicEvent(
            name="Türkiye TÜFE",
            country="TR",
            scheduled_at="",
            scheduled_at_istanbul="2026-08-18T10:00:00+03:00",
            source="TÜİK",
            event_code="TR_CPI",
        ),
        EconomicEvent(
            name="ABD FOMC Faiz Kararı",
            country="US",
            scheduled_at="",
            scheduled_at_istanbul="2026-08-19T21:00:00+03:00",
            source="FED",
            event_code="US_FOMC",
        ),
    ]

    extra_warnings = ["Duplicate warning test", "Special market note"]

    content = build_morning_email_content(
        snapshot=snapshot,
        events=events,
        as_of=as_of,
        warnings=extra_warnings,
    )

    # 1. Subject
    assert content.subject == "☀️ FinBot Güne Başlarken — 18 Ağustos 2026"

    # 2. HTML Body Checks
    html_body = content.body_html
    assert "☀️ Güne Başlarken" in html_body
    assert "18 Ağustos 2026 • <strong>08:00</strong>" in html_body
    assert "13.721,29" in html_body
    assert "▲ %0,12" in html_body
    assert "47,7578" in html_body
    assert "54,9600" in html_body
    assert "$4.400,83" in html_body
    assert "6.189,50 TL" in html_body
    assert "$63.741" in html_body
    assert "S&amp;P 500" in html_body or "S&P 500" in html_body
    assert "6.412,50" in html_body
    assert "▼ %0,27" in html_body
    assert "Nasdaq" in html_body
    assert "21.108,20" in html_body
    assert "▲ %0,41" in html_body
    assert "Türkiye TÜFE" in html_body
    assert "ABD FOMC Faiz Kararı" in html_body
    assert "X’te Paylaş ↗" in html_body
    assert "📋 Metni seçip kopyalayın" in html_body
    assert "Kopyalamaya hazır" not in html_body

    # No raw dumps or JSON
    assert "HAM PİYASA VERİLERİ" not in html_body
    assert "model_dump" not in html_body
    assert '"bist100":' not in html_body

    # Security in HTML
    assert "<script" not in html_body
    assert "onclick=" not in html_body
    assert "navigator.clipboard" not in html_body
    assert "javascript:" not in html_body

    # Warnings in HTML (deduplicated)
    assert html_body.count("Duplicate warning test") == 1
    assert "Special market note" in html_body

    # 3. Plain Text Checks
    text_body = content.body_text
    assert "FinBot Güne Başlarken — 18 Ağustos 2026 • 08:00" in text_body
    assert "13.721,29 ▲ %0,12" in text_body
    assert "47,7578" in text_body
    assert "Türkiye TÜFE" in text_body
    assert "Duplicate warning test" in text_body
    assert text_body.count("Duplicate warning test") == 1
    assert "HAM PİYASA VERİLERİ" not in text_body

    # 4. Social Media Text Checks
    social = content.social_text
    assert "📌 Güne Başlarken | 18 Ağustos 2026" in social
    assert "📈 BIST100: 13.721,29 (+%0,12)" in social
    assert "🥇 Ons Altın: 4.400,83" in social
    assert "💵 USD/TRY: 47,7578" in social
    assert "₿ Bitcoin: 63.741" in social
    assert "🗓 Bugün: Türkiye TÜFE • 10:00" in social
    assert "#BIST100" in social
    assert "#XAU" in social
    assert "#Altın" in social
    assert "#Dolar" in social
    assert "#Bitcoin" in social
    assert "#TÜFE" in social
    assert x_weighted_length(social) <= 280

    # 5. X Share URL Verification
    assert content.share_url.startswith("https://twitter.com/intent/tweet?text=")
    encoded_part = content.share_url.split("https://twitter.com/intent/tweet?text=")[1]
    assert unquote(encoded_part) == social


def test_closing_email_golden_content():
    tz = ZoneInfo("Europe/Istanbul")
    as_of = datetime(2026, 8, 18, 18, 30, tzinfo=tz)

    market = MarketData(
        bist100=Bist100Data(price=14220.42, change_percent=1.02, low=13980.20, high=14245.10),
        usdtry=CurrencyData(price=47.7578),
        eurtry=CurrencyData(price=54.9600),
        gold=GoldData(xauusd=4400.83, gram_try=6189.50),
        bitcoin=BitcoinData(usd=63741.0),
    )

    snapshot = MarketSnapshot(
        success=True,
        generated_at=as_of.isoformat(),
        timezone="Europe/Istanbul",
        session=MarketSession.CLOSED,
        market=market,
        text="Closing snapshot text",
        warnings=[],
    )

    movers = BistMovers(
        gainers=[
            MarketMover(symbol="ASELS", price=100.50, change_percent=4.82),
            MarketMover(symbol="THYAO", price=250.00, change_percent=3.20),
            MarketMover(symbol="TUPRS", price=160.00, change_percent=2.10),
            MarketMover(symbol="GARAN", price=110.00, change_percent=1.50),  # 4th gainer (should be excluded from top 3)
        ],
        losers=[
            MarketMover(symbol="SAHOL", price=80.20, change_percent=-2.84),
            MarketMover(symbol="BIMAS", price=320.00, change_percent=-1.90),
            MarketMover(symbol="SISE", price=45.00, change_percent=-1.20),
            MarketMover(symbol="EREGL", price=48.00, change_percent=-0.90),  # 4th loser
        ],
    )

    content = build_closing_email_content(
        snapshot=snapshot,
        movers=movers,
        as_of=as_of,
        warnings=[],
    )

    # 1. Subject
    assert content.subject == "📊 FinBot Günün Kapanışı — 18 Ağustos 2026"

    # 2. HTML Body Checks
    html_body = content.body_html
    assert "📊 Günün Kapanışı" in html_body
    assert "14.220,42" in html_body
    assert "▲ %1,02" in html_body
    assert "13.980,20 — 14.245,10" in html_body
    assert "ASELS" in html_body
    assert "THYAO" in html_body
    assert "TUPRS" in html_body
    assert "GARAN" not in html_body  # only top 3
    assert "SAHOL" in html_body
    assert "BIMAS" in html_body
    assert "SISE" in html_body
    assert "EREGL" not in html_body
    assert "HAM PİYASA VERİLERİ" not in html_body
    assert "📋 Metni seçip kopyalayın" in html_body
    assert "Kopyalamaya hazır" not in html_body

    # 3. Social Media Text Checks
    social = content.social_text
    assert "📌 Günün Kapanışı | 18 Ağustos 2026" in social
    assert "📈 BIST100: 14.220,42 (+%1,02)" in social
    assert "↕ Gün içi: 13.980,20 – 14.245,10" in social
    assert "💵 USD/TRY: 47,7578" in social
    assert "🥇 Ons Altın: 4.400,83" in social
    assert "₿ Bitcoin: 63.741" in social
    assert "🚀 Yükselen: ASELS +%4,82" in social
    assert "📉 Düşen: SAHOL -%2,84" in social
    # 2nd and 3rd movers should NOT be in the tweet
    assert "THYAO" not in social
    assert "BIMAS" not in social
    assert "#BIST100" in social
    assert "#Borsa" in social
    assert "#Altın" in social
    assert "#Dolar" in social
    assert "#Bitcoin" in social
    assert x_weighted_length(social) <= 280

    # 4. X Share URL
    assert unquote(content.share_url.split("https://twitter.com/intent/tweet?text=")[1]) == social


def test_missing_values_graceful_handling():
    tz = ZoneInfo("Europe/Istanbul")
    as_of = datetime(2026, 8, 18, 8, 0, tzinfo=tz)

    # Empty/missing market data
    market = MarketData(
        bist100=Bist100Data(price=14000.0, change_percent=None, low=None, high=None),
        usdtry=CurrencyData(price=None),
        eurtry=CurrencyData(price=None),
        gold=GoldData(xauusd=None, gram_try=None),
        bitcoin=BitcoinData(usd=None),
        us_market=None,
    )

    snapshot = MarketSnapshot(
        success=True,
        generated_at=as_of.isoformat(),
        timezone="Europe/Istanbul",
        session=MarketSession.PRE_MARKET,
        market=market,
        text="Missing data text",
        warnings=[],
    )

    # Morning with no events
    morning_content = build_morning_email_content(
        snapshot=snapshot,
        events=[],
        as_of=as_of,
        warnings=[],
    )
    assert "☀️ FinBot Güne Başlarken" in morning_content.subject
    assert "Bu hafta için gösterilecek önemli ekonomik veri bulunmuyor." in morning_content.body_html
    assert "🇺🇸 ABD Önceki Kapanışı" not in morning_content.body_html
    assert "None" not in morning_content.social_text
    assert "Veri yok" not in morning_content.social_text
    assert "(+%" not in morning_content.social_text
    assert "#Bitcoin" not in morning_content.social_text
    assert "#Dolar" not in morning_content.social_text
    assert x_weighted_length(morning_content.social_text) <= 280

    # Closing with no movers
    closing_content = build_closing_email_content(
        snapshot=snapshot,
        movers=BistMovers(gainers=[], losers=[]),
        as_of=as_of,
        warnings=[],
    )
    assert "📊 FinBot Günün Kapanışı" in closing_content.subject
    assert "Gün İçi Aralık" not in closing_content.body_html
    assert "None" not in closing_content.social_text
    assert "Veri yok" not in closing_content.social_text
    assert x_weighted_length(closing_content.social_text) <= 280


def test_html_escaping_and_security():
    tz = ZoneInfo("Europe/Istanbul")
    as_of = datetime(2026, 8, 18, 8, 0, tzinfo=tz)

    market = MarketData(
        bist100=Bist100Data(price=14000.0, change_percent=0.5),
        usdtry=CurrencyData(price=47.0),
        eurtry=CurrencyData(price=54.0),
        gold=GoldData(xauusd=4000.0, gram_try=6000.0),
        bitcoin=BitcoinData(usd=60000.0),
    )

    snapshot = MarketSnapshot(
        success=True,
        generated_at=as_of.isoformat(),
        timezone="Europe/Istanbul",
        session=MarketSession.PRE_MARKET,
        market=market,
        text="text",
        warnings=['<script>alert("xss")</script>'],
    )

    events = [
        EconomicEvent(
            name='<img src=x onerror=alert("hack")> Türkiye TÜFE',
            country="TR",
            scheduled_at="",
            scheduled_at_istanbul="2026-08-18T10:00:00+03:00",
            source="TÜİK",
        )
    ]

    content = build_morning_email_content(snapshot, events, as_of)

    assert "<script>" not in content.body_html
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in content.body_html
    assert "<img" not in content.body_html
    assert "&lt;img" in content.body_html


def test_social_text_priority_drop_when_too_long():
    tz = ZoneInfo("Europe/Istanbul")
    as_of = datetime(2026, 8, 18, 8, 0, tzinfo=tz)

    market = MarketData(
        bist100=Bist100Data(price=14000.0, change_percent=0.5),
        usdtry=CurrencyData(price=47.0),
        eurtry=CurrencyData(price=54.0),
        gold=GoldData(xauusd=4000.0, gram_try=6000.0),
        bitcoin=BitcoinData(usd=60000.0),
    )

    # Very long event name (180 chars)
    long_name = "Çok Uzun Ekonomik Gösterge Başlığı " * 5
    events = [
        EconomicEvent(
            name=long_name,
            country="TR",
            scheduled_at="",
            scheduled_at_istanbul="2026-08-18T10:00:00+03:00",
            source="TÜİK",
        )
    ]

    social = build_morning_social_text(market, events, as_of)
    assert x_weighted_length(social) <= 280
    assert "📌 Güne Başlarken" in social
    assert "📈 BIST100" in social


def test_x_weighted_length_regression_with_emojis():
    """Demonstrate a case where Python len(text) <= 280 but X weighted length > 280.

    Reconstruct an un-trimmed legacy candidate string with:
    - Golden closing market values
    - Gainer symbol: "A" * 37 (+%4,82)
    - Loser symbol: "B" (-%2,84)

    Assert:
    - len(legacy_candidate) == 273
    - x_weighted_length(legacy_candidate) == 281
    - build_closing_social_text() trims it so x_weighted_length(result) <= 280 and result != legacy_candidate.
    """
    tz = ZoneInfo("Europe/Istanbul")
    as_of = datetime(2026, 8, 18, 18, 30, tzinfo=tz)

    market = MarketData(
        bist100=Bist100Data(price=14220.42, change_percent=1.02, low=13980.20, high=14245.10),
        usdtry=CurrencyData(price=47.7578),
        eurtry=CurrencyData(price=54.9600),
        gold=GoldData(xauusd=4400.83, gram_try=6189.50),
        bitcoin=BitcoinData(usd=63741.0),
    )

    gainer_sym = "A" * 37
    loser_sym = "B"
    movers = BistMovers(
        gainers=[MarketMover(symbol=gainer_sym, price=100.50, change_percent=4.82)],
        losers=[MarketMover(symbol=loser_sym, price=80.20, change_percent=-2.84)],
    )

    # Reconstruct the un-trimmed legacy candidate text
    legacy_candidate = (
        "📌 Günün Kapanışı | 18 Ağustos 2026\n\n"
        "📈 BIST100: 14.220,42 (+%1,02)\n"
        "↕ Gün içi: 13.980,20 – 14.245,10\n"
        "💵 USD/TRY: 47,7578\n"
        "🥇 Ons Altın: 4.400,83\n"
        "₿ Bitcoin: 63.741\n\n"
        f"🚀 Yükselen: {gainer_sym} +%4,82\n"
        f"📉 Düşen: {loser_sym} -%2,84\n\n"
        "#BIST100 #Borsa #Altın #Dolar #Bitcoin"
    )

    assert len(legacy_candidate) == 273
    assert x_weighted_length(legacy_candidate) == 281

    # Now verify build_closing_social_text trims the text because x_weighted_length > 280
    result = build_closing_social_text(market, movers, as_of)

    assert x_weighted_length(result) <= 280
    assert result != legacy_candidate

    # Ensure share URL matches the final trimmed social text exactly
    share_url = build_x_share_url(result)
    assert unquote(share_url.split("https://twitter.com/intent/tweet?text=")[1]) == result


def test_email_client_compatibility_rules():
    """Verify email client & Outlook compatibility (no flex, no min-width, solid header fallback on table/td)."""
    from html.parser import HTMLParser

    class TagAttributeChecker(HTMLParser):
        def __init__(self):
            super().__init__()
            self.div_bgcolors = []
            self.table_bgcolors = []
            self.td_bgcolors = []

        def handle_starttag(self, tag, attrs):
            attr_dict = dict(attrs)
            if "bgcolor" in attr_dict:
                if tag == "div":
                    self.div_bgcolors.append(attr_dict["bgcolor"])
                elif tag == "table":
                    self.table_bgcolors.append(attr_dict["bgcolor"])
                elif tag == "td":
                    self.td_bgcolors.append(attr_dict["bgcolor"])

    tz = ZoneInfo("Europe/Istanbul")
    as_of = datetime(2026, 8, 18, 8, 0, tzinfo=tz)

    market = MarketData(
        bist100=Bist100Data(price=13721.29, change_percent=0.12),
        usdtry=CurrencyData(price=47.7578),
        eurtry=CurrencyData(price=54.9600),
        gold=GoldData(xauusd=4400.83, gram_try=6189.50),
        bitcoin=BitcoinData(usd=63741.0),
        us_market=USMarketData(
            sp500=USIndexData(price=6412.50, change_percent=-0.27),
            nasdaq=USIndexData(price=21108.20, change_percent=0.41),
            status="CLOSED",
        ),
    )
    snapshot = MarketSnapshot(
        success=True,
        generated_at=as_of.isoformat(),
        timezone="Europe/Istanbul",
        session=MarketSession.PRE_MARKET,
        market=market,
        text="text",
        warnings=[],
    )
    movers = BistMovers(
        gainers=[MarketMover(symbol="ASELS", price=100.0, change_percent=4.82)],
        losers=[MarketMover(symbol="SAHOL", price=80.0, change_percent=-2.84)],
    )

    morning_content = build_morning_email_content(snapshot, [], as_of)
    closing_content = build_closing_email_content(snapshot, movers, as_of)

    for html_body in [morning_content.body_html, closing_content.body_html]:
        assert "display: flex" not in html_body
        assert "display:flex" not in html_body
        assert "min-width: 260px" not in html_body
        assert "min-width:260px" not in html_body
        assert "justify-content" not in html_body
        assert "align-items" not in html_body
        # Header style must NOT use shorthand background: linear-gradient
        assert "background: linear-gradient" not in html_body
        # Header style must use background-image: linear-gradient
        assert "background-image: linear-gradient" in html_body

    # Check HTML tags using HTMLParser: no div bgcolor, valid table/td bgcolor
    morning_checker = TagAttributeChecker()
    morning_checker.feed(morning_content.body_html)
    assert len(morning_checker.div_bgcolors) == 0
    assert "#1E3A8A" in morning_checker.table_bgcolors
    assert "#1E3A8A" in morning_checker.td_bgcolors
    assert 'background-color: #1E3A8A;' in morning_content.body_html

    closing_checker = TagAttributeChecker()
    closing_checker.feed(closing_content.body_html)
    assert len(closing_checker.div_bgcolors) == 0
    assert "#0F172A" in closing_checker.table_bgcolors
    assert "#0F172A" in closing_checker.td_bgcolors
    assert 'background-color: #0F172A;' in closing_content.body_html
