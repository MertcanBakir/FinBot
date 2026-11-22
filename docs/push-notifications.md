# Lucrari push notifications

## Scope and schedule

This additive feature runs separately from price jobs and FinBotAgents. No
GitHub workflow or public HTTP server is required. Preparation, sending and Expo
receipt processing are separate functions in one persistent worker process.
The worker checks PostgreSQL every 30 seconds. This is not an exact delivery SLA:
the device may display a push later than the server submits it.

Earnings reminders include positive open positions across the user's unarchived
portfolios, deduplicated by instrument. Monetary daily summaries require exactly
one unarchived portfolio; multiple portfolios are never silently combined.
General and watchlist reminders also work without a portfolio.

| Type | First local submission | Ordering / expiration |
| --- | --- | --- |
| `earnings_morning` | 10:00 | Portfolio earnings digest, including a fresh empty day; expires at 13:00 |
| `earnings_today` | 10:15 | Catalog-wide today's digest, including a fresh empty day; expires at 13:00 |
| `earnings_midday` | 13:30 | One company per push, ordered by symbol/id; 15-minute intervals; expires at 23:59 |
| `watchlist_earnings` | 14:30 | Watchlist digest, excluding held names when `earnings_midday` is enabled; expires at 23:59 |
| `earnings_tomorrow` | 20:00 | Tomorrow's catalog-wide digest, only when nonempty; expires at 23:59 |
| `economic_reminder` | 180, 60, 15 minutes before event | Each slot expires five minutes later, always before event; no backlog |
| `daily_portfolio` | 00:00 Europe/Istanbul | Previous Turkish calendar day; expires at 00:30 Turkey time |

Default timezone is `Europe/Istanbul`. The preference timezone controls morning
and midday clock times and calendar dates. Daily portfolio valuation stays tied
to the existing engine's Turkish calendar day and midnight Europe/Istanbul,
regardless of the display timezone. In particular, 00:00 is midnight, not noon.
Midday spacing is also enforced against **actual attempt times**, so a delayed worker
does not send that backlog in a burst. Economic slots are exempt from spacing.
The same notification
can be sent to all registered devices without adding 15 minutes per device.
This rule is between distinct notifications, not Expo receipt checks.
Notifications whose remaining slots fall outside the day's window are omitted.
No historical replay is made to newly registered devices.

No relevant events means no company/watchlist/economic/tomorrow notification.
Portfolio and general today's digests still send an empty-day message when the
latest completed earnings refresh is successful, its database sync succeeded,
its coverage includes today, and it finished within 36 hours. Failed, partial,
missing or stale source state never becomes a claim that no events exist.
Earnings require an active, non-stale
calendar event seen within 36 hours and a positive open quantity from the existing
position-summary function for portfolio reminders. All-company digests include
active stocks in FinBot's BIST100/NASDAQ100 catalogs, not the entire exchanges.
All earnings notices describe expected announcement dates, not published results.
Macro events require that same source freshness,
matching country, and curated importance. Existing calendar refresh jobs remain
unchanged: if late, preparation can catch up inside the window. It never calls
external calendar providers. This means a calendar outage can suppress pushes.

`notifications.event_importance` contains an explicit product classification:
3 for CPI/GDP/policy decisions/NFP/PCE, 2 for other currently tracked releases.
These are application choices, not importance ratings supplied by providers.
Unknown event codes are excluded until classified. Countries currently support
TR and US; minimum importance is inclusive (1–3).

## Database and authorization

Migrations: `20261120000000_push_notifications.sql` and
`20261122082548_notification_categories_v2.sql`. Earlier migrations are unchanged.
The `notifications` schema is not a Data API schema.

| Table | Purpose | Mobile roles |
| --- | --- | --- |
| `devices` | User, installation, Expo project/environment, token, validity | No direct access |
| `preferences` | Timezone, notification switches, countries, importance | No direct access |
| `event_importance` | Curated macro classification | No direct access |
| `outbox` | Immutable-after-attempt notification identity, payload, schedule and expiry | No direct access |
| `deliveries` | Per-device attempts, lease, Expo ticket/receipt state, safe error code | No direct access |

All tables enable RLS and revoke all privileges from PUBLIC, anon and
authenticated. There are no public-read policies. Mobile RPCs use SECURITY DEFINER,
empty search_path, qualified table references and mandatory `auth.uid()` checks.
Execute is granted only to authenticated, for exact signatures below. Users
cannot supply another user ID. Backend DB credentials must never reach Lucrari.

The worker uses the existing trusted `SUPABASE_DATABASE_URL` connection factory
and explicit transactions. Its current configuration requires the existing
FinBot trusted database login, not the mobile key or the FinBotAgents NOLOGIN
role. A separate least-privilege runtime login should be provisioned during
deployment if those existing broad credentials are not acceptable.

## Lucrari integration contract

1. Ask OS notification permission. Obtain Expo push token using the real EAS
   project ID in a development or production build. Do not confuse Expo project
   UUID with Supabase project reference. Persist a random installation UUID.
