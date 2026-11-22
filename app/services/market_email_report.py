import html
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Set, Tuple, Union
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.models.earnings import WeeklyEarningsEvent
from app.models.market import (
    BistMovers,
    EconomicEvent,
    MarketData,
    MarketSnapshot,
)

TURKISH_MONTHS = {
    1: "Ocak",
    2: "Şubat",
    3: "Mart",
    4: "Nisan",
    5: "Mayıs",
    6: "Haziran",
    7: "Temmuz",
    8: "Ağustos",
    9: "Eylül",
    10: "Ekim",
    11: "Kasım",
    12: "Aralık",
}

WEEKDAY_TR = {
    0: "Pazartesi",
    1: "Salı",
    2: "Çarşamba",
    3: "Perşembe",
    4: "Cuma",
    5: "Cumartesi",
    6: "Pazar",
}


@dataclass
class MarketEmailContent:
    subject: str
    body_text: str
    body_html: str
    social_text: str
    share_url: str


def x_weighted_length(text: str) -> int:
    """Calculate X / Twitter weighted character count.

    - U+0000 to U+10FF: weight 1 (Basic Latin, Latin Extended, Cyrillic, Turkish chars, etc.)
    - U+2000 to U+200D: weight 1 (General punctuation / spaces)
    - U+2010 to U+201F: weight 1 (General punctuation: dashes, quotes, etc.)
    - U+2032 to U+2037: weight 1 (Primes)
    - All other characters (Emojis, CJK, Bitcoin symbol, etc.): weight 2
    """
    total = 0
    for ch in text:
        cp = ord(ch)
        if (
            (0x0000 <= cp <= 0x10FF)
            or (0x2000 <= cp <= 0x200D)
            or (0x2010 <= cp <= 0x201F)
            or (0x2032 <= cp <= 0x2037)
        ):
            total += 1
        else:
            total += 2
    return total


def _normalize_str(s: Optional[str]) -> str:
    """Normalize text by stripping and collapsing internal whitespace."""
    if not s:
        return ""
    return " ".join(s.strip().split())


def format_turkish_date(dt_or_str: Optional[Union[date, datetime, str]]) -> str:
    """Format date to Turkish deterministic date: e.g. 18 Ağustos 2026."""
    if not dt_or_str:
        return ""
    if isinstance(dt_or_str, str):
        try:
            d = date.fromisoformat(dt_or_str.split("T")[0])
        except Exception:
            return dt_or_str
    elif isinstance(dt_or_str, datetime):
        d = dt_or_str.date()
    else:
        d = dt_or_str
    month_name = TURKISH_MONTHS.get(d.month, str(d.month))
    return f"{d.day} {month_name} {d.year}"


def format_turkish_number(val: Optional[float], decimals: int = 2) -> str:
    """Format float into Turkish number format (e.g. 13721.29 -> 13.721,29)."""
    if val is None:
        return ""
    formatted = f"{val:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return formatted


def format_signed_percent(pct: Optional[float], mode: str = "badge") -> str:
    """Format change percent.

    mode='badge': '▲ %1,02', '▼ %0,50', '%0,00'
    mode='social': '(+%1,02)', '(-%0,50)', '(%0,00)'
    mode='mover': '+%1,02', '-%0,50', '%0,00'
    """
    if pct is None:
        return ""
    abs_str = f"{abs(pct):.2f}".replace(".", ",")
    if pct > 0:
        if mode == "social":
            return f"(+%{abs_str})"
        elif mode == "mover":
            return f"+%{abs_str}"
        else:
            return f"▲ %{abs_str}"
    elif pct < 0:
        if mode == "social":
            return f"(-%{abs_str})"
        elif mode == "mover":
            return f"-%{abs_str}"
        else:
            return f"▼ %{abs_str}"
    else:
        if mode == "social":
            return f"(%{abs_str})"
        elif mode == "mover":
            return f"%{abs_str}"
        else:
            return f"%{abs_str}"


def deduplicate_warnings(warnings: Optional[List[str]]) -> List[str]:
    """Deduplicate warnings preserving exact first-seen order."""
    if not warnings:
        return []
    seen: Set[str] = set()
    unique: List[str] = []
    for w in warnings:
        if not w:
            continue
        cleaned = w.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique


def build_x_share_url(social_text: str) -> str:
    """Create secure X / Twitter web intent share URL."""
    return "https://twitter.com/intent/tweet?text=" + quote(social_text, safe="")


def get_countdown_label(event_dt: datetime, ref_dt: datetime) -> str:
    """Return friendly Turkish countdown label for upcoming event."""
    diff_days = (event_dt.date() - ref_dt.date()).days
    time_str = event_dt.strftime("%H:%M")
    if diff_days == 0:
        return f"Bugün {time_str}"
    elif diff_days == 1:
        return f"Yarın {time_str}"
    elif diff_days == 2:
        return f"2 gün kaldı • {time_str}"
    else:
        day_name = WEEKDAY_TR.get(event_dt.weekday(), "")
        return f"{day_name} {time_str} ({diff_days} gün kaldı)"


def _parse_event_datetime(ev: EconomicEvent, tz: ZoneInfo) -> Optional[datetime]:
    """Parse scheduled_at_istanbul datetime safely."""
    raw_str = ev.scheduled_at_istanbul or ev.scheduled_at
    if not raw_str:
        return None
    try:
        dt = datetime.fromisoformat(raw_str)
        return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
    except Exception:
        return None


def _filter_and_sort_upcoming_events(
    events: Optional[List[EconomicEvent]],
    as_of: datetime,
    tz: ZoneInfo,
) -> List[Tuple[EconomicEvent, datetime]]:
    """Filter events scheduled for the target week, sorted ascending."""
    if not events:
        return []
    result = []
    for ev in events:
        ev_dt = _parse_event_datetime(ev, tz)
        if ev_dt:
            if as_of.weekday() >= 4 or ev_dt.date() >= as_of.date():
                result.append((ev, ev_dt))
    result.sort(key=lambda item: item[1])
    return result


