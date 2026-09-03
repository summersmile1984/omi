import { DurableObject } from "cloudflare:workers";

const WINDOW_STORAGE_KEY = "window";
const RESERVATION_STORAGE_PREFIX = "reservation:";
const TTS_BURST_STORAGE_KEY = "tts-burst";
const TTS_DAILY_STORAGE_KEY = "tts-daily";
const MAX_POLICY_NAME_BYTES = 128;
const MAX_REQUESTS = 1_000_000;
const MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60;

type StoredWindow = {
  count: number;
  resetAt: number;
};

type StoredReservation = {
  resetAt: number;
};

type StoredTtsBurst = {
  timestamps: number[];
};

type StoredTtsDaily = {
  chars: number;
  resetAt: number;
};

type RateLimitCheckRequest = {
  policy?: unknown;
  max_requests?: unknown;
  window_seconds?: unknown;
};

type RateLimitReleaseRequest = {
  reservation_id?: unknown;
};

export type RateLimitResult = {
  allowed: boolean;
  limit: number;
  remaining: number;
  retryAfter: number;
  resetAt: number;
};

export type RateLimitReservationResult = {
  reserved: boolean;
  reservationId: string | null;
  limit: number;
  remaining: number;
  retryAfter: number;
  resetAt: number;
};

export type RateLimitReleaseResult = {
  released: boolean;
  remaining: number;
  resetAt: number | null;
};

function reservationStorageKey(reservationId: string): string {
  return `${RESERVATION_STORAGE_PREFIX}${reservationId}`;
}

function parseReservationId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const bytes = new TextEncoder().encode(value).byteLength;
  return bytes >= 1 && bytes <= 128 && /^[A-Za-z0-9_-]+$/.test(value)
    ? value
    : null;
}

function activeWindow(
  value: StoredWindow | undefined,
  now: number,
): StoredWindow | null {
  if (
    !value ||
    !Number.isSafeInteger(value.count) ||
    value.count < 0 ||
    !Number.isFinite(value.resetAt) ||
    value.resetAt <= now
  ) {
    return null;
  }
  return value;
}

function unavailableRateLimitResponse(): Response {
  return Response.json(
    { error: "rate_limit_unavailable" },
    { status: 503, headers: { "cache-control": "no-store" } },
  );
}

type RateLimitPolicy = {
  name: string;
  maxRequests: number;
  windowSeconds: number;
};

export type TtsFineRateLimitResult = {
  status: 0 | 1 | 2;
  retryAfter: number;
  burstRemaining: number;
  dailyCharsRemaining: number;
  dailyResetAt: number;
};

export const TTS_FINE_RATE_LIMITS = {
  desktop: {
    burstRequests: 20,
    burstWindowSeconds: 60,
    dailyChars: 50_000,
    maxRequestChars: 4_096,
  },
  mobile: {
    burstRequests: 50,
    burstWindowSeconds: 60,
    dailyChars: 10_000,
    maxRequestChars: 5_000,
  },
} as const;
export type TtsFineRateLimitProfile = keyof typeof TTS_FINE_RATE_LIMITS;
type TtsFineRateLimitPolicy =
  (typeof TTS_FINE_RATE_LIMITS)[TtsFineRateLimitProfile];
export const TTS_FINE_RATE_LIMIT = TTS_FINE_RATE_LIMITS.desktop;
const TTS_BURST_RETENTION_WINDOW_SECONDS = Math.max(
  ...Object.values(TTS_FINE_RATE_LIMITS).map(
    (policy) => policy.burstWindowSeconds,
  ),
);

export class SharedRateLimitDurableObject extends DurableObject<
  Record<string, never>
