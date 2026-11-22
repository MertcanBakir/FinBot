from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.push_notifications import plan


def at(hour, minute=0):
    return datetime(2026, 11, 22, hour, minute, tzinfo=ZoneInfo("Europe/Istanbul"))


def prefs(**changes):
    return {
        "timezone": "Europe/Istanbul",
        **dict.fromkeys(
            [
                "earnings_morning",
                "earnings_midday",
                "daily_portfolio",
                "watchlist_earnings",
                "economic_reminders",
                "earnings_today",
                "earnings_tomorrow",
            ],
            True,
        ),
        **changes,
    }


def event(code="AAA"):
    return dict(id=code, instrument_id=code, symbol=code)


def test_empty_digests_require_fresh_source():
    assert plan(at(10, 15), prefs(), [], []) == []
    result = plan(at(10, 15), prefs(), [], [], earnings_fresh=True)
    assert [n.kind for n in result] == ["earnings_morning", "earnings_today"]
    assert all(n.data["count"] == 0 for n in result)
    assert not any(n.kind == "earnings_tomorrow" for n in plan(at(20), prefs(), [], []))


def test_independent_watchlist_and_global_reminders():
    a, b = event(), event("BBB")
    result = plan(at(14, 30), prefs(), [a], [], watchlist=[a, b], all_earnings=[a, b])
    watch = next(n for n in result if n.kind == "watchlist_earnings")
    assert watch.data["instrument_ids"] == ["BBB"]
    result = plan(at(14, 30), prefs(earnings_midday=False), [a], [], watchlist=[a, b])
    assert next(n for n in result if n.kind == "watchlist_earnings").data["count"] == 2


def test_macro_three_precise_slots_no_backlog_and_no_legacy_digest():
    e = dict(id="macro", name="ABD TÜFE", scheduled_at=at(16, 30))
    keys = []
    for hour, minute, offset in [(13, 30, 180), (15, 30, 60), (16, 15, 15)]:
        result = plan(at(hour, minute), prefs(), [], [e])
        notice = next(n for n in result if n.kind == "economic_reminder")
        assert notice.data["minutes_before"] == offset
        assert notice.expires_at == at(hour, minute) + timedelta(minutes=5)
        keys.append(notice.event_key)
        assert not any(n.kind == "macro_morning" for n in result)
    assert len(set(keys)) == 3
    assert plan(at(15, 36), prefs(), [], [e]) == []
    assert plan(at(16, 30), prefs(), [], [e]) == []
    assert plan(at(15, 30), prefs(economic_reminders=False), [], [e]) == []


def test_tomorrow_uses_next_date_and_today_schedule():
    result = plan(at(20), prefs(), [], [], tomorrow=[event()])
    notice = next(n for n in result if n.kind == "earnings_tomorrow")
    assert notice.data["event_date"] == "2026-11-23"
    assert notice.scheduled_at == at(20)


def test_daily_body_never_exposes_total_value():
    day = at(0).date() - timedelta(days=1)
    row = dict(
        snapshot_date=day,
        previous_date=day - timedelta(days=1),
        valuation_status="success",
        previous_status="success",
        calculation_version=2,
        previous_version=2,
        absolute_pnl_base=15,
        previous_absolute_pnl=5,
        total_value_base=999999,
        base_currency="TRY",
        portfolio_id="p",
        id="s",
    )
    notice = plan(at(0), prefs(), [], [], row)[0]
    assert "+10.00 TRY" in notice.body
    assert "999999" not in notice.body and "Portföy değeri" not in notice.body


def test_macro_across_midnight_and_unknown_time():
    e = dict(id="m", name="CPI", scheduled_at=at(23, 30) + timedelta(hours=3))
    notice = plan(at(23, 30), prefs(), [], [e])[0]
    assert notice.data["event_date"] == "2026-11-23" and notice.body.startswith("Yarın")
    assert plan(at(23, 30), prefs(), [], [dict(id="m", name="CPI")]) == []
    e["scheduled_at"] = datetime(2026, 11, 23, 2, 30)
    assert plan(at(23, 30), prefs(), [], [e]) == []


def test_digest_fingerprint_covers_ids_beyond_routing_limit():
    events = [event(f"A{i:02}") for i in range(25)]
    first = next(
        n
        for n in plan(at(10, 15), prefs(), [], [], all_earnings=events)
        if n.kind == "earnings_today"
    )
    assert len(first.data["instrument_ids"]) == 20 and first.data["count"] == 25
    events[-1] = event("ZZZ")
    second = next(
        n
        for n in plan(at(10, 15), prefs(), [], [], all_earnings=events)
        if n.kind == "earnings_today"
    )
    assert first.data["instrument_ids"] == second.data["instrument_ids"]
    assert first.data["content_hash"] != second.data["content_hash"]
