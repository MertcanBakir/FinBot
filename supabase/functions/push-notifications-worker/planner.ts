export type NotificationPreferences = {
  timezone: string;
  earnings_morning: boolean;
  earnings_midday: boolean;
  daily_portfolio: boolean;
  watchlist_earnings: boolean;
  economic_reminders?: boolean;
  macro_morning?: boolean;
  earnings_today: boolean;
  earnings_tomorrow: boolean;
};

export type NotificationNotice = {
  kind: string;
  event_key: string;
  title: string;
  body: string;
  data: Record<string, unknown>;
  scheduled_at: string;
  expires_at: string;
};

type EarningsEvent = {
  id: string;
  instrument_id: string;
  symbol: string;
  earnings_date: string;
  watched?: boolean;
  held?: boolean;
};

type MacroEvent = { id: string; name: string; scheduled_at: string | Date };

type Snapshot = {
  id: string;
  portfolio_id: string;
  snapshot_date: string;
  previous_date: string;
  valuation_status: string;
  previous_status: string;
  calculation_version: number;
  previous_version: number;
  absolute_pnl_base: string;
  previous_absolute_pnl: string;
  base_currency: string;
};

const minute = 60_000;

function partsForTimeZone(date: Date, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  return Object.fromEntries(parts.map((part) => [part.type, part.value]));
}

export function localDateInTimeZone(date: Date, timezone: string): string {
  const parts = partsForTimeZone(date, timezone);
  return parts.year + "-" + parts.month + "-" + parts.day;
}

export function localWallClockToUtc(
  day: string,
  timezone: string,
  hour: number,
  minuteOfHour = 0,
): Date {
  const [year, month, date] = day.split("-").map(Number);
  const target = Date.UTC(year, month - 1, date, hour, minuteOfHour);
  let guess = target;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const parts = partsForTimeZone(new Date(guess), timezone);
    const represented = Date.UTC(
      Number(parts.year),
      Number(parts.month) - 1,
      Number(parts.day),
      Number(parts.hour),
      Number(parts.minute),
      Number(parts.second),
    );
    const difference = target - represented;
    if (difference === 0) return new Date(guess);
    guess += difference;
  }
  throw new Error("notification_invalid_local_time");
}

export function shiftCalendarDate(day: string, count: number): string {
  const [year, month, date] = day.split("-").map(Number);
  const value = new Date(Date.UTC(year, month - 1, date + count));
  return value.getUTCFullYear() + "-" +
    String(value.getUTCMonth() + 1).padStart(2, "0") + "-" +
    String(value.getUTCDate()).padStart(2, "0");
}

function uniqueEvents(events: EarningsEvent[]): EarningsEvent[] {
  const byInstrument = new Map<string, EarningsEvent>();
  for (const event of events) byInstrument.set(String(event.instrument_id), event);
  return [...byInstrument.values()].sort((left, right) =>
    left.symbol.localeCompare(right.symbol) ||
    String(left.instrument_id).localeCompare(String(right.instrument_id))
  );
}

