# Notification categories implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline. Do not deploy.

**Goal:** Deliver the approved seven-category backend and mobile settings contract.
**Architecture:** Keep the existing private outbox, Expo adapter and worker.
Expand preference RPCs, deterministic planning, source reads and claim-time guards.
**Tech stack:** Python 3.11+, psycopg, PostgreSQL/Supabase, Expo.
**Spec:** ../specs/2026-11-22-notification-categories-design.md

## Global constraints

- No live writes, sends, service changes, commits or pushes.
- Preserve unrelated dirty work and existing financial/reset behavior.
- No user IDs or push tokens exposed in mobile preferences.
- Prepare is idempotent. Claim revalidates preferences, dates and ownership.
- Old preferences callers remain supported.

## Review focus

- Fresh empty calendars versus failed refreshes: never falsely announce no events.
- Cross-midnight economic events, rescheduling and expired slots: no backlog burst.
- Portfolio/watchlist overlap and changes after preparation: no duplicate or stale private content.
- Legacy toggles and already pending deliveries: no preference bypass.
- Concurrent workers, refreshed device tokens and receipts: existing fencing stays intact.

## Task 1: Preference and scheduling contracts

- [x] RED: tests for empty morning digests, seven toggles, watchlist exclusion,
  tomorrow date, P/L privacy and three timed economic slots.
- [x] GREEN: update app/services/push_notifications.py; retain five positional
  arguments and add keyword-only watchlist/all_earnings/tomorrow/source flags.
- [x] Real PostgreSQL acceptance tests for v2 RPC auth, values, legacy alias,
  scope ownership and new finite notification kinds.
- [x] Add forward-only 20261122082548_notification_categories_v2.sql.
- [x] Verify: pytest tests/test_push_notifications.py and local SQL tests.

## Task 2: Repository end-to-end delivery

- [x] Real PostgreSQL tests for watchlist reads, catalog digests, source
  freshness, economic date filtering, claim-time cancellation and slot isolation.
- [x] GREEN: extend PushRepository.prepare, add private claim eligibility checks
  and spacing groups; preserve Expo state machine.
- [x] Verify: RUN_PUSH_POSTGRES=1 pytest --allow-hosts=127.0.0.1
  tests/test_push_notifications_postgres.py.
- [x] Update docs/push-notifications.md with exact RPCs, copy, routing and rollout.
- [x] Final: full Python suite with coverage, Ruff, diff and migration uniqueness.
- [x] Fresh-context review of changed files; address important findings with tests.

## Execution notes

- Existing feature branch push-notifications is the workspace. It already contains
  the live first-version worker as uncommitted work; no destructive branch switch.
- No per-task commits: user asked for local development, not publication.
- Initial five planner acceptance tests failed on missing categories/privacy;
  they passed after implementation. Database tests cover actual PostgreSQL 17.
- Review found four edge cases: archived portfolio empty digest, daily scope
  changes, digest membership beyond 20 routing IDs, and cross-midnight copy.
  All were fixed with regression tests. Reviewer verified the fixes read-only.
- Scope ruling: combine only earnings membership across owned unarchived
  portfolios; monetary daily summaries remain single-portfolio-only. This avoids
  losing independent general/watchlist reminders for multi-portfolio users.

## Final verification

- Default full suite: 965 passed, 30 opt-in PostgreSQL cases skipped, one existing
  Starlette deprecation warning. Branch coverage 80.05%, above the unchanged 80% gate.
- Separate real PostgreSQL 17 run plus notification unit tests: 46 passed
  (21 PostgreSQL cases, 25 unit cases). Disposable loopback cluster only.
- Ruff passed on the five changed Python files; git diff --check clean.
- All 28 migration timestamp prefixes are unique. New migration was applied
  only to the disposable local test database, including production-schema upgrade.
- No remote Supabase connection, AWS service change, SMTP/Expo send, commit or push.
- Mobile UI is not in this repository. V2 preference signatures, card mapping,
  routing payloads, expiry policy and safe rollout are documented in
  docs/push-notifications.md.
