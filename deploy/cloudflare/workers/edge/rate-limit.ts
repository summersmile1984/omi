import { recordFallback } from "../shared/fallback";
import type { AuthContext } from "../shared/auth-context";
import type { EdgeEnv } from "./env";

const WINDOW_STORAGE_KEY = "window";
const MAX_POLICY_NAME_BYTES = 128;
const MAX_REQUESTS = 1_000_000;
const MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60;

type StoredWindow = {
  count: number;
  resetAt: number;
};

type RateLimitCheckRequest = {
  policy?: unknown;
  max_requests?: unknown;
  window_seconds?: unknown;
};

export type RateLimitResult = {
  allowed: boolean;
  limit: number;
  remaining: number;
  retryAfter: number;
  resetAt: number;
};

export type EdgeRateLimitPolicy = {
  name: string;
  maxRequests: number;
  windowSeconds: number;
};

export const TTS_SYNTHESIZE_RATE_LIMIT: EdgeRateLimitPolicy = {
  name: "tts:synthesize",
  maxRequests: 300,
  windowSeconds: 3600,
};

export class RateLimitDurableObject {
  constructor(private readonly state: DurableObjectState) {}

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET") {
      await this.state.storage.get(WINDOW_STORAGE_KEY);
      return Response.json({ status: "ok", service: "rate-limit" });
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

  async alarm(): Promise<void> {
    const nextResetAt = await this.state.storage.transaction(
      async (transaction) => {
        const stored =
          await transaction.get<StoredWindow>(WINDOW_STORAGE_KEY);
        if (!stored) return null;
        if (stored.resetAt <= Date.now()) {
          await transaction.delete(WINDOW_STORAGE_KEY);
          return null;
        }
        return stored.resetAt;
      },
    );
    if (nextResetAt !== null) {
      await this.state.storage.setAlarm(nextResetAt);
    }
  }
}

function parsePolicy(body: RateLimitCheckRequest): EdgeRateLimitPolicy | null {
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

function parseResult(value: unknown): RateLimitResult | null {
  if (!value || typeof value !== "object") return null;
  const result = value as Partial<RateLimitResult>;
  if (
    typeof result.allowed !== "boolean" ||
    !Number.isInteger(result.limit) ||
    (result.limit as number) < 1 ||
    !Number.isInteger(result.remaining) ||
    (result.remaining as number) < 0 ||
    !Number.isInteger(result.retryAfter) ||
    (result.retryAfter as number) < 0 ||
    !Number.isFinite(result.resetAt)
  ) {
    return null;
  }
  return result as RateLimitResult;
}

export async function enforceEdgeRateLimit(
  env: EdgeEnv,
  auth: AuthContext,
  policy: EdgeRateLimitPolicy,
  requestId: string,
): Promise<Response | null> {
  try {
    const id = env.RATE_LIMITS.idFromName(`${policy.name}:${auth.uid}`);
    const response = await env.RATE_LIMITS.get(id).fetch(
      new Request("https://rate-limit.internal/check", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          policy: policy.name,
          max_requests: policy.maxRequests,
          window_seconds: policy.windowSeconds,
        }),
      }),
    );
    if (!response.ok)
      throw new Error("rate limit Durable Object rejected check");
    const result = parseResult(await response.json());
    if (!result) {
      recordFallback({
        component: "rate_limit",
        from: "durable_object",
        to: "unlimited",
        reason: "invalid_response",
        outcome: "degraded",
        requestId,
      });
      return null;
    }
    if (result.allowed) return null;
    return Response.json(
      {
        error: "rate_limit_exceeded",
        detail: `Rate limit exceeded. Try again in ${result.retryAfter}s.`,
      },
      {
        status: 429,
        headers: {
          "cache-control": "no-store",
          "x-ratelimit-limit": String(result.limit),
          "x-ratelimit-remaining": "0",
          "retry-after": String(result.retryAfter),
        },
      },
    );
  } catch {
    recordFallback({
      component: "rate_limit",
      from: "durable_object",
      to: "unlimited",
      reason: "dependency_unavailable",
      outcome: "degraded",
      requestId,
    });
    return null;
  }
}