def build_morning_social_text(
    market: MarketData,
    events: Optional[List[EconomicEvent]],
    as_of: datetime,
) -> str:
    """Build deterministic Turkish social media text for Morning job (max 280 X-weighted chars)."""
    date_str = format_turkish_date(as_of)
    header = f"📌 Güne Başlarken | {date_str}"

    bist = market.bist100
    bist_line = None
    if bist and bist.price is not None:
        bist_num = format_turkish_number(bist.price, decimals=2)
        pct_str = format_signed_percent(bist.change_percent, mode="social")
        bist_line = f"📈 BIST100: {bist_num} {pct_str}".strip()

    ons_line = None
    if market.gold and market.gold.xauusd is not None:
        ons_num = format_turkish_number(market.gold.xauusd, decimals=2)
        ons_line = f"🥇 Ons Altın: {ons_num}"

    usd_line = None
    if market.usdtry and market.usdtry.price is not None:
        usd_num = format_turkish_number(market.usdtry.price, decimals=4)
        usd_line = f"💵 USD/TRY: {usd_num}"

    btc_line = None
    if getattr(market, "bitcoin", None) and market.bitcoin.usd is not None:
        btc_num = format_turkish_number(market.bitcoin.usd, decimals=0)
        btc_line = f"₿ Bitcoin: {btc_num}"

    # Upcoming event line
    tz = as_of.tzinfo or ZoneInfo("Europe/Istanbul")
    upcoming = _filter_and_sort_upcoming_events(events, as_of, tz)
    event_line = None
    event_ht = None
    if upcoming:
        first_ev, first_dt = upcoming[0]
        time_part = first_dt.strftime("%H:%M")
        diff_days = (first_dt.date() - as_of.date()).days
        ev_name = _normalize_str(first_ev.name)
        if diff_days == 0:
            event_line = f"🗓 Bugün: {ev_name} • {time_part}"
        elif diff_days == 1:
            event_line = f"🗓 Yarın: {ev_name} • {time_part}"
        else:
            day_name = WEEKDAY_TR.get(first_dt.weekday(), "")
            event_line = f"🗓 {day_name}: {ev_name} • {time_part}"

        # Hashtag mapping
        if first_ev.event_code == "TR_CPI":
            event_ht = "#TÜFE"
        elif first_ev.event_code == "US_FOMC":
            event_ht = "#FED"

    # Base hashtags
    hashtags = []
    if bist_line:
        hashtags.append("#BIST100")
    if ons_line:
        hashtags.append("#XAU")
    if (market.gold and market.gold.gram_try is not None) or ons_line:
        hashtags.append("#Altın")
    if usd_line:
        hashtags.append("#Dolar")
    if btc_line:
        hashtags.append("#Bitcoin")
    if event_ht:
        hashtags.append(event_ht)

    # Assembly with priority drop to guarantee <= 280 X-weighted chars
    def assemble(
        include_event: bool,
        ht_list: List[str],
        include_btc: bool,
        include_ons: bool,
    ) -> str:
        body_lines = []
        if bist_line:
            body_lines.append(bist_line)
        if include_ons and ons_line:
            body_lines.append(ons_line)
        if usd_line:
            body_lines.append(usd_line)
        if include_btc and btc_line:
            body_lines.append(btc_line)

        sections = [header]
        if body_lines:
            sections.append("\n".join(body_lines))
        if include_event and event_line:
            sections.append(event_line)
        if ht_list:
            sections.append(" ".join(ht_list))
        return "\n\n".join(sections)

    # 1. Try full
    text = assemble(include_event=True, ht_list=hashtags, include_btc=True, include_ons=True)
    if x_weighted_length(text) <= 280:
        return text

    # 2. Drop event line
    text = assemble(include_event=False, ht_list=hashtags, include_btc=True, include_ons=True)
    if x_weighted_length(text) <= 280:
        return text

    # 3. Drop hashtags from right to left
    cur_ht = list(hashtags)
    while cur_ht:
        cur_ht.pop()
        text = assemble(include_event=False, ht_list=cur_ht, include_btc=True, include_ons=True)
        if x_weighted_length(text) <= 280:
            return text

    # 4. Drop Bitcoin
    text = assemble(include_event=False, ht_list=[], include_btc=False, include_ons=True)
    if x_weighted_length(text) <= 280:
        return text

    # 5. Drop Ons Gold
    text = assemble(include_event=False, ht_list=[], include_btc=False, include_ons=False)
    assert x_weighted_length(text) <= 280
    return text


