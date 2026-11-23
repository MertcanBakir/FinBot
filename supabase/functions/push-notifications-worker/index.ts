import {
  localDateInTimeZone,
  planNotifications,
  shiftCalendarDate,
} from "./planner.ts";

const MAX_ACTIONS_PER_INVOCATION = 6;
const REQUEST_TIMEOUT_MS = 5_000;
const FUNCTION_BUDGET_MS = 115_000;

type Environment = { get(name: string): string | undefined };
type WorkerItem = Record<string, any>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function constantTimeEqual(left: string, right: string): boolean {
  const encoder = new TextEncoder();
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index] ?? 0) ^ (b[index] ?? 0);
  }
  return difference === 0;
}

function timeoutSignal(duration: number): {
  signal: AbortSignal;
  clear: () => void;
} {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.max(1, duration));
  return { signal: controller.signal, clear: () => clearTimeout(timer) };
}

function responseFailure(status: number): string {
  return status === 429 || status >= 500 ? "retry" : "permanent";
}

async function boundedFetch(
  fetchImpl: typeof fetch,
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const timeout = timeoutSignal(timeoutMs);
  try {
    return await fetchImpl(url, { ...init, signal: timeout.signal });
  } finally {
    timeout.clear();
  }
}

function makeRpc(
  baseUrl: string,
  serviceKey: string,
  fetchImpl: typeof fetch,
  getRemainingMs: () => number,
) {
  return async (name: string, args: Record<string, unknown>) => {
    const timeoutMs = Math.min(REQUEST_TIMEOUT_MS, getRemainingMs());
    if (timeoutMs <= 0) throw new Error("push_worker_budget_exhausted");
    let response: Response;
    try {
      response = await boundedFetch(
        fetchImpl,
        baseUrl.replace(/\/$/, "") + "/rest/v1/rpc/" + name,
        {
          method: "POST",
          headers: {
            apikey: serviceKey,
            Authorization: "Bearer " + serviceKey,
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify(args),
        },
        timeoutMs,
      );
    } catch {
      throw new Error("push_worker_rpc_unavailable");
    }
    if (!response.ok) {
      let diagnostic = name + ":" + response.status + ":unexpected";
      try {
        const body = await response.json();
        const code = typeof body?.code === "string" &&
            /^[0-9A-Z]{5}$/.test(body.code)
          ? body.code
          : "";
        if (
          code === "42501" &&
          body?.message === "notification_worker_forbidden"
        ) diagnostic = name + ":" + response.status + ":" + code + ":claim_guard";
        else if (
          code === "42501" &&
          typeof body?.message === "string" &&
          body.message.startsWith("permission denied for function")
        ) diagnostic = name + ":" + response.status + ":" + code + ":execute_privilege";
        else if (code) diagnostic = name + ":" + response.status + ":" + code;
      } catch {
        // Keep response bodies out of logs.
      }
      throw new Error("push_worker_rpc_failed:" + diagnostic);
    }
    try {
      return await response.json();
    } catch (error) {
      throw new Error("push_worker_rpc_invalid_response:" + name);
    }
  };
}

async function expoRequest(
  fetchImpl: typeof fetch,
  route: "send" | "getReceipts",
  payload: unknown,
  accessToken: string | undefined,
  getRemainingMs: () => number,
): Promise<Record<string, any>> {
  const timeoutMs = Math.min(REQUEST_TIMEOUT_MS, getRemainingMs());
  if (timeoutMs <= 0) return { failure: "unknown" };
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (accessToken) headers.Authorization = "Bearer " + accessToken;
  try {
    const response = await boundedFetch(
      fetchImpl,
      "https://exp.host/--/api/v2/push/" + route,
      {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      },
      timeoutMs,
    );
    if (!response.ok) return { failure: responseFailure(response.status) };
    const body = await response.json();
    return body && typeof body === "object"
      ? body as Record<string, any>
      : { failure: "unknown" };
  } catch {
    return { failure: "unknown" };
  }
}

async function sendOrCheckReceipt(
  item: WorkerItem,
  receipts: boolean,
  fetchImpl: typeof fetch,
  accessToken: string | undefined,
  getRemainingMs: () => number,
): Promise<Record<string, any>> {
  if (receipts) {
    return await expoRequest(
      fetchImpl,
      "getReceipts",
      { ids: [item.ticket_id] },
      accessToken,
      getRemainingMs,
    );
  }
  const payload = {
    to: item.token,
    title: item.title,
    body: item.body,
    data: { ...item.data, notification_id: String(item.notification_id) },
    sound: "default",
    expiration: Math.floor(new Date(item.expires_at).getTime() / 1000),
  };
  if (new TextEncoder().encode(JSON.stringify(payload)).length > 3_800) {
    return { failure: "permanent" };
  }
  return await expoRequest(
    fetchImpl,
    "send",
    payload,
    accessToken,
    getRemainingMs,
  );
}

async function processOne(
  rpc: (name: string, args: Record<string, unknown>) => Promise<any>,
  fetchImpl: typeof fetch,
  accessToken: string | undefined,
  now: Date,
  projectId: string,
  environment: string,
  receipts: boolean,
  getRemainingMs: () => number,
): Promise<string | null> {
  const item = await rpc("notification_worker_claim", {
    p_now: now.toISOString(),
    p_project_id: projectId,
    p_environment: environment,
    p_receipts: receipts,
  }) as WorkerItem | null;
  if (!item) return null;

  const response = await sendOrCheckReceipt(
    item,
    receipts,
    fetchImpl,
    accessToken,
    getRemainingMs,
  );
  const failure = response.failure;
  let status: string;
  let errorCode: string | null = null;
  let ticketId: string | null = null;
  let disableDevice = false;

  if (failure) {
    status = receipts
      ? "ticket"
      : failure === "retry" && item.attempts < 4
      ? "pending"
      : failure === "unknown"
      ? "unknown"
      : "failed";
    if (receipts && now.getTime() - new Date(item.attempted_at).getTime() >
      23 * 60 * 60 * 1000) status = "unknown";
    errorCode = "EXPO_" + String(failure).toUpperCase();
  } else {
    const data = response.data && typeof response.data === "object"
      ? response.data
      : {};
    const result = receipts ? data[item.ticket_id] : data;
    if (!result || typeof result !== "object") {
      status = !receipts ||
          now.getTime() - new Date(item.attempted_at).getTime() >
            23 * 60 * 60 * 1000
        ? "unknown"
        : "ticket";
      errorCode = receipts ? "RECEIPT_MISSING" : "INVALID_TICKET";
    } else if (result.status === "ok") {
      if (!receipts && typeof result.id !== "string") {
        status = "unknown";
        errorCode = "INVALID_TICKET";
      } else {
        status = receipts ? "accepted" : "ticket";
        ticketId = typeof result.id === "string" ? result.id : null;
      }
    } else {
      const candidate = result.details?.error;
      const allowed = new Set([
        "DeviceNotRegistered",
        "MessageTooBig",
        "MessageRateExceeded",
        "InvalidCredentials",
        "MismatchSenderId",
      ]);
      const providerError = typeof candidate === "string" && allowed.has(candidate)
        ? candidate
        : "EXPO_REJECTED";
      status = !receipts && providerError === "MessageRateExceeded" &&
          item.attempts < 4
        ? "pending"
        : "failed";
      errorCode = providerError;
      disableDevice = providerError === "DeviceNotRegistered";
    }
  }

  const finished = await rpc("notification_worker_finish", {
    p_delivery_id: item.id,
    p_lease_token: item.lease_token,
    p_now: now.toISOString(),
    p_status: status,
    p_error_code: errorCode,
    p_ticket_id: ticketId,
    p_device_id: item.device_id,
    p_device_version: item.device_version,
    p_disable_device: disableDevice,
    p_attempts: item.attempts,
  });
  if (finished !== true) throw new Error("push_worker_lease_lost");
  return status;
}

export function createHandler(options: {
  env: Environment;
  fetchImpl?: typeof fetch;
  now?: () => Date;
}) {
  const fetchImpl = options.fetchImpl ?? fetch;
  const now = options.now ?? (() => new Date());

  return async (request: Request): Promise<Response> => {
    if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
    const expectedSecret = options.env.get("PUSH_WORKER_SECRET") ?? "";
    const suppliedSecret = request.headers.get("x-worker-secret") ?? "";
    if (
      expectedSecret.length < 32 ||
      !constantTimeEqual(expectedSecret, suppliedSecret)
    ) return json({ error: "unauthorized" }, 401);

    const baseUrl = options.env.get("SUPABASE_URL") ?? "";
    const serviceKey = options.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
    const projectId = options.env.get("EXPO_PROJECT_ID") ?? "";
    const environment = options.env.get("PUSH_ENVIRONMENT") ?? "production";
    const accessToken = options.env.get("EXPO_ACCESS_TOKEN");
    if (
      !baseUrl.startsWith("https://") ||
      !serviceKey ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(projectId) ||
      !["development", "production"].includes(environment)
    ) return json({ error: "worker_not_configured" }, 503);

    const startedAt = Date.now();
    const getRemainingMs = () =>
      Math.max(0, FUNCTION_BUDGET_MS - (Date.now() - startedAt));
    const rpc = makeRpc(baseUrl, serviceKey, fetchImpl, getRemainingMs);
    let runId: string | null = null;
    const counts: Record<string, number> = { processed_count: 0 };
    try {
      const currentTime = now();
      const prepared = await rpc("notification_worker_prepare", {
        p_now: currentTime.toISOString(),
        p_project_id: projectId,
        p_environment: environment,
      });
      let plannedCount = 0;
      for (const user of prepared?.users ?? []) {
        const preferences = user.preferences;
        const today = localDateInTimeZone(currentTime, preferences.timezone);
        const events = user.earnings ?? [];
        const todayEvents = events.filter((event: WorkerItem) =>
          event.earnings_date === today
        );
        const notices = await planNotifications({
          now: currentTime,
          preferences,
          earnings: todayEvents.filter((event: WorkerItem) => event.held),
          allEarnings: todayEvents,
          watchlist: todayEvents.filter((event: WorkerItem) => event.watched),
          tomorrow: events.filter((event: WorkerItem) =>
            event.earnings_date === shiftCalendarDate(today, 1)
          ),
          macro: user.macro ?? [],
          snapshot: user.snapshot ?? null,
          earningsFresh: user.earnings_fresh === true,
          portfolioAvailable: (user.portfolios ?? []).length > 0,
        });
        plannedCount += Number(await rpc("notification_worker_persist", {
          p_user_id: user.user_id,
          p_now: currentTime.toISOString(),
          p_project_id: projectId,
          p_environment: environment,
          p_items: notices,
        }) ?? 0);
      }
      const due = await rpc("notification_worker_due", {
        p_now: currentTime.toISOString(),
        p_project_id: projectId,
        p_environment: environment,
      });
      if (!due?.send && !due?.receipts) {
        return json({ status: "idle", processed_count: 0 });
      }

      runId = await rpc("notification_worker_start_tracking", {
        p_started_at: currentTime.toISOString(),
        p_prepared_count: plannedCount,
      }) as string;
      let remaining = MAX_ACTIONS_PER_INVOCATION;
      for (const receipts of [true, false]) {
        if (!due[receipts ? "receipts" : "send"] || remaining <= 0) continue;
        const bothKindsDue = Boolean(due.receipts && due.send);
        const quota = bothKindsDue
          ? Math.min(3, remaining)
          : remaining;
        for (let index = 0; index < quota; index += 1) {
          if (getRemainingMs() < 10_000) break;
          const itemNow = now();
          const status = await processOne(
            rpc,
            fetchImpl,
            accessToken,
            itemNow,
            projectId,
            environment,
            receipts,
            getRemainingMs,
          );
          if (!status) break;
          counts.processed_count += 1;
          counts[status] = (counts[status] ?? 0) + 1;
          remaining -= 1;
        }
      }
      const partial = ["failed", "unknown", "pending"].some((key) =>
        (counts[key] ?? 0) > 0
      );
      const finalized = await rpc("notification_worker_finish_tracking", {
        p_run_id: runId,
        p_finished_at: now().toISOString(),
        p_status: partial ? "partial" : "success",
        p_metrics: counts,
        p_error: null,
      });
      if (finalized !== true) throw new Error("push_worker_tracking_finalize_failed");
      return json({
        status: partial ? "partial" : "success",
        processed_count: counts.processed_count,
        ticket_count: counts.ticket ?? 0,
        accepted_count: counts.accepted ?? 0,
        failed_count: counts.failed ?? 0,
        unknown_count: counts.unknown ?? 0,
      });
    } catch (error) {
      if (runId) {
        try {
          await rpc("notification_worker_finish_tracking", {
            p_run_id: runId,
            p_finished_at: now().toISOString(),
            p_status: "failed",
            p_metrics: counts,
            p_error: "push_worker_cycle_failed",
          });
        } catch {
          // Keep secrets and provider responses out of platform logs.
        }
      }
      const diagnostic = error instanceof Error &&
          /^push_worker_rpc_failed:[a-z0-9_:]+$/.test(error.message)
        ? error.message
        : "push_worker_cycle_failed";
      console.error("push_worker_cycle_failed", diagnostic);
      return json({ error: "push_worker_cycle_failed" }, 500);
    }
  };
}

if (import.meta.main) {
  Deno.serve(createHandler({ env: Deno.env }));
}
