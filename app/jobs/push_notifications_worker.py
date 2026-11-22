"""Persistent local scheduler. No GitHub trigger, import-time work or raw logs."""

import argparse
import logging
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from uuid import UUID

from app.repositories.push_notifications_repository import PushRepository
from app.services.expo_push import ExpoPush


def process_one(repository, expo, now, receipts=False):
    item = repository.claim(now, receipts)
    if not item:
        return False
    response = expo.receipt(item["ticket_id"]) if receipts else expo.send(item)
    failure = response.get("failure")
    if failure:
        status = (
            "ticket"
            if receipts
            else (
                "pending"
                if failure == "retry" and item["attempts"] < 4
                else "unknown"
                if failure == "unknown"
                else "failed"
            )
        )
        if receipts and now - item["attempted_at"] > timedelta(hours=23):
            status = "unknown"
        repository.finish(item, now, status, "EXPO_" + failure.upper())
        return status
    data = response.get("data", {})
    result = data.get(item["ticket_id"]) if receipts else data
    if not isinstance(result, dict):
        status = (
            "unknown"
            if not receipts or now - item["attempted_at"] > timedelta(hours=23)
            else "ticket"
        )
        repository.finish(
            item, now, status, "RECEIPT_MISSING" if receipts else "INVALID_TICKET"
        )
    elif result.get("status") == "ok":
        if not receipts and not isinstance(result.get("id"), str):
            status = "unknown"
            repository.finish(item, now, status, "INVALID_TICKET")
        else:
            status = "accepted" if receipts else "ticket"
            repository.finish(item, now, status, ticket=result.get("id"))
    else:
        error = (result.get("details") or {}).get("error")
        code = (
            error
            if error
            in {
                "DeviceNotRegistered",
                "MessageTooBig",
                "MessageRateExceeded",
                "InvalidCredentials",
                "MismatchSenderId",
            }
            else "EXPO_REJECTED"
        )
        status = (
            "pending"
            if not receipts and error == "MessageRateExceeded" and item["attempts"] < 4
            else "failed"
        )
        repository.finish(
            item, now, status, code, disable=error == "DeviceNotRegistered"
        )
    return status


def process_batch(repository, expo, stop, receipts):
    counts = {"processed_count": 0}
    for _ in range(100):
        if stop.is_set():
            break
        status = process_one(repository, expo, datetime.now(timezone.utc), receipts)
        if not status:
            break
        counts["processed_count"] += 1
        counts[status] = counts.get(status, 0) + 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--apply-db",
        action="store_true",
        help="Enable database writes and real Expo delivery",
    )
    args = parser.parse_args(argv)
    if not args.apply_db:
        parser.error(
            "--apply-db is required; this worker writes to the database and sends push notifications"
        )
    project = UUID(os.environ["EXPO_PROJECT_ID"])
    environment = os.environ.get("PUSH_ENVIRONMENT", "production")
    if environment not in ("production", "development"):
        raise ValueError("INVALID_PUSH_ENVIRONMENT")
    repository = PushRepository(project, environment)
    expo = ExpoPush(os.environ.get("EXPO_ACCESS_TOKEN"))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            now = datetime.now(timezone.utc)
            repository.prepare(now)
            for receipts in (False, True):
                # First check is non-mutating; empty polls produce no run rows.
                with repository.transaction() as cur:
                    cur.execute(
                        """SELECT 1 FROM notifications.deliveries d JOIN notifications.devices v ON v.id=d.device_id
                        JOIN notifications.outbox o ON o.id=d.notification_id
                        WHERE d.status=%s AND d.next_at<=%s AND v.project_id=%s AND v.environment=%s
                        AND (d.lease_until IS NULL OR d.lease_until<=%s)
                        AND (%s OR o.scheduled_at<=%s) LIMIT 1""",
                        (
                            "ticket" if receipts else "pending",
                            now,
                            project,
                            environment,
                            now,
                            receipts,
                            now,
                        ),
                    )
                    due = bool(cur.fetchone())
                if due:
                    from app.services.push_tracking import tracked_batch

                    tracked_batch(
                        partial(process_batch, repository, expo, stop, receipts)
                    )
        except Exception:
            logging.error("push_cycle_failed")
            if args.once:
                return 1
        if args.once:
            break
        stop.wait(30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