def build_closing_social_text(
    market: MarketData,
    movers: Optional[BistMovers],
    as_of: datetime,
) -> str:
    """Build deterministic Turkish social media text for Closing job (max 280 X-weighted chars)."""
    date_str = format_turkish_date(as_of)
    header = f"📌 Günün Kapanışı | {date_str}"

    bist = market.bist100
    bist_line = None
    intraday_line = None
    if bist and bist.price is not None:
        bist_num = format_turkish_number(bist.price, decimals=2)
        pct_str = format_signed_percent(bist.change_percent, mode="social")
        bist_line = f"📈 BIST100: {bist_num} {pct_str}".strip()
        if bist.low is not None and bist.high is not None:
            low_num = format_turkish_number(bist.low, decimals=2)
            high_num = format_turkish_number(bist.high, decimals=2)
            intraday_line = f"↕ Gün içi: {low_num} – {high_num}"

    usd_line = None
    if market.usdtry and market.usdtry.price is not None:
        usd_num = format_turkish_number(market.usdtry.price, decimals=4)
        usd_line = f"💵 USD/TRY: {usd_num}"

    ons_line = None
    if market.gold and market.gold.xauusd is not None:
        ons_num = format_turkish_number(market.gold.xauusd, decimals=2)
        ons_line = f"🥇 Ons Altın: {ons_num}"

    btc_line = None
    if getattr(market, "bitcoin", None) and market.bitcoin.usd is not None:
        btc_num = format_turkish_number(market.bitcoin.usd, decimals=0)
        btc_line = f"₿ Bitcoin: {btc_num}"

    # Movers lines (top 1 gainer & top 1 loser)
    gainer_line = None
    loser_line = None
    if movers:
        if movers.gainers:
            g = movers.gainers[0]
            g_sym = _normalize_str(g.symbol)
            pct = format_signed_percent(g.change_percent, mode="mover")
            gainer_line = f"🚀 Yükselen: {g_sym} {pct}".strip()
        if movers.losers:
            loser = movers.losers[0]
            l_sym = _normalize_str(loser.symbol)
            pct = format_signed_percent(loser.change_percent, mode="mover")
            loser_line = f"📉 Düşen: {l_sym} {pct}".strip()

    # Hashtags
    hashtags = []
    if bist_line:
        hashtags.extend(["#BIST100", "#Borsa"])
    if (market.gold and market.gold.gram_try is not None) or ons_line:
        hashtags.append("#Altın")
    if usd_line:
        hashtags.append("#Dolar")
    if btc_line:
        hashtags.append("#Bitcoin")

    def assemble(
        include_movers: bool,
        include_intraday: bool,
        ht_list: List[str],
        include_btc: bool,
        include_ons: bool,
    ) -> str:
        body_lines = []
        if bist_line:
            body_lines.append(bist_line)
        if include_intraday and intraday_line:
            body_lines.append(intraday_line)
        if usd_line:
            body_lines.append(usd_line)
        if include_ons and ons_line:
            body_lines.append(ons_line)
        if include_btc and btc_line:
            body_lines.append(btc_line)

        mover_lines = []
        if include_movers:
            if gainer_line:
                mover_lines.append(gainer_line)
            if loser_line:
                mover_lines.append(loser_line)

        sections = [header]
        if body_lines:
            sections.append("\n".join(body_lines))
        if mover_lines:
            sections.append("\n".join(mover_lines))
        if ht_list:
            sections.append(" ".join(ht_list))
        return "\n\n".join(sections)

    # 1. Try full
    text = assemble(include_movers=True, include_intraday=True, ht_list=hashtags, include_btc=True, include_ons=True)
    if x_weighted_length(text) <= 280:
        return text

    # 2. Drop movers
    text = assemble(include_movers=False, include_intraday=True, ht_list=hashtags, include_btc=True, include_ons=True)
    if x_weighted_length(text) <= 280:
        return text

    # 3. Drop intraday range
    text = assemble(include_movers=False, include_intraday=False, ht_list=hashtags, include_btc=True, include_ons=True)
    if x_weighted_length(text) <= 280:
        return text

    # 4. Drop hashtags right to left
    cur_ht = list(hashtags)
    while cur_ht:
        cur_ht.pop()
        text = assemble(include_movers=False, include_intraday=False, ht_list=cur_ht, include_btc=True, include_ons=True)
        if x_weighted_length(text) <= 280:
            return text

    # 5. Drop Bitcoin
    text = assemble(include_movers=False, include_intraday=False, ht_list=[], include_btc=False, include_ons=True)
    if x_weighted_length(text) <= 280:
        return text

    # 6. Drop Ons Gold
    text = assemble(include_movers=False, include_intraday=False, ht_list=[], include_btc=False, include_ons=False)
    assert x_weighted_length(text) <= 280
    return text


def _render_change_badge_html(pct: Optional[float]) -> str:
    """Render HTML change badge (positive green, negative red, zero gray, or empty if None)."""
    if pct is None:
        return ""
    if pct > 0:
        text = format_signed_percent(pct, mode="badge")
        return f'<span style="display: inline-block; padding: 2px 8px; font-size: 12px; font-weight: 700; border-radius: 9999px; color: #166534; background-color: #DCFCE7;">{html.escape(text)}</span>'
    elif pct < 0:
        text = format_signed_percent(pct, mode="badge")
        return f'<span style="display: inline-block; padding: 2px 8px; font-size: 12px; font-weight: 700; border-radius: 9999px; color: #991B1B; background-color: #FEE2E2;">{html.escape(text)}</span>'
    else:
        text = format_signed_percent(pct, mode="badge")
        return f'<span style="display: inline-block; padding: 2px 8px; font-size: 12px; font-weight: 700; border-radius: 9999px; color: #4B5563; background-color: #F3F4F6;">{html.escape(text)}</span>'


def _render_warnings_html(warnings: List[str]) -> str:
    """Render warnings card in HTML if warnings exist."""
    if not warnings:
        return ""
    items = "".join([
        f'<li style="margin-bottom: 4px; font-size: 12px; color: #991B1B; line-height: 1.4;">{html.escape(w)}</li>'
        for w in warnings
    ])
    return f"""
    <div style="margin-top: 20px; background-color: #FEF2F2; border: 1px solid #FECACA; border-radius: 8px; padding: 14px;">
      <div style="font-size: 13px; font-weight: 700; color: #991B1B; margin-bottom: 8px;">⚠️ Uyarılar ve Notlar</div>
      <ul style="margin: 0; padding-left: 18px;">
        {items}
      </ul>
    </div>
    """


