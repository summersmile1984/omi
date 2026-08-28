import { afterEach, describe, expect, it, vi } from "vitest";
import { RateLimitDurableObject } from "../workers/edge/rate-limit";

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
});
