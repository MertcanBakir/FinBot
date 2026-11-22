# Notification categories v2

Approved in chat on 2026-11-22. Backend implementation only; do not deploy, send
test pushes, restart AWS services, commit or push during this task.

Two mobile settings cards, seven independent booleans:

| Card | Preference | Delivery |
| --- | --- | --- |
| Portfolio | earnings_morning | 10:00 portfolio earnings digest, including a fresh empty-day message |
| Portfolio | earnings_midday | From 13:30, one held company per notification, 15 minutes apart |
| Portfolio | daily_portfolio | 00:00 Turkey, yesterday's P/L only; never total portfolio value |
| Portfolio | watchlist_earnings | 14:30 watchlist digest; suppress held names when their portfolio reminder is enabled |
| General | economic_reminders | 180, 60 and 15 minutes before an eligible timed event |
| General | earnings_today | 10:15 catalog-wide today's digest, including a fresh empty-day message |
| General | earnings_tomorrow | 20:00 catalog-wide tomorrow's digest; no message when empty |

Calendar notices describe expected announcement dates, not published financial
results. All companies means active stock instruments in FinBot's BIST100 and
NASDAQ100 groups, not the entire exchange. Reuse private queue and existing Expo
delivery state machine, RLS isolation, lease fencing and bounded retries.

No event does not mean unavailable data: empty-day digests require a successful
recent earnings refresh covering the requested day. Positive candidates must also
be active, not stale, and observed within 36 hours. Lack of a portfolio suppresses
portfolio digests, but does not suppress general or watchlist notices.

Compatibility: retain the original preferences RPC signature and macro_morning
alias, add v2 getter/setter. New switches default enabled, matching existing
defaults. Old macro preference changes must affect the replacement reminders.
Pending legacy macro summaries are no longer sent by the v2 worker; receipts
remain readable. Rollout must pause the old worker before migrating preferences
and starting the new image; never run two different planner versions together.

Operator choices: economic slots expire after five minutes (and always before
the event), so outages do not replay old reminders. Event identity includes the
event time and offset. No 15-minute earnings spacing applies to economic slots.
Daily P/L still requires complete consecutive snapshots and excludes cash edits.
Earnings holdings are deduplicated across unarchived portfolios; daily monetary
summaries require one portfolio and are suppressed for multiple portfolios.
The mobile app must use v2 preferences and implement payload routing/foreground
display; no mobile repository is in this workspace.