def _render_share_box_html(social_text: str, share_url: str) -> str:
    """Render secure social media sharing card in HTML."""
    return f"""
    <div style="margin-top: 24px; padding: 16px; background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px;">
      <table role="presentation" style="width: 100%; border-collapse: collapse; margin-bottom: 10px;">
        <tr>
          <td style="font-size: 13px; font-weight: 700; color: #1E293B; vertical-align: middle;">
            📱 Sosyal Medya Paylaşım Metni
          </td>
          <td style="text-align: right; vertical-align: middle;">
            <span style="font-size: 11px; color: #64748B; margin-right: 8px;">📋 Metni seçip kopyalayın</span>
            <a href="{html.escape(share_url)}" target="_blank" rel="noopener noreferrer" style="display: inline-block; padding: 5px 12px; font-size: 11px; font-weight: 700; color: #FFFFFF; background-color: #0F172A; border-radius: 4px; text-decoration: none;">X’te Paylaş ↗</a>
          </td>
        </tr>
      </table>
      <pre style="margin: 0; padding: 12px; background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 6px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 12px; line-height: 1.5; color: #1E293B; white-space: pre-wrap; word-break: break-word; user-select: all;">{html.escape(social_text)}</pre>
    </div>
    """


def build_morning_email_content(
    snapshot: MarketSnapshot,
    events: Optional[List[EconomicEvent]],
    as_of: datetime,
    warnings: Optional[List[str]] = None,
    earnings_events: Optional[List[WeeklyEarningsEvent]] = None,
) -> MarketEmailContent:
    """Build complete, pure MarketEmailContent for Morning job."""
    date_str = format_turkish_date(as_of)
    time_str = as_of.strftime("%H:%M")
    subject = f"☀️ FinBot Güne Başlarken — {date_str}"

    # Merge and deduplicate warnings
    combined_warnings = list(snapshot.warnings or [])
    if warnings:
        combined_warnings.extend(warnings)
    unique_warnings = deduplicate_warnings(combined_warnings)

    market = snapshot.market
    bist = market.bist100

    # Social text and share URL
    social_text = build_morning_social_text(market, events, as_of)
    share_url = build_x_share_url(social_text)

    # 1. BIST 100 values
    bist_price_str = format_turkish_number(bist.price, decimals=2) if (bist and bist.price is not None) else "Veri alınamadı"
    bist_badge_html = _render_change_badge_html(bist.change_percent) if bist else ""
    bist_change_text = format_signed_percent(bist.change_percent, mode="badge") if bist else ""

    # 2. Currencies, Gold, Crypto
    usd_val = format_turkish_number(market.usdtry.price, decimals=4) if (market.usdtry and market.usdtry.price is not None) else "—"
    eur_val = format_turkish_number(market.eurtry.price, decimals=4) if (market.eurtry and market.eurtry.price is not None) else "—"
    xau_val = f"${format_turkish_number(market.gold.xauusd, decimals=2)}" if (market.gold and market.gold.xauusd is not None) else "—"
    gram_val = f"{format_turkish_number(market.gold.gram_try, decimals=2)} TL" if (market.gold and market.gold.gram_try is not None) else "—"

    btc = getattr(market, "bitcoin", None)
    btc_val = f"${format_turkish_number(btc.usd, decimals=0)}" if (btc and btc.usd is not None) else "—"

    # 3. US Market Section
    us = getattr(market, "us_market", None)
    has_us = bool(us and ((us.sp500 and us.sp500.price is not None) or (us.nasdaq and us.nasdaq.price is not None)))
    us_section_html = ""
    us_text_lines = []
    if has_us:
        us_cards_html = []
        us_text_lines.append("ABD ÖNCEKİ KAPANIŞ:")
        if us.sp500 and us.sp500.price is not None:
            sp_price = format_turkish_number(us.sp500.price, decimals=2)
            sp_badge = _render_change_badge_html(us.sp500.change_percent)
            sp_pct_text = format_signed_percent(us.sp500.change_percent, mode="badge")
            us_text_lines.append(f"  • S&P 500  : {sp_price} {sp_pct_text}".strip())
            us_cards_html.append(f"""
            <td style="width: 50%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <table role="presentation" style="width: 100%; border-collapse: collapse; margin-bottom: 4px;">
                <tr>
                  <td style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">S&P 500</td>
                  <td style="text-align: right;">{sp_badge}</td>
                </tr>
              </table>
              <div style="font-size: 16px; font-weight: 700; color: #111827;">{sp_price}</div>
            </td>
            """)
        if us.nasdaq and us.nasdaq.price is not None:
            nas_price = format_turkish_number(us.nasdaq.price, decimals=2)
            nas_badge = _render_change_badge_html(us.nasdaq.change_percent)
            nas_pct_text = format_signed_percent(us.nasdaq.change_percent, mode="badge")
            us_text_lines.append(f"  • Nasdaq   : {nas_price} {nas_pct_text}".strip())
            us_cards_html.append(f"""
            <td style="width: 50%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <table role="presentation" style="width: 100%; border-collapse: collapse; margin-bottom: 4px;">
                <tr>
                  <td style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Nasdaq</td>
                  <td style="text-align: right;">{nas_badge}</td>
                </tr>
              </table>
              <div style="font-size: 16px; font-weight: 700; color: #111827;">{nas_price}</div>
            </td>
            """)
        us_section_html = f"""
        <div style="margin-bottom: 20px;">
          <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 10px;">🇺🇸 ABD Önceki Kapanışı</div>
          <table role="presentation" style="width: 100%; border-collapse: separate; border-spacing: 8px;">
            <tr>
              {''.join(us_cards_html)}
            </tr>
          </table>
        </div>
        """

    # 4. Economic Calendar Section
    tz = as_of.tzinfo or ZoneInfo("Europe/Istanbul")
    upcoming_events = _filter_and_sort_upcoming_events(events, as_of, tz)
    cal_html_items = []
    cal_text_lines = []

    is_next_week = as_of.weekday() >= 4
    cal_title_prefix = "Gelecek Hafta" if is_next_week else "Bu Hafta"
    cal_title_prefix_cap = "Gelecek hafta" if is_next_week else "Bu hafta"
    cal_title_prefix_upper = "GELECEK HAFTA" if is_next_week else "BU HAFTA"

    if not upcoming_events:
        cal_html = f"""
        <div style="background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px; padding: 14px; text-align: center;">
          <p style="margin: 0; font-size: 13px; color: #6B7280;">{cal_title_prefix_cap} için gösterilecek önemli ekonomik veri bulunmuyor.</p>
        </div>
        """
        cal_text_lines.append(f"  {cal_title_prefix_cap} için gösterilecek önemli ekonomik veri bulunmuyor.")
    else:
        displayed_events = upcoming_events[:5]
        for ev, ev_dt in displayed_events:
            c_code = (ev.country or "TR").upper()
            country_badge_color = "#DC2626" if c_code == "TR" else "#2563EB"
            country_badge_bg = "#FEF2F2" if c_code == "TR" else "#EFF6FF"
            date_part = format_turkish_date(ev_dt.date())
            time_part = ev_dt.strftime("%H:%M")
            countdown = get_countdown_label(ev_dt, as_of)
            name_esc = html.escape(ev.name)

            cal_text_lines.append(f"  • [{c_code}] {countdown} — {ev.name}")
            cal_html_items.append(f"""
            <tr style="border-bottom: 1px solid #F1F5F9;">
              <td style="padding: 10px 12px; width: 44px; vertical-align: middle;">
                <span style="display: inline-block; padding: 2px 6px; font-size: 10px; font-weight: 700; border-radius: 4px; color: {country_badge_color}; background-color: {country_badge_bg};">
                  {html.escape(c_code)}
                </span>
              </td>
              <td style="padding: 10px 12px; vertical-align: middle;">
                <div style="font-size: 13px; font-weight: 600; color: #1E293B;">{name_esc}</div>
                <div style="font-size: 11px; color: #64748B; margin-top: 1px;">{html.escape(date_part)} • {html.escape(time_part)}</div>
              </td>
              <td style="padding: 10px 12px; text-align: right; vertical-align: middle; white-space: nowrap;">
                <span style="display: inline-block; padding: 2px 8px; font-size: 11px; font-weight: 600; color: #475569; background-color: #F1F5F9; border-radius: 9999px;">
                  {html.escape(countdown)}
                </span>
              </td>
            </tr>
            """)

        if len(upcoming_events) > 5:
            rem = len(upcoming_events) - 5
            cal_text_lines.append(f"  ... ve {rem} etkinlik daha")
            cal_html_items.append(f"""
            <tr>
              <td colspan="3" style="text-align: center; padding: 8px; font-size: 12px; color: #64748B; font-style: italic;">
                + {rem} etkinlik daha
              </td>
            </tr>
            """)

        cal_html = f"""
        <table role="presentation" style="width: 100%; border-collapse: collapse; border: 1px solid #E2E8F0; border-radius: 8px; overflow: hidden;">
          <tbody>
            {''.join(cal_html_items)}
          </tbody>
        </table>
        """

    # 5. Earnings Calendar Section (if any earnings events)
    session_labels_tr = {
        "pre_market": "Piyasa Öncesi",
        "after_hours": "Piyasa Sonrası",
        "time_not_supplied": "Saat Belirtilmedi",
    }
    earn_html_items = []
    earn_text_lines = []
    earn_section_html = ""

    if earnings_events:
        earn_text_lines.append("")
        earn_text_lines.append(f"{cal_title_prefix_upper} BİLANÇO AÇIKLAYACAK ŞİRKETLER:")
        displayed_earnings = earnings_events[:8]
        for earn in displayed_earnings:
            is_bist = "BIST" in earn.market
            m_code = "BIST" if is_bist else "NASDAQ"
            badge_color = "#DC2626" if is_bist else "#2563EB"
            badge_bg = "#FEF2F2" if is_bist else "#EFF6FF"

            period_text = f" • {earn.fiscal_year}/{earn.fiscal_period}" if (earn.fiscal_year and earn.fiscal_period) else (f" • {earn.fiscal_period}" if earn.fiscal_period else "")
            session_text = f" ({session_labels_tr.get(earn.session, earn.session)})" if earn.session and earn.session in session_labels_tr else ""
            date_part = format_turkish_date(earn.earnings_date)

            earn_text_lines.append(f"  • [{m_code}] {date_part}{session_text} — {earn.symbol} ({earn.company_name}){period_text}")

            sym_esc = html.escape(earn.symbol)
            comp_esc = html.escape(earn.company_name)
            meta_desc = f"{html.escape(date_part)}{html.escape(session_text)}{html.escape(period_text)}"

            earn_html_items.append(f"""
            <tr style="border-bottom: 1px solid #F1F5F9;">
              <td style="padding: 10px 12px; width: 56px; vertical-align: middle;">
                <span style="display: inline-block; padding: 2px 6px; font-size: 10px; font-weight: 700; border-radius: 4px; color: {badge_color}; background-color: {badge_bg};">
                  {html.escape(m_code)}
                </span>
              </td>
              <td style="padding: 10px 12px; vertical-align: middle;">
                <div style="font-size: 13px; font-weight: 600; color: #1E293B;">{sym_esc} <span style="font-size: 12px; font-weight: 400; color: #64748B;">({comp_esc})</span></div>
                <div style="font-size: 11px; color: #64748B; margin-top: 1px;">{meta_desc}</div>
              </td>
            </tr>
            """)

        if len(earnings_events) > 8:
            rem_earn = len(earnings_events) - 8
            earn_text_lines.append(f"  ... ve {rem_earn} şirket daha")
            earn_html_items.append(f"""
            <tr>
              <td colspan="2" style="text-align: center; padding: 8px; font-size: 12px; color: #64748B; font-style: italic;">
                + {rem_earn} şirket daha
              </td>
            </tr>
            """)

        earn_section_html = f"""
        <div style="margin-bottom: 20px;">
          <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 10px;">📊 {cal_title_prefix} Bilanço Açıklayacak Şirketler</div>
          <table role="presentation" style="width: 100%; border-collapse: collapse; border: 1px solid #E2E8F0; border-radius: 8px; overflow: hidden;">
            <tbody>
              {''.join(earn_html_items)}
            </tbody>
          </table>
        </div>
        """

    # Build Plain-Text Body
    text_sections = [
        "=" * 60,
        f"FinBot Güne Başlarken — {date_str} • {time_str}",
        "=" * 60,
        "",
        "TÜRKİYE PİYASALARI:",
        f"  • BIST 100   : {bist_price_str} {bist_change_text}".strip(),
        f"  • USD/TRY    : {usd_val}",
        f"  • EUR/TRY    : {eur_val}",
        f"  • Ons Altın  : {xau_val}",
        f"  • Gram Altın : {gram_val}",
        f"  • Bitcoin    : {btc_val}",
    ]

    if has_us:
        text_sections.append("")
        text_sections.extend(us_text_lines)

    text_sections.append("")
    text_sections.append(f"{cal_title_prefix_upper} TAKİP EDİLECEK EKONOMİK VERİLER:")
    text_sections.extend(cal_text_lines)

    if earn_text_lines:
        text_sections.extend(earn_text_lines)

    if unique_warnings:
        text_sections.append("")
        text_sections.append("UYARILAR:")
        for w in unique_warnings:
            text_sections.append(f"  ⚠️ {w}")

    text_sections.append("")
    text_sections.append("PAYLAŞIM METNİ")
    text_sections.append("-" * 30)
    text_sections.append(social_text)
    text_sections.append("-" * 30)
    text_sections.append("X’te paylaş:")
    text_sections.append(share_url)
    text_sections.append("")
    text_sections.append("-" * 60)
    text_sections.append("FinBot Otomasyon Sistemi • Bu e-posta bilgilendirme amaçlıdır.")
    text_sections.append("-" * 60)

    body_text = "\n".join(text_sections)

    # Build HTML Body
    warnings_html = _render_warnings_html(unique_warnings)
    share_box_html = _render_share_box_html(social_text, share_url)

    body_html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(subject)}</title>