async function contentHash(events: EarningsEvent[]): Promise<string> {
  const values = [...new Set(events.map((event) =>
    String(event.instrument_id) + ":" + event.symbol
  ))].sort();
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(values.join(",")),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function scaledDecimal(value: string): { coefficient: bigint; scale: number } {
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (match === null) throw new Error("notification_invalid_snapshot_amount");
  const fraction = match[3] ?? "";
  const coefficient = BigInt(match[2] + fraction) * (match[1] === "-" ? -1n : 1n);
  return { coefficient, scale: fraction.length };
}

function formatDecimalDifference(current: string, previous: string): string {
  const left = scaledDecimal(current);
  const right = scaledDecimal(previous);
  const scale = Math.max(left.scale, right.scale);
  const leftValue = left.coefficient * 10n ** BigInt(scale - left.scale);
  const rightValue = right.coefficient * 10n ** BigInt(scale - right.scale);
  const value = leftValue - rightValue;
  const absolute = value < 0n ? -value : value;
  const divisor = 10n ** BigInt(scale);
  const centsNumerator = absolute * 100n;
  let cents = centsNumerator / divisor;
  const remainder = centsNumerator % divisor;
  if (
    remainder * 2n > divisor ||
    (remainder * 2n === divisor && cents % 2n === 1n)
  ) cents += 1n;
  const whole = cents / 100n;
  const fraction = String(cents % 100n).padStart(2, "0");
  return (value >= 0n ? "+" : "-") + whole + "." + fraction;
}

export async function planNotifications(input: {
  now: Date;
  preferences: NotificationPreferences;
  earnings: EarningsEvent[];
  allEarnings: EarningsEvent[];
  watchlist: EarningsEvent[];
  tomorrow: EarningsEvent[];
  macro: MacroEvent[];
  snapshot: Snapshot | null;
  earningsFresh: boolean;
  portfolioAvailable: boolean;
}): Promise<NotificationNotice[]> {
  const {
    now,
    preferences,
    earnings,
    allEarnings,
    watchlist,
    tomorrow,
    macro,
    snapshot,
    earningsFresh,
    portfolioAvailable,
  } = input;
  const timezone = preferences.timezone;
  const day = localDateInTimeZone(now, timezone);
  const notices: NotificationNotice[] = [];

  const add = (
    kind: string,
    key: string,
    title: string,
    body: string,
    hour: number,
    minuteOfHour = 0,
    slot = 0,
    payload: Record<string, unknown> = {},
    eventDay = day,
  ) => {
    let scheduled = new Date(
      localWallClockToUtc(day, timezone, hour, minuteOfHour).getTime() +
        slot * 15 * minute,
    );
    let expires = localWallClockToUtc(day, timezone, 23, 59);
    if (kind === "earnings_morning" || kind === "earnings_today") {
      expires = localWallClockToUtc(day, timezone, 13, 0);
    }
    if (kind === "daily_portfolio") {
      const turkeyDay = localDateInTimeZone(now, "Europe/Istanbul");
      scheduled = localWallClockToUtc(turkeyDay, "Europe/Istanbul", 0, 0);
      expires = new Date(scheduled.getTime() + 30 * minute);
    }
    if (scheduled <= now && now < expires) {
      notices.push({
        kind,
        event_key: String(key),
        title,
        body,
        data: {
          schema_version: 1,
          type: kind,
          event_date: eventDay,
          ...payload,
        },
        scheduled_at: scheduled.toISOString(),
        expires_at: expires.toISOString(),
      });
    }
  };

  const portfolioEarnings = uniqueEvents(earnings);
  const digest = async (
    kind: "earnings_today" | "watchlist_earnings" | "earnings_tomorrow",
    events: EarningsEvent[],
    title: string,
    emptyTitle: string,
    hour: number,
    minuteOfHour = 0,
    fresh = false,
    eventDay = day,
  ) => {
    const unique = uniqueEvents(events);
    if (preferences[kind] && (unique.length > 0 || fresh)) {
      const hash = await contentHash(unique);
      add(
        kind,
        "daily",
        unique.length ? title : emptyTitle,
        unique.length
          ? unique.map((event) => event.symbol).join(", ").slice(0, 500) +
            " için finansal sonuç açıklaması bekleniyor."
          : "Güncel takvimde planlanmış bilanço açıklaması bulunmuyor.",
        hour,
        minuteOfHour,
        0,
        {
          instrument_ids: unique.slice(0, 20).map((event) =>
            String(event.instrument_id)
          ),
          count: unique.length,
          content_hash: hash,
        },
        eventDay,
      );
    }
  };

  if (
    portfolioAvailable && preferences.earnings_morning &&
    (portfolioEarnings.length > 0 || earningsFresh)
  ) {
    const hash = await contentHash(portfolioEarnings);
    add(
      "earnings_morning",
      "daily",
      portfolioEarnings.length
        ? "Bugünkü portföy bilançoların"
        : "Portföyünün bilanço takvimi sakin",
      portfolioEarnings.length
        ? portfolioEarnings.map((event) => event.symbol).join(", ").slice(0, 500) +
          " için finansal sonuçların bugün açıklanması bekleniyor."
        : "Portföyündeki şirketler için bugün planlanmış bilanço açıklaması bulunmuyor.",
      10,
      0,
      0,
      {
        instrument_ids: portfolioEarnings.slice(0, 20).map((event) =>
          String(event.instrument_id)
        ),
        count: portfolioEarnings.length,
        content_hash: hash,
      },
    );
  }

  await digest(
    "earnings_today",
    allEarnings,
    "Bugünün bilanço takvimi",
    "Bugün planlanmış bilanço yok",
    10,
    15,
    earningsFresh,
  );
  const heldIds = new Set(
    portfolioEarnings.map((event) => String(event.instrument_id)),
  );
  const eligibleWatchlist = watchlist.filter((event) =>
    !(preferences.earnings_midday &&
      heldIds.has(String(event.instrument_id)))
  );
  await digest(
    "watchlist_earnings",
    eligibleWatchlist,
    "Takip listendeki bugünkü bilançolar",
    "",
    14,
    30,
  );
  await digest(
    "earnings_tomorrow",
    tomorrow,
    "Yarının bilanço takvimi",
    "",
    20,
    0,
    false,
    shiftCalendarDate(day, 1),
  );

  if (
    preferences.economic_reminders ?? preferences.macro_morning ?? false
  ) {
    for (const event of macro) {
      const scheduled = event.scheduled_at instanceof Date
        ? new Date(event.scheduled_at.getTime())
        : new Date(event.scheduled_at);
      if (!Number.isFinite(scheduled.getTime())) continue;
      for (const minutesBefore of [180, 60, 15]) {
        const when = new Date(scheduled.getTime() - minutesBefore * minute);
        const expiry = new Date(
          Math.min(when.getTime() + 5 * minute, scheduled.getTime()),
        );
        if (when <= now && now < expiry) {
          const eventDay = localDateInTimeZone(scheduled, timezone);
          const localParts = partsForTimeZone(scheduled, timezone);
          notices.push({
            kind: "economic_reminder",
            event_key: event.id + ":" + scheduled.toISOString() + ":" +
              minutesBefore,
            title: event.name + " verisine " + minutesBefore + " dakika kaldı",
            body: (eventDay === day ? "Bugün" : "Yarın") + " " +
              localParts.hour + ":" + localParts.minute +
              "’da açıklanması bekleniyor.",
            data: {
              schema_version: 1,
              type: "economic_reminder",
              event_date: eventDay,
              event_id: String(event.id),
              scheduled_at: scheduled.toISOString(),
              minutes_before: minutesBefore,
            },
            scheduled_at: when.toISOString(),
            expires_at: expiry.toISOString(),
          });
        }
      }
    }
  }

  if (preferences.earnings_midday) {
    for (const [index, event] of portfolioEarnings.entries()) {
      add(
        "earnings_midday",
        event.instrument_id,
        event.symbol + " bilançosu bugün",
        "Finansal sonuçların bugün açıklanması bekleniyor.",
        13,
        30,
        index,
        {
          instrument_id: String(event.instrument_id),
          event_id: String(event.id),
        },
      );
    }
  }

  if (preferences.daily_portfolio && snapshot) {
    const previousDay = shiftCalendarDate(
      localDateInTimeZone(now, "Europe/Istanbul"),
      -1,
    );
    const priorDay = shiftCalendarDate(previousDay, -1);
    if (
      snapshot.snapshot_date === previousDay &&
      snapshot.previous_date === priorDay &&
      snapshot.valuation_status === "success" &&
      snapshot.previous_status === "success" &&
      snapshot.calculation_version === 2 &&
      snapshot.previous_version === 2
    ) {
      const pnl = formatDecimalDifference(
        snapshot.absolute_pnl_base,
        snapshot.previous_absolute_pnl,
      );
      add(
        "daily_portfolio",
        snapshot.portfolio_id,
        "Günlük portföy özetin hazır",
        "Dünkü kâr/zararın: " + pnl + " " + snapshot.base_currency + ".",
        0,
        0,
        0,
        {
          portfolio_id: String(snapshot.portfolio_id),
          snapshot_id: String(snapshot.id),
          currency: snapshot.base_currency,
          valuation_date: previousDay,
        },
        previousDay,
      );
    }
  }

  return notices;
}
