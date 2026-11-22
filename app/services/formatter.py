from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from app.core.market_session import MarketSession
from app.models.market import BistMovers, EconomicEvent, MarketData

WEEKDAY_TR = {
    0: "Pazartesi",
    1: "Salı",
    2: "Çarşamba",
    3: "Perşembe",
    4: "Cuma",
    5: "Cumartesi",
    6: "Pazar"
}

def fmt_num(val: Optional[float], decimals: int = 2, prefix: str = "", suffix: str = "") -> str:
    if val is None:
        return "Veri yok"
    formatted = f"{val:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{prefix}{formatted}{suffix}"

def fmt_change(change_pct: Optional[float]) -> str:
    if change_pct is None:
        return ""
    abs_val = abs(change_pct)
    formatted_pct = f"{abs_val:.2f}".replace(".", ",")
    if change_pct > 0:
        return f"▲ %{formatted_pct}"
    elif change_pct < 0:
        return f"▼ %{formatted_pct}"
    else:
        return f"%{formatted_pct}"

def get_countdown_str(event_dt: datetime, today_dt: datetime) -> str:
    diff_days = (event_dt.date() - today_dt.date()).days
    time_str = event_dt.strftime("%H:%M")
    
    if diff_days == 0:
        return f"Bugün {time_str}"
    elif diff_days == 1:
        return f"Yarın {time_str}"
    elif diff_days == 2:
        return f"2 gün sonra {time_str}"
    else:
        day_name = WEEKDAY_TR.get(event_dt.weekday(), "")
        return f"{day_name} {time_str} ({diff_days} gün kaldı)"

def format_instant_summary(market: MarketData, session: MarketSession, time_str: str) -> str:
    bist = market.bist100
    usd = market.usdtry
    eur = market.eurtry
    gold = market.gold
    btc = getattr(market, "bitcoin", None)

    bist_str = f"{fmt_num(bist.price)} {fmt_change(bist.change_percent)}".strip()
    usd_str = fmt_num(usd.price)
    eur_str = fmt_num(eur.price)
    xau_str = fmt_num(gold.xauusd, decimals=0, prefix="$")
    gram_str = fmt_num(gold.gram_try, decimals=0, suffix=" TL")

    lines = []

    if session == MarketSession.INTRADAY:
        lines.extend([
            "Borsa İstanbul'da güncel görünüm:",
            "",
            f"BIST 100: {bist_str}",
            f"Dolar/TL: {usd_str}",
            f"Euro/TL: {eur_str}",
            f"Ons Altın: {xau_str}",
            f"Gram Altın: {gram_str}"
        ])
        if btc and btc.usd is not None:
            lines.append(f"Bitcoin: {fmt_num(btc.usd, decimals=0, prefix='$')}")
        lines.extend(["", f"Veri zamanı: {time_str}"])

    elif session == MarketSession.CLOSED:
        change_text = "yükselişle"
        if bist.change_percent is not None:
            if bist.change_percent < 0:
                change_text = "düşüşle"
            elif bist.change_percent == 0:
                change_text = "yatay"
        pct_text = f" %{abs(bist.change_percent):.2f}".replace(".", ",") if bist.change_percent is not None else ""
        lines.extend([
            f"Borsa İstanbul günü{pct_text} {change_text} tamamladı.",
            "",
            f"BIST 100: {bist_str}"
        ])
        if bist.high is not None and bist.low is not None:
            lines.append(f"Gün içi: {fmt_num(bist.low)} — {fmt_num(bist.high)}")
        lines.extend([
            "",
            f"Dolar/TL: {usd_str}",
            f"Euro/TL: {eur_str}",
            f"Ons Altın: {xau_str}",
            f"Gram Altın: {gram_str}"
        ])
        if btc and btc.usd is not None:
            lines.append(f"Bitcoin: {fmt_num(btc.usd, decimals=0, prefix='$')}")

    elif session == MarketSession.PRE_MARKET:
        lines.extend([
            "Borsa İstanbul açılışı öncesi piyasalarda son görünüm:",
            "",
            f"BIST 100: {bist_str}",
            f"Dolar/TL: {usd_str}",
            f"Euro/TL: {eur_str}",
            f"Ons Altın: {xau_str}",
            f"Gram Altın: {gram_str}"
        ])
        if btc and btc.usd is not None:
            lines.append(f"Bitcoin: {fmt_num(btc.usd, decimals=0, prefix='$')}")
        lines.extend(["", f"Veri zamanı: {time_str}"])

    else:  # WEEKEND
        lines.extend([
            "Hafta sonu piyasalarda son görünüm:",
            "",
            f"BIST 100: {bist_str}",
            f"Dolar/TL: {usd_str}",
            f"Euro/TL: {eur_str}",
            f"Ons Altın: {xau_str}",
            f"Gram Altın: {gram_str}"
        ])
        if btc and btc.usd is not None:
            lines.append(f"Bitcoin: {fmt_num(btc.usd, decimals=0, prefix='$')}")

    return "\n".join(lines)