> {
  constructor(
    private readonly state: DurableObjectState,
    env: Record<string, never>,
  ) {
    super(state, env);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET") {
      return Response.json(await this.health());
    }
    if (url.pathname === "/tts/check") {
      if (request.method !== "POST") {
        return Response.json({ error: "method_not_allowed" }, { status: 405 });
      }
      let body: { char_count?: unknown; profile?: unknown };
      try {
        body = (await request.json()) as {
          char_count?: unknown;
          profile?: unknown;
        };
      } catch {
        return Response.json({ error: "invalid_json" }, { status: 400 });
      }
      const profile = ttsFineRateLimitProfile(body.profile);
      if (profile === null) {
        return Response.json(
          { error: "invalid_tts_character_count" },
          { status: 400 },
        );
      }
      const policy = TTS_FINE_RATE_LIMITS[profile];
      if (
        !Number.isInteger(body.char_count) ||
        (body.char_count as number) < 1 ||
        (body.char_count as number) > policy.maxRequestChars
      ) {
        return Response.json(
          { error: "invalid_tts_character_count" },
          { status: 400 },
        );
      }
      return Response.json(
        await this.checkTts(body.char_count as number, profile),
        { headers: { "cache-control": "no-store" } },
      );
    }
    if (url.pathname === "/reserve") {
      return this.handleReserve(request);
    }
    if (url.pathname === "/release") {
      return this.handleRelease(request);
    }
    if (url.pathname !== "/check") {
      return Response.json({ error: "not_found" }, { status: 404 });
    }
    if (request.method !== "POST") {
      return Response.json({ error: "method_not_allowed" }, { status: 405 });
    }

    let body: RateLimitCheckRequest;
    try {
      body = (await request.json()) as RateLimitCheckRequest;
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }
    const policy = parsePolicy(body);
    if (!policy) {
      return Response.json(
        { error: "invalid_rate_limit_policy" },
        { status: 400 },
      );
    }

    const now = Date.now();
    const result = await this.state.storage.transaction(async (transaction) => {
      const stored = await transaction.get<StoredWindow>(WINDOW_STORAGE_KEY);
      const resetAt =
        stored && stored.resetAt > now
          ? stored.resetAt
          : now + policy.windowSeconds * 1000;
      const count = stored && stored.resetAt > now ? stored.count + 1 : 1;
      await transaction.put(WINDOW_STORAGE_KEY, { count, resetAt });
      const allowed = count <= policy.maxRequests;
      return {
        allowed,
        limit: policy.maxRequests,
        remaining: Math.max(0, policy.maxRequests - count),
        retryAfter: allowed
          ? 0
          : Math.max(1, Math.ceil((resetAt - now) / 1000)),
        resetAt,
      } satisfies RateLimitResult;
    });
    await this.state.storage.setAlarm(result.resetAt);
    return Response.json(result, {
      headers: { "cache-control": "no-store" },
    });
  }

  private async handleReserve(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return Response.json({ error: "method_not_allowed" }, { status: 405 });
    }
    let body: RateLimitCheckRequest;
    try {
      body = (await request.json()) as RateLimitCheckRequest;
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }
    const policy = parsePolicy(body);
    if (!policy) {
      return Response.json(
        { error: "invalid_rate_limit_policy" },
        { status: 400 },
      );
    }

    try {
      const result = await this.state.storage.transaction(
        async (transaction) => {
          const now = Date.now();
          const stored = await transaction.get<StoredWindow>(
            WINDOW_STORAGE_KEY,
          );
          const active = activeWindow(stored, now);
          const window =
            active || {
              count: 0,
              resetAt: now + policy.windowSeconds * 1000,
            };
          if (window.count >= policy.maxRequests) {
            await transaction.setAlarm(window.resetAt);
            return {
              reserved: false,
              reservationId: null,
              limit: policy.maxRequests,
              remaining: 0,
              retryAfter: Math.max(
                1,
                Math.ceil((window.resetAt - now) / 1000),
              ),
              resetAt: window.resetAt,
            } satisfies RateLimitReservationResult;
          }

          const reservationId = crypto.randomUUID();
          const count = window.count + 1;
          await transaction.put(WINDOW_STORAGE_KEY, {
            count,
            resetAt: window.resetAt,
          });
          await transaction.put(
            reservationStorageKey(reservationId),
            { resetAt: window.resetAt } satisfies StoredReservation,
          );
          await transaction.setAlarm(window.resetAt);
          return {
            reserved: true,
            reservationId,
            limit: policy.maxRequests,
            remaining: Math.max(0, policy.maxRequests - count),
            retryAfter: 0,
            resetAt: window.resetAt,
          } satisfies RateLimitReservationResult;
        },
      );
      return Response.json(result, {
        headers: { "cache-control": "no-store" },
      });
    } catch {
      return unavailableRateLimitResponse();
    }
  }

  private async handleRelease(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return Response.json({ error: "method_not_allowed" }, { status: 405 });
    }
    let body: RateLimitReleaseRequest;
    try {
      body = (await request.json()) as RateLimitReleaseRequest;
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }
    const reservationId = parseReservationId(body.reservation_id);
    if (!reservationId) {
      return Response.json(
        { error: "invalid_reservation_id" },
        { status: 400 },
      );
    }

    try {
      const result = await this.state.storage.transaction(
        async (transaction) => {
          const now = Date.now();
          const stored = await transaction.get<StoredWindow>(
            WINDOW_STORAGE_KEY,
          );
          const reservation = await transaction.get<StoredReservation>(
            reservationStorageKey(reservationId),
          );
          const active = activeWindow(stored, now);
          if (
            !reservation ||
            !active ||
            reservation.resetAt !== active.resetAt ||
            active.count < 1
          ) {
            // A stale reservation must never decrement a new window. Cleaning
            // its marker is safe and makes retries idempotent.
            if (reservation) {
              await transaction.delete(reservationStorageKey(reservationId));
            }
            if (stored && !active) {
              await transaction.delete(WINDOW_STORAGE_KEY);
            }
            return {
              released: false,
              remaining: active ? active.count : 0,
              resetAt: active?.resetAt || null,
            } satisfies RateLimitReleaseResult;
          }

          await transaction.delete(reservationStorageKey(reservationId));
          const count = active.count - 1;
          if (count === 0) {
            await transaction.delete(WINDOW_STORAGE_KEY);
            await transaction.deleteAlarm();
            return {
              released: true,
              remaining: 0,
              resetAt: null,
            } satisfies RateLimitReleaseResult;
          }
          await transaction.put(WINDOW_STORAGE_KEY, {
            count,
            resetAt: active.resetAt,
          });
          await transaction.setAlarm(active.resetAt);
          return {
            released: true,
            remaining: count,
            resetAt: active.resetAt,
          } satisfies RateLimitReleaseResult;
        },
      );
      return Response.json(result, {
        headers: { "cache-control": "no-store" },
      });
    } catch {
      return unavailableRateLimitResponse();
    }
  }

  async health(): Promise<{ status: "ok"; service: "rate-limit" }> {
    await this.state.storage.get(WINDOW_STORAGE_KEY);
    return { status: "ok", service: "rate-limit" };
  }

  async checkTts(
    charCount: number,
    profile: TtsFineRateLimitProfile = "desktop",
  ): Promise<TtsFineRateLimitResult> {
    const policy = TTS_FINE_RATE_LIMITS[profile];
    if (
      !policy ||
      !Number.isInteger(charCount) ||
      charCount < 1 ||
      charCount > policy.maxRequestChars
    ) {
      throw new RangeError("invalid TTS character count");
    }
    const now = Date.now();
    const windowMs = policy.burstWindowSeconds * 1000;
    const { result, nextAlarmAt } = await this.state.storage.transaction(
      async (transaction) => {
        const storedBurst = await transaction.get<StoredTtsBurst>(
          TTS_BURST_STORAGE_KEY,
        );
        const timestamps = (storedBurst?.timestamps || []).filter(
          (timestamp) => timestamp > now - windowMs,
        );
        const storedDaily = await transaction.get<StoredTtsDaily>(
          TTS_DAILY_STORAGE_KEY,
        );
        const dailyResetAt =
          storedDaily && storedDaily.resetAt > now
            ? storedDaily.resetAt
            : nextUtcMidnight(now);
        const dailyChars =
          storedDaily && storedDaily.resetAt > now ? storedDaily.chars : 0;

        if (timestamps.length >= policy.burstRequests) {
          await storeOrDeleteBurst(transaction, timestamps);
          return {
            result: ttsFineResult(
              1,
              policy.burstWindowSeconds,
              timestamps.length,
              dailyChars,
              dailyResetAt,
              policy,
            ),
            nextAlarmAt: earliestTimestamp(
              timestamps[0] + windowMs,
              dailyChars > 0 ? dailyResetAt : null,
            ),
          };
        }
        if (dailyChars + charCount > policy.dailyChars) {
          await storeOrDeleteBurst(transaction, timestamps);
          return {
            result: ttsFineResult(
              2,
              Math.max(1, Math.floor((dailyResetAt - now) / 1000)),
              timestamps.length,
              dailyChars,
              dailyResetAt,
              policy,
            ),
            nextAlarmAt: earliestTimestamp(
              timestamps.length > 0 ? timestamps[0] + windowMs : null,
              dailyResetAt,
            ),
          };
        }

        timestamps.push(now);
        const nextDailyChars = dailyChars + charCount;
        await transaction.put(TTS_BURST_STORAGE_KEY, { timestamps });
        await transaction.put(TTS_DAILY_STORAGE_KEY, {
          chars: nextDailyChars,
          resetAt: dailyResetAt,
        });
        return {
          result: ttsFineResult(
            0,
            0,
            timestamps.length,
            nextDailyChars,
            dailyResetAt,
            policy,
          ),
          nextAlarmAt: Math.min(timestamps[0] + windowMs, dailyResetAt),
        };
      },
    );
    await this.state.storage.setAlarm(nextAlarmAt);
    return result;
  }

  async alarm(): Promise<void> {
    const now = Date.now();
    const windowMs = TTS_BURST_RETENTION_WINDOW_SECONDS * 1000;
    const nextResetAt = await this.state.storage.transaction(
      async (transaction) => {
        const nextAlarms: number[] = [];
        const stored = await transaction.get<StoredWindow>(WINDOW_STORAGE_KEY);
        if (stored && stored.resetAt <= now) {
          await transaction.delete(WINDOW_STORAGE_KEY);
        } else if (stored) {
          nextAlarms.push(stored.resetAt);
        }

        const reservations = await transaction.list<StoredReservation>({
          prefix: RESERVATION_STORAGE_PREFIX,
        });
        for (const [key, reservation] of reservations) {
          if (
            !reservation ||
            !Number.isFinite(reservation.resetAt) ||
            reservation.resetAt <= now
          ) {
            await transaction.delete(key);
          }
        }

        const storedBurst = await transaction.get<StoredTtsBurst>(
          TTS_BURST_STORAGE_KEY,
        );
        const timestamps = (storedBurst?.timestamps || []).filter(
          (timestamp) => timestamp > now - windowMs,
        );
        await storeOrDeleteBurst(transaction, timestamps);
        if (timestamps.length > 0) {
          nextAlarms.push(timestamps[0] + windowMs);
        }

        const storedDaily = await transaction.get<StoredTtsDaily>(
          TTS_DAILY_STORAGE_KEY,
        );
        if (storedDaily && storedDaily.resetAt <= now) {
          await transaction.delete(TTS_DAILY_STORAGE_KEY);
        } else if (storedDaily) {
          nextAlarms.push(storedDaily.resetAt);
        }
        return nextAlarms.length > 0 ? Math.min(...nextAlarms) : null;
      },
    );
    if (nextResetAt !== null) {
      await this.state.storage.setAlarm(nextResetAt);
    }
  }
}

