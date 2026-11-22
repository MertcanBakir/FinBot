from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.jobs.push_notifications_worker import main, process_one
from app.services.expo_push import ExpoPush
from app.services.push_notifications import plan


def preferences(**changes):
    return dict(
        timezone="Europe/Istanbul",
        earnings_morning=True,
        macro_morning=True,
        earnings_midday=True,
        daily_portfolio=True,
        **changes,
    )


def moment(hour, minute=0):
    return datetime(2026, 11, 20, hour - 3, minute, tzinfo=timezone.utc)


def company(symbol="AAA"):
    return dict(id=uuid4(), instrument_id=uuid4(), symbol=symbol)


def test_morning_at_ten_then_actual_fifteen_minute_slot():
    earnings, macro = (
        [company()],
        [dict(id=uuid4(), name="TÜFE", scheduled_at=moment(13, 15))],
    )
    assert plan(moment(9, 59), preferences(), earnings, macro) == []
    first = plan(moment(10), preferences(), earnings, macro)
    assert [n.kind for n in first] == ["earnings_morning"]
    second = plan(moment(10, 15), preferences(), earnings, macro)
    assert [n.kind for n in second] == ["earnings_morning", "economic_reminder"]
    assert second[1].scheduled_at - second[0].scheduled_at == timedelta(minutes=15)
    assert not any(
        n.kind.endswith("morning")
        for n in plan(moment(13), preferences(), earnings, macro)
    )


def test_macro_uses_event_slot_and_empty_sources_create_nothing():
    assert (
        plan(
            moment(10),
            preferences(),
            [],
            [dict(id=uuid4(), name="CPI", scheduled_at=moment(13))],
        )[0].kind
        == "economic_reminder"
    )
    assert plan(moment(10), preferences(), [], []) == []


def test_midday_starts_thirteen_thirty_deduplicates_and_orders():
    a, b = company("AAA"), company("BBB")
    prefs = preferences()
    prefs.update(earnings_morning=False, macro_morning=False)
    assert plan(moment(13, 29), prefs, [b, a, a], []) == []
    assert len(plan(moment(13, 30), prefs, [b, a, a], [])) == 1
    result = plan(moment(13, 45), prefs, [b, a, a], [])
    assert len(result) == 2 and result[0].event_key == str(a["instrument_id"])
    assert result[1].scheduled_at - result[0].scheduled_at == timedelta(minutes=15)
    assert all(n.data["schema_version"] == 1 for n in result)


@pytest.mark.parametrize(
    "flow,closing,pnl",
    [("100", "1150", "+50.00"), ("-100", "950", "+50.00"), ("0", "975", "-25.00")],
)
def test_daily_cash_is_not_profit(flow, closing, pnl):
    row = dict(
        snapshot_date=date(2026, 11, 19),
        previous_date=date(2026, 11, 18),
        valuation_status="success",
        previous_status="success",
        calculation_version=2,
        previous_version=2,
        total_value_base=Decimal(closing),
        previous_value=Decimal("1000"),
        external_cash_flow_base=Decimal(flow),
        base_currency="TRY",
        portfolio_id=uuid4(),
        id=uuid4(),
    )
    now = datetime(2026, 11, 19, 21, tzinfo=timezone.utc)
    result = plan(now, preferences(), [], [], row)
    assert len(result) == 1 and pnl + " TRY" in result[0].body
    assert result[0].data["event_date"] == "2026-11-19"
    row["valuation_status"] = "partial"
    assert plan(now, preferences(), [], [], row) == []
    row["valuation_status"] = "success"
    assert plan(now + timedelta(minutes=30), preferences(), [], [], row) == []


@pytest.mark.parametrize(
    "response,receipts,status,disable",
    [
        ({"failure": "unknown"}, False, "unknown", False),
        ({"failure": "retry"}, False, "pending", False),
        ({"data": {"status": "ok", "id": "ticket"}}, False, "ticket", False),
        ({"data": {"ticket": {"status": "ok"}}}, True, "accepted", False),
        (
            {
                "data": {
                    "ticket": {
                        "status": "error",
                        "details": {"error": "DeviceNotRegistered"},
                    }
                }
            },
            True,
            "failed",
            True,
        ),
        ({"data": {}}, True, "ticket", False),
    ],
)
def test_expo_state_machine(response, receipts, status, disable):
    repo, expo = Mock(), Mock()
    now = moment(10)
    repo.claim.return_value = {
        "attempts": 1,
        "ticket_id": "ticket",
        "attempted_at": now,
    }
    expo.send.return_value = expo.receipt.return_value = response
    assert process_one(repo, expo, now, receipts) == status
    assert repo.finish.call_args.args[2] == status
    assert repo.finish.call_args.kwargs.get("disable", False) == disable
    assert expo.send.call_count == int(not receipts)


def test_missing_receipt_expires_and_network_outcome_is_not_retried():
    repo, expo = Mock(), Mock()
    now = moment(10)
    repo.claim.return_value = {
        "attempts": 1,
        "ticket_id": "ticket",
        "attempted_at": now - timedelta(hours=24),
    }
    expo.receipt.return_value = {"data": {}}
    process_one(repo, expo, now, True)
    assert repo.finish.call_args.args[2] == "unknown"


def test_worker_requires_explicit_write_and_send_flag():
    with pytest.raises(SystemExit) as exc:
        main(["--once"])
    assert exc.value.code == 2


def test_retry_budget_is_bounded_and_oversized_payload_never_sends():
    repo, expo = Mock(), Mock()
    repo.claim.return_value = {"attempts": 4}
    expo.send.return_value = {"failure": "retry"}
    assert process_one(repo, expo, moment(10)) == "failed"
    adapter = ExpoPush()
    adapter.request = Mock()
    result = adapter.send(
        dict(
            token="ExponentPushToken[private123]",
            title="Title",
            body="x" * 5000,
            data={},
            notification_id=uuid4(),
            expires_at=moment(13),
        )
    )
    assert result == {"failure": "permanent"}
    adapter.request.assert_not_called()


def test_daily_keeps_turkish_cutoff_with_other_preference_timezone():
    row = dict(
        snapshot_date=date(2026, 11, 19),
        previous_date=date(2026, 11, 18),
        valuation_status="success",
        previous_status="success",
        calculation_version=2,
        previous_version=2,
        total_value_base=1000,
        previous_value=1000,
        external_cash_flow_base=0,
        base_currency="TRY",
        portfolio_id=uuid4(),
        id=uuid4(),
    )
    prefs = preferences()
    prefs["timezone"] = "America/New_York"
    now = datetime(2026, 11, 19, 21, tzinfo=timezone.utc)
    result = plan(now, prefs, [], [], row)
    assert len(result) == 1 and result[0].data["event_date"] == "2026-11-19"
    assert result[0].scheduled_at == now


def test_expiration_and_private_response_errors():
    expo = ExpoPush()
    expo.request = Mock(return_value={"data": {"status": "ok", "id": "t"}})
    item = dict(
        token="ExponentPushToken[private123]",
        title="Title",
        body="Body",
        data={"schema_version": 1},
        notification_id=uuid4(),
        expires_at=moment(13),
    )
    expo.send(item)
    assert expo.request.call_args.args[1]["expiration"] == int(moment(13).timestamp())


def test_curated_importance_covers_real_calendar_codes():
    from pathlib import Path

    from app.providers.calendar.tracked_events import CANONICAL_EVENT_CODES

    sql = (
        Path(__file__).parents[1]
        / "supabase/migrations/20261120000000_push_notifications.sql"
    ).read_text()
    for code in CANONICAL_EVENT_CODES.values():
        assert f"('{code}'," in sql
