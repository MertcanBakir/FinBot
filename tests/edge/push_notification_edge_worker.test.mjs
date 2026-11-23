import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createHandler,
} from "../../supabase/functions/push-notifications-worker/index.ts";
import {
  localDateInTimeZone,
  localWallClockToUtc,
  planNotifications,
} from "../../supabase/functions/push-notifications-worker/planner.ts";

const workerSecret = "test-only-secret-value-that-is-long-enough";
const projectId = "123e4567-e89b-42d3-a456-426614174000";

function environment(overrides = {}) {
  const values = {
    SUPABASE_URL: "https://example.supabase.co",
    SUPABASE_SERVICE_ROLE_KEY: "service-role-test-key",
    PUSH_WORKER_SECRET: workerSecret,
    EXPO_PROJECT_ID: projectId,
    PUSH_ENVIRONMENT: "production",
    ...overrides,
  };
  return { get: (name) => values[name] };
}

function request() {
  return new Request("https://example.supabase.co/functions/v1/push-notifications-worker", {
    method: "POST",
    headers: { "x-worker-secret": workerSecret },
    body: "{}",
  });
}

function rpcResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function prefs(changes = {}) {
  return {
    timezone: "Europe/Istanbul",
    earnings_morning: true,
    macro_morning: true,
    earnings_midday: true,
    daily_portfolio: true,
    watchlist_earnings: true,
    economic_reminders: true,
    earnings_today: true,
    earnings_tomorrow: true,
    countries: ["TR", "US"],
    min_importance: 1,
    ...changes,
  };
}

function event(symbol = "AAA", changes = {}) {
  return {
    id: "event-1",
    instrument_id: "instrument-1",
    symbol,
    earnings_date: "2026-11-20",
    watched: false,
    held: true,
    ...changes,
  };
}

test("timezone helpers preserve local calendar and wall-clock time", () => {
  const now = new Date("2026-11-20T07:00:00.000Z");
  assert.equal(localDateInTimeZone(now, "Europe/Istanbul"), "2026-11-20");
  assert.equal(
    localWallClockToUtc("2026-11-20", "Europe/Istanbul", 10, 0).toISOString(),
    "2026-11-20T07:00:00.000Z",
  );
});

test("planner keeps daily portfolio notice limited to profit and loss", async () => {
  const now = new Date("2026-11-19T21:00:00.000Z");
  const snapshot = {
    id: "snapshot-1",
    portfolio_id: "portfolio-1",
    snapshot_date: "2026-11-19",
    previous_date: "2026-11-18",
    valuation_status: "success",
    previous_status: "success",
    calculation_version: 2,
    previous_version: 2,
    absolute_pnl_base: "125.00",
    previous_absolute_pnl: "0.00",
    base_currency: "TRY",
  };
  const notices = await planNotifications({
    now,
    preferences: prefs(),
    earnings: [],
    allEarnings: [],
    watchlist: [],
    tomorrow: [],
    macro: [],
    snapshot,
    earningsFresh: false,
    portfolioAvailable: true,
  });
  const daily = notices.find((notice) => notice.kind === "daily_portfolio");
  assert.ok(daily);
  assert.match(daily.body, /\+125\.00 TRY/);
  assert.doesNotMatch(daily.body, /toplam|değer/i);
});

test("planner emits fresh empty-day summaries and avoids unsourced empty claims", async () => {
  const now = new Date("2026-11-20T07:15:00.000Z");
  const ready = await planNotifications({
    now,
    preferences: prefs(),
    earnings: [],
    allEarnings: [],
    watchlist: [],
    tomorrow: [],
    macro: [],
    snapshot: null,
    earningsFresh: true,
    portfolioAvailable: true,
  });
  assert.deepEqual(
    ready.map((notice) => notice.kind).sort(),
    ["earnings_morning", "earnings_today"],
  );
  const stale = await planNotifications({
    now,
    preferences: prefs(),
    earnings: [],
    allEarnings: [],
    watchlist: [],
    tomorrow: [],
    macro: [],
    snapshot: null,
    earningsFresh: false,
    portfolioAvailable: true,
  });
  assert.deepEqual(stale, []);
});

test("planner creates event reminders only inside each three-hour one-hour and fifteen-minute window", async () => {
  const now = new Date("2026-11-20T07:00:00.000Z");
  const scheduledAt = new Date(now.getTime() + 3 * 60 * 60 * 1000);
  const notices = await planNotifications({
    now,
    preferences: prefs(),
    earnings: [],
    allEarnings: [],
    watchlist: [],
    tomorrow: [],
    macro: [{ id: "macro-1", name: "ABD TÜFE", scheduled_at: scheduledAt }],
    snapshot: null,
    earningsFresh: false,
    portfolioAvailable: true,
  });
  assert.equal(notices.length, 1);
  assert.equal(notices[0].kind, "economic_reminder");
  assert.equal(notices[0].data.minutes_before, 180);
  assert.equal(new Date(notices[0].expires_at).getTime() - now.getTime(), 5 * 60 * 1000);
});

