"""Private outbox repository. Short explicit transactions; no network under locks."""

from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.database import get_db_connection
from app.services.push_notifications import plan


class PushRepository:
    def __init__(self, project_id, environment, factory=get_db_connection):
        self.project_id, self.environment, self.factory = (
            project_id,
            environment,
            factory,
        )

    @contextmanager
    def transaction(self):
        with (
            self.factory() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute("SET LOCAL statement_timeout='20s'")
            yield cur

    def prepare(self, now):
        count = 0
        with self.transaction() as cur:
            # Reap even when there are no new pending/ticket rows to claim.
            cur.execute(
                "UPDATE notifications.deliveries SET status='unknown',error_code='SEND_OUTCOME_UNKNOWN' WHERE status='sending' AND lease_until<=%s",
                (now,),
            )
            cur.execute(
                """UPDATE notifications.deliveries d SET status='cancelled',error_code='EXPIRED'
                FROM notifications.outbox o WHERE d.notification_id=o.id
                AND d.status='pending' AND o.expires_at<=%s""",
                (now,),
            )
            cur.execute("SELECT * FROM notifications.preferences")
            users = cur.fetchall()
        for prefs in users:
            with self.transaction() as cur:
                # Group each user's preparation writes in one transaction.
                from zoneinfo import ZoneInfo

                day = now.astimezone(ZoneInfo(prefs["timezone"])).date()
                turkey_now = now.astimezone(ZoneInfo("Europe/Istanbul"))
                snapshot_day = turkey_now.date() - timedelta(days=1)
                cur.execute(
                    "SELECT id FROM public.portfolios WHERE user_id=%s AND archived_at IS NULL ORDER BY is_default DESC,created_at,id",
                    (prefs["user_id"],),
                )
                portfolios = cur.fetchall()
                # General/watchlist alerts remain available for multi-portfolio users.
                portfolio_id = portfolios[0]["id"] if len(portfolios) == 1 else None
                cur.execute(
                    """SELECT DISTINCT ON(e.instrument_id,e.earnings_date)
                    e.id,e.instrument_id,i.symbol,e.earnings_date,
                    EXISTS(SELECT 1 FROM public.user_watchlist_items w
                      WHERE w.user_id=%s AND w.instrument_id=e.instrument_id) watched,
                    EXISTS(SELECT 1 FROM public.portfolios pf
                      CROSS JOIN LATERAL private.get_portfolio_position_summary(pf.id,e.instrument_id) s
                      WHERE pf.user_id=%s AND pf.archived_at IS NULL AND s.current_quantity>0) held
                    FROM public.earnings_events e
                    JOIN public.instruments i ON i.id=e.instrument_id
                    WHERE e.earnings_date BETWEEN %s AND %s AND e.is_active AND NOT e.is_stale
                    AND i.is_active AND i.asset_type='stock' AND i.market IN ('BIST100','NASDAQ100')
                    AND e.last_seen_at>=%s
                    ORDER BY e.instrument_id,e.earnings_date,e.last_seen_at DESC,e.id DESC""",
                    (
                        prefs["user_id"],
                        prefs["user_id"],
                        day,
                        day + timedelta(days=1),
                        now - timedelta(hours=36),
                    ),
                )
                events = cur.fetchall()
                today = [e for e in events if e["earnings_date"] == day]
                earnings = [e for e in today if e["held"]]
                watched = [e for e in today if e["watched"]]
                tomorrow = [e for e in events if e["earnings_date"] != day]
                cur.execute(
                    "SELECT notifications.earnings_source_fresh(%s,%s) fresh",
                    (day, now),
                )
                freshness = cur.fetchone()
                cur.execute(
                    """SELECT e.id,e.name,e.scheduled_at FROM public.economic_events e
                    JOIN notifications.event_importance m ON m.event_code=e.event_code
                    WHERE e.is_active AND NOT e.is_stale AND e.last_seen_at>=%s
                    AND e.scheduled_at>%s AND e.scheduled_at<=%s
                    AND e.country=ANY(%s) AND m.importance>=%s ORDER BY e.scheduled_at,e.id""",
                    (
                        now - timedelta(hours=36),
                        now,
                        now + timedelta(hours=3),
                        prefs["countries"],
                        prefs["min_importance"],
                    ),
                )
                macro = cur.fetchall()
                # Production's builder accepts today's date only. Read existing
                # completed captures; never rebuild a historical date here.
                cur.execute(
                    """SELECT s.*,p.snapshot_date previous_date,p.total_value_base previous_value,p.valuation_status previous_status,p.calculation_version previous_version,p.absolute_pnl_base previous_absolute_pnl
                    FROM public.portfolio_snapshots s JOIN public.portfolio_snapshots p ON p.portfolio_id=s.portfolio_id
                    AND p.snapshot_date=s.snapshot_date-1 AND p.base_currency=s.base_currency
                    WHERE s.portfolio_id=%s AND s.snapshot_date=%s""",
                    (portfolio_id, snapshot_day),
                )
                snapshot = cur.fetchone()
                planned = plan(
                    now,
                    prefs,
                    earnings,
                    macro,
                    snapshot,
                    watchlist=watched,
                    all_earnings=today,
                    tomorrow=tomorrow,
                    earnings_fresh=freshness["fresh"],
                    portfolio_available=bool(portfolios),
                )
                for item in planned:
                    cur.execute(
                        """INSERT INTO notifications.outbox(user_id,kind,event_date,event_key,title,body,data,scheduled_at,expires_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(user_id,kind,event_date,event_key) DO UPDATE SET
                        title=excluded.title,body=excluded.body,data=excluded.data,
                        scheduled_at=excluded.scheduled_at,expires_at=excluded.expires_at
                        WHERE NOT EXISTS(SELECT 1 FROM notifications.deliveries d
                          WHERE d.notification_id=outbox.id AND d.attempted_at IS NOT NULL) RETURNING id""",
                        (
                            prefs["user_id"],
                            item.kind,
                            item.data["event_date"],
                            item.event_key,
                            item.title,
                            item.body,
                            Jsonb(item.data),
                            item.scheduled_at,
                            item.expires_at,
                        ),
                    )
                    count += bool(cur.fetchone())
                # Revalidate unsent content when holdings, filters or calendar data change.
                keys = [
                    f"{item.kind}:{item.data['event_date']}:{item.event_key}"
                    for item in planned
                ]
                cur.execute(
                    """UPDATE notifications.deliveries d SET status='cancelled',error_code='SOURCE_NO_LONGER_ELIGIBLE'
                    FROM notifications.outbox o WHERE d.notification_id=o.id AND o.user_id=%s
                    AND d.status='pending' AND o.kind<>'analysis_ready'
                    AND NOT ((o.kind||':'||o.event_date::text||':'||o.event_key)=ANY(%s::text[]))""",
                    (prefs["user_id"], keys),
                )
                # Bind only devices present during the notice window; registration
                # must not replay historical pushes on newly installed devices.
                cur.execute(
                    """INSERT INTO notifications.deliveries(notification_id,device_id,next_at)
                    SELECT o.id,d.id,greatest(o.scheduled_at,%s) FROM notifications.outbox o JOIN notifications.devices d ON d.user_id=o.user_id
                    WHERE o.user_id=%s AND o.expires_at>%s AND d.active AND d.project_id=%s AND d.environment=%s
                    AND d.updated_at<=o.created_at
                    AND ((o.kind||':'||o.event_date::text||':'||o.event_key)=ANY(%s::text[]))
                    ON CONFLICT(notification_id,device_id) DO UPDATE SET status='pending',error_code=NULL,next_at=excluded.next_at
                    WHERE deliveries.status='cancelled' AND deliveries.attempted_at IS NULL
                    AND deliveries.error_code IN ('SOURCE_NO_LONGER_ELIGIBLE','EXPIRED_OR_DISABLED')""",
                    (
                        now,
                        prefs["user_id"],
                        now,
                        self.project_id,
                        self.environment,
                        keys,
                    ),
                )
        return count

    def claim(self, now, receipts=False):
        token = uuid4()
        with self.transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(710192)")
            # Ambiguous send after a crash is not blindly retried.
            cur.execute(
                "UPDATE notifications.deliveries SET status='unknown',error_code='SEND_OUTCOME_UNKNOWN' WHERE status='sending' AND lease_until<=%s",
                (now,),
            )
            cur.execute(
                """UPDATE notifications.deliveries d SET status='cancelled',error_code='EXPIRED_OR_DISABLED'
                FROM notifications.outbox o, notifications.devices v, notifications.preferences p
                WHERE d.notification_id=o.id AND d.device_id=v.id AND p.user_id=o.user_id AND d.status='pending'
                AND (o.expires_at<=%s OR NOT v.active OR NOT notifications.kind_enabled(o.user_id,o.kind))""",
                (now,),
            )
            if receipts:
                cur.execute(
                    """SELECT d.*,v.token,v.updated_at,o.data,o.title,o.body,o.expires_at,o.user_id,o.kind FROM notifications.deliveries d
                    JOIN notifications.devices v ON v.id=d.device_id JOIN notifications.outbox o ON o.id=d.notification_id
                    WHERE d.status='ticket' AND d.next_at<=%s AND (d.lease_until IS NULL OR d.lease_until<=%s)
                    AND v.project_id=%s AND v.environment=%s ORDER BY d.next_at LIMIT 1 FOR UPDATE OF d SKIP LOCKED""",
                    (now, now, self.project_id, self.environment),
                )
            else:
                cur.execute(
                    """SELECT d.*,v.token,v.updated_at,o.data,o.title,o.body,o.expires_at,o.user_id,o.kind FROM notifications.deliveries d
                    JOIN notifications.devices v ON v.id=d.device_id JOIN notifications.outbox o ON o.id=d.notification_id
                    JOIN notifications.preferences p ON p.user_id=o.user_id
                    WHERE d.status='pending' AND d.next_at<=%s AND o.scheduled_at<=%s AND o.expires_at>%s
                    AND v.active AND v.project_id=%s AND v.environment=%s
                    AND notifications.kind_enabled(o.user_id,o.kind)
                    AND notifications.notice_eligible(o.id,%s)
                    AND (o.kind<>'daily_portfolio' OR EXISTS(SELECT 1 FROM public.portfolio_snapshots s
                      JOIN public.portfolios pf ON pf.id=s.portfolio_id
                      WHERE s.id::text=o.data->>'snapshot_id' AND pf.user_id=o.user_id AND s.valuation_status='success'))
                    AND (o.kind='economic_reminder' OR NOT EXISTS(SELECT 1 FROM notifications.deliveries d2 JOIN notifications.outbox o2 ON o2.id=d2.notification_id
                      WHERE o2.user_id=o.user_id AND o2.id<>o.id AND d2.attempted_at>%s
                      AND o2.kind=o.kind))
                    ORDER BY o.scheduled_at,o.created_at,d.id LIMIT 1 FOR UPDATE OF d SKIP LOCKED""",
                    (
                        now,
                        now,
                        now,
                        self.project_id,
                        self.environment,
                        now,
                        now - timedelta(minutes=15),
                    ),
                )
            row = cur.fetchone()
            if not row:
                return None
            if not receipts and row["kind"] != "economic_reminder":
                cur.execute(
                    """UPDATE notifications.deliveries d SET next_at=greatest(d.next_at,%s)
                    FROM notifications.outbox o WHERE d.notification_id=o.id AND d.status='pending'
                    AND o.user_id=%s AND o.id<>%s
                    AND (CASE WHEN %s IN ('earnings_morning','macro_morning')
                      THEN o.kind IN ('earnings_morning','macro_morning') ELSE o.kind=%s END)""",
                    (
                        now + timedelta(minutes=15),
                        row["user_id"],
                        row["notification_id"],
                        row["kind"],
                        row["kind"],
                    ),
                )
            cur.execute(
                """UPDATE notifications.deliveries SET lease_token=%s,lease_until=%s,
                status=CASE WHEN %s THEN status ELSE 'sending' END,
                attempted_at=CASE WHEN %s THEN attempted_at ELSE %s END,
                device_version=CASE WHEN %s THEN device_version ELSE %s END,
                attempts=attempts+CASE WHEN %s THEN 0 ELSE 1 END WHERE id=%s""",
                (
                    token,
                    now + timedelta(minutes=2),
                    receipts,
                    receipts,
                    now,
                    receipts,
                    row["updated_at"],
                    receipts,
                    row["id"],
                ),
            )
            return {
                **row,
                "lease_token": token,
                "device_version": row["device_version"]
                if receipts
                else row["updated_at"],
                "attempts": row["attempts"] if receipts else row["attempts"] + 1,
            }

    def finish(self, item, now, status, code=None, ticket=None, disable=False):
        with self.transaction() as cur:
            cur.execute(
                """UPDATE notifications.deliveries SET status=%s,error_code=%s,ticket_id=coalesce(%s,ticket_id),
                next_at=%s,lease_until=NULL,lease_token=NULL WHERE id=%s AND lease_token=%s AND lease_until>%s RETURNING id""",
                (
                    status,
                    code,
                    ticket,
                    now
                    + timedelta(
                        minutes=15
                        if status == "ticket"
                        else min(60, 2 ** min(item["attempts"] + 1, 6))
                    ),
                    item["id"],
                    item["lease_token"],
                    now,
                ),
            )
            if cur.fetchone() and disable:
                cur.execute(
                    "UPDATE notifications.devices SET active=false WHERE id=%s AND updated_at=%s",
                    (item["device_id"], item["device_version"]),
                )