</head>
<body style="margin: 0; padding: 20px 0; background-color: #F3F4F6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 640px; margin: 0 auto; background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
    
    <!-- HEADER -->
    <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" bgcolor="#1E3A8A" style="width: 100%; background-color: #1E3A8A;">
      <tr>
        <td bgcolor="#1E3A8A" style="padding: 24px; background-color: #1E3A8A; background-image: linear-gradient(135deg, #1E3A8A 0%, #0F172A 100%); color: #FFFFFF;">
          <table role="presentation" style="width: 100%; border-collapse: collapse; margin-bottom: 10px;">
            <tr>
              <td style="font-size: 12px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: #93C5FD;">FinBot Piyasa Bülteni</td>
              <td style="text-align: right;">
                <span style="display: inline-block; padding: 3px 10px; font-size: 11px; font-weight: 700; border-radius: 9999px; color: #1E3A8A; background-color: #DBEAFE;">Güne Başlarken</span>
              </td>
            </tr>
          </table>
          <h1 style="margin: 0 0 6px 0; font-size: 22px; font-weight: 700; color: #FFFFFF;">☀️ Güne Başlarken</h1>
          <div style="font-size: 13px; color: #CBD5E1;">
            {html.escape(date_str)} • <strong>{html.escape(time_str)}</strong>
          </div>
        </td>
      </tr>
    </table>

    <div style="padding: 24px;">
      
      <!-- BIST 100 HERO CARD -->
      <div style="margin-bottom: 16px; padding: 16px; background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px;">
        <table role="presentation" style="width: 100%; border-collapse: collapse;">
          <tr>
            <td style="vertical-align: middle;">
              <div style="font-size: 11px; font-weight: 600; color: #64748B; text-transform: uppercase; letter-spacing: 0.05em;">Borsa İstanbul</div>
              <div style="font-size: 20px; font-weight: 800; color: #0F172A; margin-top: 2px;">BIST 100</div>
            </td>
            <td style="text-align: right; vertical-align: middle;">
              <div style="font-size: 22px; font-weight: 800; color: #0F172A;">{html.escape(bist_price_str)}</div>
              <div style="margin-top: 4px;">{bist_badge_html}</div>
            </td>
          </tr>
        </table>
      </div>

      <!-- TURKEY MARKETS GRID -->
      <div style="margin-bottom: 20px;">
        <table role="presentation" style="width: 100%; border-collapse: separate; border-spacing: 8px;">
          <tr>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">USD/TRY</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(usd_val)}</div>
            </td>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">EUR/TRY</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(eur_val)}</div>
            </td>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Ons Altın</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(xau_val)}</div>
            </td>
          </tr>
          <tr>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Gram Altın</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(gram_val)}</div>
            </td>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Bitcoin</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(btc_val)}</div>
            </td>
            <td style="width: 33.33%;"></td>
          </tr>
        </table>
      </div>

      <!-- US MARKET SECTION -->
      {us_section_html}

      <!-- ECONOMIC CALENDAR SECTION -->
      <div style="margin-bottom: 20px;">
        <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 10px;">🗓 {cal_title_prefix} Takip Edilecek Ekonomik Veriler</div>
        {cal_html}
      </div>

      <!-- EARNINGS CALENDAR SECTION -->
      {earn_section_html}

      <!-- WARNINGS -->
      {warnings_html}

      <!-- SOCIAL SHARE CARD -->
      {share_box_html}

    </div>

    <!-- FOOTER -->
    <div style="padding: 16px 24px; background-color: #F9FAFB; border-top: 1px solid #E5E7EB; text-align: center; font-size: 11px; color: #9CA3AF;">
      Oluşturulma Saati: {html.escape(date_str)} • {html.escape(time_str)} • FinBot Otomasyon Sistemi
    </div>

  </div>
