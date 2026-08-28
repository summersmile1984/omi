import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EDGE_RATE_LIMIT_POLICIES,
  edgeRateLimitPolicyForRequest,
  enforceEdgeRateLimit,
  RateLimitDurableObject,
} from "../workers/edge/rate-limit";

class FakeDurableObjectStorage {
  private value: unknown;
  private transactionTail: Promise<void> = Promise.resolve();
  alarmAt?: number;

  async get<T>(_key: string): Promise<T | undefined> {
    return this.value as T | undefined;
  }

  async put(_key: string, value: unknown): Promise<void> {
    this.value = structuredClone(value);
  }

  async delete(_key: string): Promise<boolean> {
    const existed = this.value !== undefined;
    this.value = undefined;
    return existed;
  }

  async setAlarm(timestamp: number): Promise<void> {
    this.alarmAt = timestamp;
  }

  async transaction<T>(
    callback: (transaction: DurableObjectTransaction) => Promise<T>,
  ): Promise<T> {
    const previous = this.transactionTail;
    let release: () => void = () => undefined;
    this.transactionTail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await callback(this as unknown as DurableObjectTransaction);
    } finally {
      release();
    }
  }
}

function createLimiter() {
  const storage = new FakeDurableObjectStorage();
  const limiter = new RateLimitDurableObject({
    storage,
  } as unknown as DurableObjectState);
  return { limiter, storage };
}

function checkRequest(maxRequests: number, windowSeconds: number) {
  return new Request("https://rate-limit.internal/check", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      policy: "tts:synthesize",
      max_requests: maxRequests,
      window_seconds: windowSeconds,
    }),
  });
}

describe("RateLimitDurableObject", () => {
  afterEach(() => vi.useRealTimers());

  it("serializes concurrent checks so the limit cannot be oversubscribed", async () => {
    const { limiter } = createLimiter();
    const responses = await Promise.all(
      Array.from({ length: 301 }, () => limiter.fetch(checkRequest(300, 3600))),
    );
    const results = await Promise.all(
      responses.map(
        (response) =>
          response.json() as Promise<{ allowed: boolean; remaining: number }>,
      ),
    );

    expect(results.filter((result) => result.allowed)).toHaveLength(300);
    expect(results.filter((result) => !result.allowed)).toEqual([
      expect.objectContaining({ remaining: 0 }),
    ]);
  });

  it("resets an expired window and removes expired state on alarm", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-28T00:00:00Z"));
    const { limiter, storage } = createLimiter();

    expect(
      await (await limiter.fetch(checkRequest(2, 60))).json(),
    ).toMatchObject({
      allowed: true,
      remaining: 1,
    });
    expect(
      await (await limiter.fetch(checkRequest(2, 60))).json(),
    ).toMatchObject({
      allowed: true,
      remaining: 0,
    });
    expect(
      await (await limiter.fetch(checkRequest(2, 60))).json(),
    ).toMatchObject({
      allowed: false,
      retryAfter: 60,
    });

    vi.advanceTimersByTime(60_001);
    await limiter.alarm();
    expect(await storage.get("window")).toBeUndefined();
    expect(
      await (await limiter.fetch(checkRequest(2, 60))).json(),
    ).toMatchObject({
      allowed: true,
      remaining: 1,
    });
  });

  it("rejects malformed or unbounded policies", async () => {
    const { limiter } = createLimiter();
    const malformed = await limiter.fetch(
      new Request("https://rate-limit.internal/check", {
        method: "POST",
        body: "not-json",
      }),
    );
    const unbounded = await limiter.fetch(checkRequest(1_000_001, 60));

    expect(malformed.status).toBe(400);
    expect(unbounded.status).toBe(400);
  });

  it("maps every Cloudflare-owned request shape to the legacy policy", () => {
    const cases = [
      ["POST", "/v2/messages", "chat:send_message"],
      ["POST", "/v1/stt/transcribe", "stt:transcribe"],
      ["POST", "/v1/stt/transcribe-async", "stt:transcribe"],
      ["POST", "/v1/stt/transcribe-workers-ai", "stt:transcribe"],
      ["POST", "/v1/conversations/search", "conversations:search"],
      ["POST", "/v3/memories", "memories:create"],
      ["DELETE", "/v3/memories", "memories:delete_all"],
      ["DELETE", "/v3/memories/batch", "memories:delete_batch"],
      ["DELETE", "/v3/memories/memory-1", "memories:delete"],
      ["PATCH", "/v3/memories/memory-1", "memories:modify"],
      ["PATCH", "/v3/memories/memory-1/visibility", "memories:modify"],
      ["POST", "/v3/memories/memory-1/review", "memories:modify"],
      ["POST", "/v1/tts/synthesize", "tts:synthesize"],
      ["POST", "/v1/tts/synthesize-workers-ai", "tts:synthesize"],
    ] as const;

    for (const [method, path, policy] of cases) {
      expect(edgeRateLimitPolicyForRequest(method, path)?.name).toBe(policy);
    }
    expect(edgeRateLimitPolicyForRequest("GET", "/v3/memories")).toBeNull();
    expect(
      edgeRateLimitPolicyForRequest("DELETE", "/v3/memories/a/b"),
    ).toBeNull();
  });

  it("applies the legacy boost knob and returns the FastAPI-compatible 429 body", async () => {
    let checkBody: Record<string, unknown> | undefined;
    const env = {
      RATE_LIMIT_BOOST: "0.5",
      RATE_LIMITS: {
        idFromName: (name: string) => name,
        get: () => ({
          fetch: async (request: Request) => {
            checkBody = (await request.json()) as Record<string, unknown>;
            return Response.json({
              allowed: false,
              limit: 60,
              remaining: 0,
              retryAfter: 30,
              resetAt: Date.now() + 30_000,
            });
          },
        }),
      },
    } as never;
    const response = await enforceEdgeRateLimit(
      env,
      {
        uid: "user-1",
        authority: "better-auth",
        requestId: "request-1",
      },
      EDGE_RATE_LIMIT_POLICIES["chat:send_message"],
      "request-1",
    );

    expect(checkBody?.max_requests).toBe(60);
    expect(response?.status).toBe(429);
    expect(await response?.json()).toEqual({
      detail: "Rate limit exceeded. Try again in 30s.",
    });
  });

  it("preserves the legacy shadow-mode knob without logging a UID", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const env = {
      RATE_LIMIT_SHADOW_MODE: "true",
      RATE_LIMITS: {
        idFromName: (name: string) => name,
        get: () => ({
          fetch: () =>
            Response.json({
              allowed: false,
              limit: 60,
              remaining: 0,
              retryAfter: 30,
              resetAt: Date.now() + 30_000,
            }),
        }),
      },
    } as never;
    const response = await enforceEdgeRateLimit(
      env,
      {
        uid: "user-1",
        authority: "better-auth",
        requestId: "request-1",
      },
      EDGE_RATE_LIMIT_POLICIES["stt:transcribe"],
      "request-1",
    );

    expect(response).toBeNull();
    expect(JSON.parse(String(warning.mock.calls[0]?.[0]))).toMatchObject({
      event: "rate_limit_shadow",
      policy: "stt:transcribe",
      retry_after: 30,
      request_id: "request-1",
    });
    expect(String(warning.mock.calls[0]?.[0])).not.toContain("user-1");
    warning.mockRestore();
  });
});
