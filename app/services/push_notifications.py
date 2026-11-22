"""Deterministic notification planning. No network or database side effects."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Notice:
    kind: str
    event_key: str
    title: str
    body: str
    data: dict
    scheduled_at: datetime
    expires_at: datetime


def plan(
    now,
    preferences,
    earnings,
    macro,
    portfolio=None,
    *,
    watchlist=(),
    all_earnings=(),
    tomorrow=(),
    earnings_fresh=False,
    portfolio_available=True,
):
    local = now.astimezone(ZoneInfo(preferences["timezone"]))
    day = local.date()
    notices = []

    def add(
        kind, key, title, body, hour, minute=0, slot=0, payload=None, event_day=day
    ):
        when = datetime.combine(day, time(hour, minute), local.tzinfo) + timedelta(
            minutes=15 * slot
        )
        expiry = datetime.combine(day, time(23, 59), local.tzinfo)
        if kind in ("earnings_morning", "earnings_today"):
            expiry = datetime.combine(day, time(13), local.tzinfo)
        if kind == "daily_portfolio":
            turkey = now.astimezone(ZoneInfo("Europe/Istanbul"))
            when = datetime.combine(turkey.date(), time(0), turkey.tzinfo)
            expiry = when + timedelta(minutes=30)
        if when <= now < expiry:
            notices.append(
                Notice(
                    kind,
                    str(key),
                    title,
                    body,
                    {
                        "schema_version": 1,
                        "type": kind,
                        "event_date": event_day.isoformat(),
                        **(payload or {}),
                    },
                    when,
                    expiry,
                )
            )

    companies = {str(e["instrument_id"]): e for e in earnings}
    ordered = sorted(
        companies.values(), key=lambda e: (e["symbol"], str(e["instrument_id"]))
    )

    def digest(kind, events, title, empty, hour, minute=0, fresh=False, event_day=day):
        unique = sorted(
            {str(e["instrument_id"]): e for e in events}.values(),
            key=lambda e: (e["symbol"], str(e["instrument_id"])),
        )
        if preferences.get(kind, False) and (unique or fresh):
            add(
                kind,
                "daily",
                title if unique else empty,
                ", ".join(e["symbol"] for e in unique)[:500]
                + (
                    " için finansal sonuç açıklaması bekleniyor."
                    if unique
                    else "Güncel takvimde planlanmış bilanço açıklaması bulunmuyor."
                ),
                hour,
                minute,
                payload={
                    "instrument_ids": [str(e["instrument_id"]) for e in unique[:20]],
                    "count": len(unique),
                    "content_hash": content_hash(unique),
                },
                event_day=event_day,
            )

    if (
        portfolio_available
        and preferences.get("earnings_morning", False)
        and (ordered or earnings_fresh)
    ):
        add(
            "earnings_morning",
            "daily",
            "Bugünkü portföy bilançoların"
            if ordered
            else "Portföyünün bilanço takvimi sakin",
            (
                ", ".join(e["symbol"] for e in ordered)[:500]
                + " için finansal sonuçların bugün açıklanması bekleniyor."
            )
            if ordered
            else "Portföyündeki şirketler için bugün planlanmış bilanço açıklaması bulunmuyor.",
            10,
            payload={
                "instrument_ids": [str(e["instrument_id"]) for e in ordered[:20]],
                "count": len(ordered),
                "content_hash": content_hash(ordered),
            },
        )
    digest(
        "earnings_today",
        all_earnings,
        "Bugünün bilanço takvimi",
        "Bugün planlanmış bilanço yok",
        10,
        15,
        earnings_fresh,
    )
    watched = [
        e
        for e in watchlist
        if not (
            preferences.get("earnings_midday", False)
            and str(e["instrument_id"]) in companies
        )
    ]
    digest(
        "watchlist_earnings", watched, "Takip listendeki bugünkü bilançolar", "", 14, 30
    )
    digest(
        "earnings_tomorrow",
        tomorrow,
        "Yarının bilanço takvimi",
        "",
        20,
        event_day=day + timedelta(days=1),
    )
    if preferences.get("economic_reminders", preferences.get("macro_morning", False)):
        for event in macro:
            scheduled = event.get("scheduled_at")
            if not isinstance(scheduled, datetime) or scheduled.tzinfo is None:
                continue
            for minutes in (180, 60, 15):
                when = scheduled - timedelta(minutes=minutes)
                expiry = min(when + timedelta(minutes=5), scheduled)
                if when <= now < expiry:
                    event_date = scheduled.astimezone(local.tzinfo).date()
                    notices.append(
                        Notice(
                            "economic_reminder",
                            f"{event['id']}:{scheduled.isoformat()}:{minutes}",
                            f"{event['name']} verisine {minutes} dakika kaldı",
                            f"{'Bugün' if event_date == day else 'Yarın'} {scheduled.astimezone(local.tzinfo):%H:%M}’da açıklanması bekleniyor.",
                            {
                                "schema_version": 1,
                                "type": "economic_reminder",
                                "event_date": event_date.isoformat(),
                                "event_id": str(event["id"]),
                                "scheduled_at": scheduled.isoformat(),
                                "minutes_before": minutes,
                            },
                            when,
                            expiry,
                        )
                    )
    if preferences.get("earnings_midday", False):
        for index, event in enumerate(ordered):
            add(
                "earnings_midday",
                event["instrument_id"],
                f"{event['symbol']} bilançosu bugün",
                "Finansal sonuçların bugün açıklanması bekleniyor.",
                13,
                30,
                index,
                {
                    "instrument_id": str(event["instrument_id"]),
                    "event_id": str(event["id"]),
                },
            )
    if preferences.get("daily_portfolio", False) and portfolio:
        previous_day = now.astimezone(ZoneInfo("Europe/Istanbul")).date() - timedelta(
            days=1
        )
        if (
            portfolio["snapshot_date"] == previous_day
            and portfolio["previous_date"] == previous_day - timedelta(days=1)
            and portfolio["valuation_status"]
            == portfolio["previous_status"]
            == "success"
            and portfolio["calculation_version"] == portfolio["previous_version"] == 2
        ):
            if "previous_absolute_pnl" in portfolio:
                pnl = Decimal(portfolio["absolute_pnl_base"]) - Decimal(
                    portfolio["previous_absolute_pnl"]
                )
            else:
                pnl = (
                    Decimal(portfolio["total_value_base"])
                    - Decimal(portfolio["previous_value"])
                    - Decimal(portfolio["external_cash_flow_base"])
                )
            add(
                "daily_portfolio",
                portfolio["portfolio_id"],
                "Günlük portföy özetin hazır",
                f"Dünkü kâr/zararın: {pnl:+.2f} {portfolio['base_currency']}.",
                0,
                payload={
                    "portfolio_id": str(portfolio["portfolio_id"]),
                    "snapshot_id": str(portfolio["id"]),
                    "currency": portfolio["base_currency"],
                    "valuation_date": previous_day.isoformat(),
                },
                event_day=previous_day,
            )
    return notices


def content_hash(events):
    """Bounded payload fingerprint covers every symbol, including beyond first 20 IDs."""
    values = sorted({f"{e['instrument_id']}:{e['symbol']}" for e in events})
    return sha256(",".join(values).encode()).hexdigest()