2. After loading the Supabase session, call:

```text
register_push_device(
  p_installation_id uuid,
  p_project_id uuid,
  p_environment text,       -- development | production
  p_token text              -- ExponentPushToken[...] or ExpoPushToken[...]
) -> uuid                  -- device_id; retain locally

unregister_push_device(p_device_id uuid) -> void

get_notification_preferences() -> jsonb

set_notification_preferences(  -- legacy client compatibility only
  p_timezone text,
  p_earnings_morning boolean,
  p_macro_morning boolean,
  p_earnings_midday boolean,
  p_daily_portfolio boolean,
  p_countries text[],        -- TR and/or US; empty disables macro matches
  p_min_importance integer  -- 1..3
) -> void

get_notification_preferences_v2() -> jsonb

set_notification_preferences_v2(
  p_timezone text,
  p_earnings_morning boolean,
  p_earnings_midday boolean,
  p_daily_portfolio boolean,
  p_watchlist_earnings boolean,
  p_economic_reminders boolean,
  p_earnings_today boolean,
  p_earnings_tomorrow boolean,
  p_countries text[],
  p_min_importance integer
) -> void
```

Getter creates defaults for the caller if absent. JSON contains exactly the
legacy preference fields above without `user_id`. V2 returns exactly timezone,
seven boolean preferences, countries and min_importance. The mobile settings
screen should put the first four boolean preferences in a Portfolio card and
the last three in a General card. Mobile UI implementation is a separate handoff.
All switches default enabled. The legacy `macro_morning` getter/setter maps to
`economic_reminders`; legacy saves preserve the other new switches. The old
macro digest is no longer planned or sent. Registration is idempotent per
user/installation/project/environment; token refresh reactivates the device.
Re-register on token changes and session restoration. Unregister before logout
or switching accounts. Another user's token is not silently reassigned; failed
logout cleanup can produce `notification_device_conflict` and needs old-session
cleanup or an explicit administrator decision. An anonymous Supabase user still
needs a valid authenticated session; a new UID does not inherit the old settings.

Stable RPC messages: `notification_auth_required`, `notification_invalid_device`,
`notification_device_conflict`, `notification_invalid_preferences`.
No Expo tokens or database details should be logged by the client.

All pushes have data `{schema_version: 1, type, event_date, notification_id}`.
Dates are ISO `YYYY-MM-DD`; IDs are UUID strings, never navigation authorization.

| type | Additional data | Intended destination |
| --- | --- | --- |
| `earnings_morning` | `instrument_ids` (up to 20), `count`, `content_hash` | Today's portfolio earnings list |
| `earnings_today`, `earnings_tomorrow` | `instrument_ids` (up to 20), `count`, `content_hash` | Today's / tomorrow's earnings calendar |
| `watchlist_earnings` | `instrument_ids` (up to 20), `count`, `content_hash` | Own watchlist's earnings |
| `economic_reminder` | `event_id`, `scheduled_at` (ISO timestamp), `minutes_before` (180 / 60 / 15) | Economic event |
| `earnings_midday` | `instrument_id`, `event_id` | Instrument / earnings event |
| `daily_portfolio` | `portfolio_id`, `snapshot_id`, `currency`, `valuation_date` | Own portfolio daily summary |
| `analysis_ready` | Future producer must provide `report_id` | Own analysis report |

Unknown schema/type should open a safe default screen. On tap, refresh the
Supabase session and check current ownership/availability. Never trust payload
IDs alone. Counts may exceed the bounded ID list; load the full list from the
authorized source. `content_hash` is an opaque full-membership fingerprint used
only for send-time validation; clients should ignore it. Titles/bodies contain visible symbols and P&L amounts; lock
screen privacy is the user's OS setting.

`analysis_ready` is a reserved outbox/payload type only. No TradingAgents
producer or new report authorization is implemented. Wiring that producer and
its notification preference is a separate task; it cannot currently send.

## Daily portfolio calculation

At Turkish midnight, preparation reads yesterday's completed chart snapshot.
The production chart scheduler captures current-day snapshots every hour at :55.
The current builder rejects historical dates, so the notification worker never
calls it for yesterday. Existing financial jobs and SQL functions stay unchanged.
Two consecutive same-currency, complete, calculation-version-2 snapshots are
required. Missing/partial/stale valuation produces no monetary push; it is never
replaced with zero or an estimated gain. Preparation can retry until 00:30 when
the necessary completed captures become available. First-day portfolios lacking
a previous snapshot get no daily comparison.

```text
daily P&L = current absolute_pnl_base - previous absolute_pnl_base
```

Production defines absolute_pnl_base as cumulative realized plus unrealized P&L.
It excludes manually entered cash. Cash changes therefore do not become profit.
The former external_cash_flow_base column was removed on 19 November; runtime
queries do not reference it. Historical model fixtures also verify compatibility.
Amounts use Decimal in portfolio base currency, rounded only for display to two
decimal places. The body contains P/L only, never total portfolio value. This reports
snapshot-date valuation, not a guaranteed live 00:00 quote. It depends on price
and FX freshness checks already enforced by the snapshot engine. Rewriting a
snapshot after a push does not resend that day's notification. Investment reset
does not delete preferences/devices; pending daily pushes require the snapshot
to still exist before claiming. No notification table is tied to portfolio reset.