async function storeOrDeleteBurst(
  transaction: DurableObjectTransaction,
  timestamps: number[],
): Promise<void> {
  if (timestamps.length > 0) {
    await transaction.put(TTS_BURST_STORAGE_KEY, { timestamps });
  } else {
    await transaction.delete(TTS_BURST_STORAGE_KEY);
  }
}

function nextUtcMidnight(now: number): number {
  const date = new Date(now);
  return Date.UTC(
    date.getUTCFullYear(),
    date.getUTCMonth(),
    date.getUTCDate() + 1,
  );
}

function earliestTimestamp(
  first: number | null,
  second: number | null,
): number {
  if (first === null) return second as number;
  if (second === null) return first;
  return Math.min(first, second);
}

function ttsFineResult(
  status: 0 | 1 | 2,
  retryAfter: number,
  burstCount: number,
  dailyChars: number,
  dailyResetAt: number,
  policy: TtsFineRateLimitPolicy,
): TtsFineRateLimitResult {
  return {
    status,
    retryAfter,
    burstRemaining: Math.max(0, policy.burstRequests - burstCount),
    dailyCharsRemaining: Math.max(0, policy.dailyChars - dailyChars),
    dailyResetAt,
  };
}

function ttsFineRateLimitProfile(
  value: unknown,
): TtsFineRateLimitProfile | null {
  if (value === undefined || value === null || value === "") return "desktop";
  return value === "desktop" || value === "mobile" ? value : null;
}

function parsePolicy(body: RateLimitCheckRequest): RateLimitPolicy | null {
  if (
    typeof body.policy !== "string" ||
    new TextEncoder().encode(body.policy).byteLength < 1 ||
    new TextEncoder().encode(body.policy).byteLength > MAX_POLICY_NAME_BYTES ||
    !Number.isInteger(body.max_requests) ||
    (body.max_requests as number) < 1 ||
    (body.max_requests as number) > MAX_REQUESTS ||
    !Number.isInteger(body.window_seconds) ||
    (body.window_seconds as number) < 1 ||
    (body.window_seconds as number) > MAX_WINDOW_SECONDS
  ) {
    return null;
  }
  return {
    name: body.policy,
    maxRequests: body.max_requests as number,
    windowSeconds: body.window_seconds as number,
  };
}

export default {
  fetch(): Response {
    return Response.json({ status: "ok", service: "rate-limit" });
  },
};