</body>
</html>
"""

    return MarketEmailContent(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        social_text=social_text,
        share_url=share_url,
    )


def build_closing_email_content(
    snapshot: MarketSnapshot,
    movers: Optional[BistMovers],
    as_of: datetime,
    warnings: Optional[List[str]] = None,
) -> MarketEmailContent:
    """Build complete, pure MarketEmailContent for Closing job."""
    date_str = format_turkish_date(as_of)
    time_str = as_of.strftime("%H:%M")
    subject = f"📊 FinBot Günün Kapanışı — {date_str}"

    # Merge and deduplicate warnings
    combined_warnings = list(snapshot.warnings or [])
    if warnings:
        combined_warnings.extend(warnings)
    unique_warnings = deduplicate_warnings(combined_warnings)

    market = snapshot.market
    bist = market.bist100

    # Social text and share URL
    social_text = build_closing_social_text(market, movers, as_of)
    share_url = build_x_share_url(social_text)

    # 1. BIST 100 Close & Intraday
    bist_price_str = format_turkish_number(bist.price, decimals=2) if (bist and bist.price is not None) else "Veri alınamadı"
    bist_badge_html = _render_change_badge_html(bist.change_percent) if bist else ""
    bist_change_text = format_signed_percent(bist.change_percent, mode="badge") if bist else ""

    intraday_text = ""
    intraday_html = ""
    if bist and bist.low is not None and bist.high is not None:
        low_str = format_turkish_number(bist.low, decimals=2)
        high_str = format_turkish_number(bist.high, decimals=2)
        intraday_text = f"Gün İçi: {low_str} — {high_str}"
        intraday_html = f"""
        <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #E2E8F0; font-size: 12px; color: #64748B;">
          <span style="font-weight: 600; color: #475569;">Gün İçi Aralık:</span> {low_str} — {high_str}
        </div>
        """

    # 2. Currencies, Gold, Crypto
    usd_val = format_turkish_number(market.usdtry.price, decimals=4) if (market.usdtry and market.usdtry.price is not None) else "—"
    eur_val = format_turkish_number(market.eurtry.price, decimals=4) if (market.eurtry and market.eurtry.price is not None) else "—"
    xau_val = f"${format_turkish_number(market.gold.xauusd, decimals=2)}" if (market.gold and market.gold.xauusd is not None) else "—"
    gram_val = f"{format_turkish_number(market.gold.gram_try, decimals=2)} TL" if (market.gold and market.gold.gram_try is not None) else "—"

    btc = getattr(market, "bitcoin", None)
    btc_val = f"${format_turkish_number(btc.usd, decimals=0)}" if (btc and btc.usd is not None) else "—"

    # 3. Movers Section (Gainers & Losers, stacked full-width for mobile/Outlook safety)
    gainers_rows = []
    losers_rows = []
    has_gainers = bool(movers and movers.gainers)
    has_losers = bool(movers and movers.losers)

    movers_text_lines = []

    if has_gainers:
        movers_text_lines.append("")
        movers_text_lines.append("EN ÇOK YÜKSELENLER:")
        for g in movers.gainers[:3]:
            p_str = f" {format_turkish_number(g.price, decimals=2)} TL" if g.price is not None else ""
            pct_str = format_signed_percent(g.change_percent, mode="social")
            movers_text_lines.append(f"  • {g.symbol:<8}:{p_str} {pct_str}".strip())
            pct_badge = _render_change_badge_html(g.change_percent)
            p_html = f"{format_turkish_number(g.price, decimals=2)} TL" if g.price is not None else "—"
            gainers_rows.append(f"""
            <tr style="border-bottom: 1px solid #F1F5F9;">
              <td style="padding: 8px 10px; font-size: 13px; font-weight: 700; color: #166534;">{html.escape(g.symbol)}</td>
              <td style="padding: 8px 10px; font-size: 12px; color: #475569; text-align: right;">{html.escape(p_html)}</td>
              <td style="padding: 8px 10px; text-align: right; width: 75px;">{pct_badge}</td>
            </tr>
            """)

    if has_losers:
        movers_text_lines.append("")
        movers_text_lines.append("EN ÇOK DÜŞENLER:")
        for loser in movers.losers[:3]:
            p_str = f" {format_turkish_number(loser.price, decimals=2)} TL" if loser.price is not None else ""
            pct_str = format_signed_percent(loser.change_percent, mode="social")
            movers_text_lines.append(f"  • {loser.symbol:<8}:{p_str} {pct_str}".strip())
            pct_badge = _render_change_badge_html(loser.change_percent)
            p_html = f"{format_turkish_number(loser.price, decimals=2)} TL" if loser.price is not None else "—"
            losers_rows.append(f"""
            <tr style="border-bottom: 1px solid #F1F5F9;">
              <td style="padding: 8px 10px; font-size: 13px; font-weight: 700; color: #991B1B;">{html.escape(loser.symbol)}</td>
              <td style="padding: 8px 10px; font-size: 12px; color: #475569; text-align: right;">{html.escape(p_html)}</td>
              <td style="padding: 8px 10px; text-align: right; width: 75px;">{pct_badge}</td>
            </tr>
            """)

    movers_section_html = ""
    if has_gainers or has_losers:
        tables_html = []
        if has_gainers:
            tables_html.append(f"""
            <div style="margin-bottom: 14px;">
              <div style="font-size: 12px; font-weight: 700; color: #166534; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.05em;">🚀 En Çok Yükselenler</div>
              <table role="presentation" style="width: 100%; border-collapse: collapse; border: 1px solid #E2E8F0; border-radius: 6px; overflow: hidden; background-color: #FAFCFA;">
                <tbody>
                  {''.join(gainers_rows)}
                </tbody>
              </table>
            </div>
            """)
        if has_losers:
            tables_html.append(f"""
            <div style="margin-bottom: 6px;">
              <div style="font-size: 12px; font-weight: 700; color: #991B1B; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.05em;">📉 En Çok Düşenler</div>
              <table role="presentation" style="width: 100%; border-collapse: collapse; border: 1px solid #E2E8F0; border-radius: 6px; overflow: hidden; background-color: #FCFAFA;">
                <tbody>
                  {''.join(losers_rows)}
                </tbody>
              </table>
            </div>
            """)
        movers_section_html = f"""
        <div style="margin-bottom: 20px;">
          <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 10px;">📊 BIST Hisseleri Öne Çıkanlar</div>
          {''.join(tables_html)}
        </div>
        """

    # Build Plain-Text Body
    text_sections = [
        "=" * 60,
        f"FinBot Günün Kapanışı — {date_str} • {time_str}",
        "=" * 60,
        "",
        "BIST 100 KAPANIŞI:",
        f"  • Kapanış    : {bist_price_str} {bist_change_text}".strip(),
    ]
    if intraday_text:
        text_sections.append(f"  • {intraday_text}")

    text_sections.extend([
        "",
        "PİYASA GÖRÜNÜMÜ:",
        f"  • USD/TRY    : {usd_val}",
        f"  • EUR/TRY    : {eur_val}",
        f"  • Ons Altın  : {xau_val}",
        f"  • Gram Altın : {gram_val}",
        f"  • Bitcoin    : {btc_val}",
    ])

    if movers_text_lines:
        text_sections.extend(movers_text_lines)

    if unique_warnings:
        text_sections.append("")
        text_sections.append("UYARILAR:")
        for w in unique_warnings:
            text_sections.append(f"  ⚠️ {w}")

    text_sections.append("")
    text_sections.append("PAYLAŞIM METNİ")
    text_sections.append("-" * 30)
    text_sections.append(social_text)
    text_sections.append("-" * 30)
    text_sections.append("X’te paylaş:")
    text_sections.append(share_url)
    text_sections.append("")
    text_sections.append("-" * 60)
    text_sections.append("FinBot Otomasyon Sistemi • Bu e-posta bilgilendirme amaçlıdır.")
    text_sections.append("-" * 60)

    body_text = "\n".join(text_sections)

    # Build HTML Body
    warnings_html = _render_warnings_html(unique_warnings)
    share_box_html = _render_share_box_html(social_text, share_url)

    body_html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(subject)}</title>
</head>
<body style="margin: 0; padding: 20px 0; background-color: #F3F4F6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 640px; margin: 0 auto; background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
    
    <!-- HEADER -->
    <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" bgcolor="#0F172A" style="width: 100%; background-color: #0F172A;">
      <tr>
        <td bgcolor="#0F172A" style="padding: 24px; background-color: #0F172A; background-image: linear-gradient(135deg, #0F172A 0%, #1E293B 100%); color: #FFFFFF;">
          <table role="presentation" style="width: 100%; border-collapse: collapse; margin-bottom: 10px;">
            <tr>
              <td style="font-size: 12px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: #94A3B8;">FinBot Piyasa Bülteni</td>
              <td style="text-align: right;">
                <span style="display: inline-block; padding: 3px 10px; font-size: 11px; font-weight: 700; border-radius: 9999px; color: #0F172A; background-color: #E2E8F0;">Günün Kapanışı</span>
              </td>
            </tr>
          </table>
          <h1 style="margin: 0 0 6px 0; font-size: 22px; font-weight: 700; color: #FFFFFF;">📊 Günün Kapanışı</h1>
          <div style="font-size: 13px; color: #CBD5E1;">
            {html.escape(date_str)} • <strong>{html.escape(time_str)}</strong>
          </div>
        </td>
      </tr>
    </table>

    <div style="padding: 24px;">
      
      <!-- BIST 100 HERO CARD -->
      <div style="margin-bottom: 16px; padding: 16px; background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px;">
        <table role="presentation" style="width: 100%; border-collapse: collapse;">
          <tr>
            <td style="vertical-align: middle;">
              <div style="font-size: 11px; font-weight: 600; color: #64748B; text-transform: uppercase; letter-spacing: 0.05em;">Borsa İstanbul</div>
              <div style="font-size: 20px; font-weight: 800; color: #0F172A; margin-top: 2px;">BIST 100 Kapanışı</div>
            </td>
            <td style="text-align: right; vertical-align: middle;">
              <div style="font-size: 22px; font-weight: 800; color: #0F172A;">{html.escape(bist_price_str)}</div>
              <div style="margin-top: 4px;">{bist_badge_html}</div>
            </td>
          </tr>
        </table>
        {intraday_html}
      </div>

      <!-- TURKEY MARKETS GRID -->
      <div style="margin-bottom: 20px;">
        <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 10px;">Piyasa Görünümü</div>
        <table role="presentation" style="width: 100%; border-collapse: separate; border-spacing: 8px;">
          <tr>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">USD/TRY</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(usd_val)}</div>
            </td>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">EUR/TRY</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(eur_val)}</div>
            </td>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Ons Altın</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(xau_val)}</div>
            </td>
          </tr>
          <tr>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Gram Altın</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(gram_val)}</div>
            </td>
            <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px;">
              <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Bitcoin</div>
              <div style="font-size: 15px; font-weight: 700; color: #111827; margin-top: 4px;">{html.escape(btc_val)}</div>
            </td>
            <td style="width: 33.33%;"></td>
          </tr>
        </table>
      </div>

      <!-- MOVERS SECTION -->
      {movers_section_html}

      <!-- WARNINGS -->
      {warnings_html}

      <!-- SOCIAL SHARE CARD -->
      {share_box_html}

    </div>

    <!-- FOOTER -->
    <div style="padding: 16px 24px; background-color: #F9FAFB; border-top: 1px solid #E5E7EB; text-align: center; font-size: 11px; color: #9CA3AF;">
      Oluşturulma Saati: {html.escape(date_str)} • {html.escape(time_str)} • FinBot Otomasyon Sistemi
    </div>

  </div>
</body>
</html>
"""

    return MarketEmailContent(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        social_text=social_text,
        share_url=share_url,
    )