## Delivery semantics and operations

Unique `(user_id, kind, event_date, event_key)` prevents repeated preparation.
Unique `(notification_id, device_id)` prevents duplicate device jobs. Short DB
transactions, an advisory claim lock, row locks and fenced two-minute leases
protect concurrent workers. No HTTP request runs under a DB lock.

Safe HTTP rejection retries are bounded to four send attempts with backoff.
Ambiguous network outcomes or crashes after send enter `unknown`; they are not
blindly resent. Exactly-once display cannot be guaranteed across DB and Expo.
This chooses avoiding duplicate pushes over automatic recovery of an ambiguous
send. Expired or disabled pending sends are cancelled. Unsent content is
revalidated during preparation; already attempted content is not rewritten.

An Expo ticket is stored, then receipts are checked after 15 minutes. Missing
receipts are rechecked for up to 23 hours. `accepted` means handoff accepted by
APNs/FCM, **not** device display or user read. `DeviceNotRegistered` disables
only the registration version used for that send; an old receipt cannot disable
a newly refreshed token. Only finite safe codes are retained, never raw Expo
errors. Payloads have expiration timestamps and a conservative byte-size limit.
See [Expo delivery documentation](https://docs.expo.dev/push-notifications/sending-notifications/).

Non-empty send/receipt batches use `TrackedJobRunner` as
`push_notifications_batch`, operation `notification_dispatch`, scope `PUSH`.
Metrics contain counts only; partial/failed/unknown delivery outcomes are visible
in private tables. Provider child rows are not created. Email alerts are disabled
for this worker to avoid recurring email storms. Preparation failures log a fixed
`push_cycle_failed` marker and retry next loop; they currently have no independent
refresh_runs row. Existing job/email behavior is unchanged.

## Local validation and deployment handoff

```sh
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 venv/bin/pytest -p no:cacheprovider -q
RUN_PUSH_POSTGRES=1 PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 venv/bin/pytest \
  -p no:cacheprovider --allow-hosts=127.0.0.1 -q tests/test_push_notifications_postgres.py
git diff --check
```

SQL tests create/stop a temporary local PostgreSQL cluster. They test notification
migrations against the baseline financial schema, then explicitly remove the
retired snapshot cash-flow column to verify the production read-query contract.
This focused test does not redeploy or retest the separate portfolio/analysis
redesigns, and is not a live scheduler test.
They do not accept a remote DSN. `TEST_PG_BIN` selects PostgreSQL
binaries. Existing global test network/DB guards remain in place.

V2 rollout requires pausing the old worker, applying the new migration, deploying
the new worker image, then resuming. Do not run old and new planners together.
Pending legacy macro digests are cancelled; ticket receipts remain processable.
Preference migration preserves the old macro opt-out. Rolling back requires
keeping the worker paused until an operator resolves v2 preference compatibility;
do not resume an old planner that generates the retired digest.

Deployment requires a separately approved migration push, dedicated ECR image
and Expo credential/device smoke test. `Dockerfile.notifications` copies only
application code and pinned minimal requirements; it excludes `.env` and keys.
Use a separate ECR repository from FinBotAgents so lifecycle retention cannot
delete the other application's image. Record the source SHA and image digest.

`compose.notifications.yml` uses project `finbot-notifications`, no inbound
ports, non-root image, bounded logs/resources and no shared TradingAgents volumes.
It does not deploy or update FinBotAgents. Keep its private environment file
outside Git and populate:

```text
SUPABASE_DATABASE_URL=<FinBot pooler DSN, existing port-6543 contract>
EXPO_PROJECT_ID=<EAS UUID>
PUSH_ENVIRONMENT=production
EXPO_ACCESS_TOKEN=<optional Expo enhanced-security token>
```

Docker Compose 2.30+ is required for raw env-file handling (credentials are not
subject to variable interpolation). Host environment must specify `FINBOT_NOTIFICATIONS_IMAGE` as a dedicated image
digest and `FINBOT_NOTIFICATIONS_ENV_FILE` as the private file's absolute path.
The worker requires explicit `--apply-db` for writes and real sending; image CMD
includes it. `--once --apply-db` is **not a dry run**. No CI/CD or AWS modification
is part of this development. To pause, stop only this Compose service. Existing
outbox data is retained; restart catches up only inside unexpired windows.
Application rollback stops this worker; the additive private tables may remain.
Do not remove populated tables as an automatic rollback or alter earlier SQL.

Before enabling: apply migration, arrange fresh morning calendar/price inputs,
configure real Expo project credentials, integrate RPCs/tap handling in Lucrari,
and test on an actual device. Database tests do not validate APNs/FCM credentials.