def format_morning_summary(market: MarketData, events: Optional[List[EconomicEvent]] = None, ref_dt: Optional[datetime] = None) -> str:
    bist = market.bist100
    btc = getattr(market, "bitcoin", None)
    us = getattr(market, "us_market", None)

    bist_str = f"{fmt_num(bist.price, decimals=0)} {fmt_change(bist.change_percent)}".strip()

    lines = [
        "Güne Başlarken | Piyasalar",
        "",
        f"BIST 100: {bist_str}",
        f"Dolar/TL: {fmt_num(market.usdtry.price)}",
        f"Euro/TL: {fmt_num(market.eurtry.price)}",
        f"Ons Altın: {fmt_num(market.gold.xauusd, decimals=0, prefix='$')}",
        f"Gram Altın: {fmt_num(market.gold.gram_try, decimals=0, suffix=' TL')}"
    ]

    if btc and btc.usd is not None:
        lines.append(f"Bitcoin: {fmt_num(btc.usd, decimals=0, prefix='$')}")

    # US Previous Close Section
    if us and (us.sp500 or us.nasdaq):
        lines.append("")
        lines.append("ABD önceki kapanış:")
        if us.sp500 and us.sp500.price is not None:
            sp_str = f"{fmt_num(us.sp500.price, decimals=0)} {fmt_change(us.sp500.change_percent)}".strip()
            lines.append(f"S&P 500: {sp_str}")
        if us.nasdaq and us.nasdaq.price is not None:
            nas_str = f"{fmt_num(us.nasdaq.price, decimals=0)} {fmt_change(us.nasdaq.change_percent)}".strip()
            lines.append(f"Nasdaq: {nas_str}")

    # Economic Calendar Section
    lines.append("")
    
    tz_tr = ZoneInfo("Europe/Istanbul")
    today_dt = ref_dt or datetime.now(tz_tr)
    
    # Filter upcoming events (exclude past days)
    upcoming_events = []
    if events:
        for ev in events:
            try:
                ev_dt = datetime.fromisoformat(ev.scheduled_at_istanbul).astimezone(tz_tr)
                # Keep if it is today or in the future of the current week
                if ev_dt.date() >= today_dt.date():
                    upcoming_events.append((ev, ev_dt))
            except Exception:
                continue

    upcoming_events.sort(key=lambda x: x[1])
    upcoming_events = upcoming_events[:3]

    if len(upcoming_events) > 0:
        # Check if there is an event today
        has_today = any((ev_dt.date() == today_dt.date()) for _, ev_dt in upcoming_events)
        if has_today:
            lines.append("Bugün takip edilecekler:")
        else:
            lines.append("Bu hafta takip edilecekler:")

        for ev, ev_dt in upcoming_events:
            diff_days = (ev_dt.date() - today_dt.date()).days
            time_part = ev_dt.strftime("%H:%M")
            if diff_days == 0:
                lines.append(f"• {time_part} — {ev.name}")
            elif diff_days == 1:
                lines.append(f"• Yarın {time_part} — {ev.name}")
            elif diff_days == 2:
                lines.append(f"• 2 gün sonra {time_part} — {ev.name}")
            else:
                day_name = WEEKDAY_TR.get(ev_dt.weekday(), "")
                lines.append(f"• {day_name} {time_part} — {ev.name} ({diff_days} gün kaldı)")
    else:
        lines.append("Bugün öne çıkan yüksek önem düzeyinde veri bulunmuyor.")

    return "\n".join(lines)

def format_closing_summary(market: MarketData, movers: Optional[BistMovers] = None) -> str:
    bist = market.bist100
    btc = getattr(market, "bitcoin", None)

    bist_str = f"{fmt_num(bist.price)} {fmt_change(bist.change_percent)}".strip()

    lines = [
        "Günün Kapanışı | Borsa İstanbul",
        "",
        f"BIST 100: {bist_str}"
    ]

    if bist.low is not None and bist.high is not None:
        lines.append(f"Gün içi: {fmt_num(bist.low)} — {fmt_num(bist.high)}")

    lines.extend([
        "",
        f"Dolar/TL: {fmt_num(market.usdtry.price)}",
        f"Euro/TL: {fmt_num(market.eurtry.price)}",
        f"Ons Altın: {fmt_num(market.gold.xauusd, decimals=0, prefix='$')}",
        f"Gram Altın: {fmt_num(market.gold.gram_try, decimals=0, suffix=' TL')}"
    ])

    if btc and btc.usd is not None:
        lines.append(f"Bitcoin: {fmt_num(btc.usd, decimals=0, prefix='$')}")

    if movers:
        if movers.gainers and len(movers.gainers) > 0:
            lines.append("")
            lines.append("En çok yükselenler:")
            for g in movers.gainers[:3]:
                pct_val = f"{g.change_percent:.2f}".replace(".", ",") if g.change_percent is not None else "0,00"
                lines.append(f"▲ {g.symbol} %{pct_val}")

        if movers.losers and len(movers.losers) > 0:
            lines.append("")
            lines.append("En çok düşenler:")
            for loser in movers.losers[:3]:
                pct_val = f"{abs(loser.change_percent):.2f}".replace(".", ",") if loser.change_percent is not None else "0,00"
                lines.append(f"▼ {loser.symbol} %{pct_val}")

    return "\n".join(lines)