test("unauthorized requests fail before calling Supabase", async () => {
  let calls = 0;
  const handler = createHandler({
    env: environment(),
    fetchImpl: async () => {
      calls += 1;
      return rpcResponse({});
    },
    now: () => new Date("2026-11-20T07:00:00.000Z"),
  });
  const response = await handler(new Request("https://worker.test", { method: "POST" }));
  assert.equal(response.status, 401);
  assert.equal(calls, 0);
});

test("worker rejects malformed Expo project ID before calling Supabase", async () => {
  let calls = 0;
  const handler = createHandler({
    env: environment({ EXPO_PROJECT_ID: "----not-a-project-id-----------" }),
    fetchImpl: async () => {
      calls += 1;
      return rpcResponse({});
    },
  });
  const response = await handler(request());
  assert.equal(response.status, 503);
  assert.equal(calls, 0);
});

test("idle Cron invocation prepares queue but creates no refresh run", async () => {
  const calls = [];
  const handler = createHandler({
    env: environment(),
    fetchImpl: async (url) => {
      const name = new URL(url).pathname.split("/").at(-1);
      calls.push(name);
      return rpcResponse(name === "notification_worker_prepare"
        ? { user_count: 0, planned_count: 0 }
        : { send: false, receipts: false });
    },
    now: () => new Date("2026-11-20T07:00:00.000Z"),
  });
  const response = await handler(request());
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: "idle", processed_count: 0 });
  assert.deepEqual(calls, ["notification_worker_prepare", "notification_worker_due"]);
});

test("due send uses one lease and records Expo ticket without exposing secrets", async () => {
  const calls = [];
  let claimed = false;
  const handler = createHandler({
    env: environment(),
    fetchImpl: async (url, init) => {
      const parsed = new URL(url);
      const name = parsed.pathname.split("/").at(-1);
      calls.push({ name, body: init?.body ? JSON.parse(init.body) : null });
      if (parsed.hostname === "exp.host") {
        return rpcResponse({ data: { status: "ok", id: "expo-ticket-1" } });
      }
      if (name === "notification_worker_prepare") return rpcResponse({ user_count: 1, planned_count: 1 });
      if (name === "notification_worker_due") return rpcResponse({ send: true, receipts: false });
      if (name === "notification_worker_start_tracking") return rpcResponse("run-1");
      if (name === "notification_worker_claim") {
        const args = calls.at(-1).body;
        if (args.p_receipts || claimed) return rpcResponse(null);
        claimed = true;
        return rpcResponse({
          id: "delivery-1",
          notification_id: "notice-1",
          device_id: "device-1",
          device_version: "2026-11-20T06:00:00.000Z",
          lease_token: "lease-1",
          attempts: 1,
          ticket_id: null,
          token: "ExponentPushToken[private-device-token]",
          title: "Title",
          body: "Body",
          data: { schema_version: 1 },
          expires_at: "2026-11-20T20:59:00.000Z",
          attempted_at: "2026-11-20T07:00:00.000Z",
        });
      }
      if (name === "notification_worker_finish") return rpcResponse(true);
      if (name === "notification_worker_finish_tracking") return rpcResponse(true);
      throw new Error(`unexpected request ${url}`);
    },
    now: () => new Date("2026-11-20T07:00:00.000Z"),
  });
  const response = await handler(request());
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.status, "success");
  assert.equal(result.ticket_count, 1);
  assert.ok(calls.some((call) => call.name === "notification_worker_finish" && call.body.p_ticket_id === "expo-ticket-1"));
  const responseText = JSON.stringify(result);
  assert.doesNotMatch(responseText, /private-device-token|service-role-test-key/);
});

test("worker fails closed if a claimed delivery lease cannot be finalized", async () => {
  let claimed = false;
  let trackingStatus = null;
  const handler = createHandler({
    env: environment(),
    fetchImpl: async (url, init) => {
      const parsed = new URL(url);
      const name = parsed.pathname.split("/").at(-1);
      if (parsed.hostname === "exp.host") {
        return rpcResponse({ data: { status: "ok", id: "expo-ticket-1" } });
      }
      const body = init?.body ? JSON.parse(init.body) : {};
      if (name === "notification_worker_prepare") return rpcResponse({ users: [] });
      if (name === "notification_worker_due") return rpcResponse({ send: true, receipts: false });
      if (name === "notification_worker_start_tracking") return rpcResponse("run-1");
      if (name === "notification_worker_claim") {
        if (body.p_receipts || claimed) return rpcResponse(null);
        claimed = true;
        return rpcResponse({
          id: "delivery-1",
          notification_id: "notice-1",
          device_id: "device-1",
          device_version: "2026-11-20T06:00:00.000Z",
          lease_token: "lease-1",
          attempts: 1,
          token: "ExponentPushToken[private-device-token]",
          title: "Title",
          body: "Body",
          data: {},
          expires_at: "2026-11-20T20:59:00.000Z",
        });
      }
      if (name === "notification_worker_finish") return rpcResponse(false);
      if (name === "notification_worker_finish_tracking") {
        trackingStatus = body.p_status;
        return rpcResponse(true);
      }
      throw new Error(`unexpected request ${url}`);
    },
    now: () => new Date("2026-11-20T07:00:00.000Z"),
  });
  const response = await handler(request());
  assert.equal(response.status, 500);
  assert.equal(trackingStatus, "failed");
  assert.doesNotMatch(await response.text(), /private-device-token|lease-1/);
});
