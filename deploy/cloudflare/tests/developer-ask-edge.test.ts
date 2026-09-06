import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";

function fixture(
  result: unknown = {
    allowed: true,
    limit: 25,
    remaining: 24,
    retryAfter: 0,
    resetAt: 1788656400000,
  }
) {
  const requests: Request[] = [],
    policies: unknown[] = [],
    names: string[] = [];
  const env = {
    API_CORE: {
      fetch: async (request: Request) => {
        requests.push(request);
        return Response.json({ answer: "answer", sources: [] });
      },
    },
    RATE_LIMITS: {
      idFromName(name: string) {
        names.push(name);
        return name;
      },
      get() {
        return {
          fetch: async (request: Request) => {
            policies.push(await request.json());
            return Response.json(result);
          },
        };
      },
    },
  };
  return { env, requests, policies, names };
}

function request() {
  return new Request("https://edge.test/v1/dev/user/ask", {
    method: "POST",
    headers: {
      authorization: "Bearer omi_dev_0123456789abcdef0123456789abcdef",
      cookie: "session=private",
      "x-omi-auth-context": "forged",
      "x-omi-internal-signature": "forged",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      question: "When is release?",
      timezone: "Asia/Shanghai",
    }),
  });
}

describe("Developer ask public boundary", () => {
  it("preserves only the external key and body, with the upstream 25/hour budget", async () => {
    const { env, requests, policies, names } = fixture();
    expect((await edge.fetch(request(), env as never)).status).toBe(200);
    expect(policies).toEqual([
      { policy: "dev:ask", max_requests: 25, window_seconds: 3600 },
    ]);
    expect(names[0]).toMatch(/^dev:ask:developer:[0-9a-f]{64}$/);
    expect(requests).toHaveLength(1);
    expect(requests[0].headers.get("authorization")).toBe(
      request().headers.get("authorization")
    );
    for (const name of [
      "cookie",
      "x-omi-auth-context",
      "x-omi-internal-signature",
    ])
      expect(requests[0].headers.has(name)).toBe(false);
    expect(await requests[0].json()).toEqual({
      question: "When is release?",
      timezone: "Asia/Shanghai",
    });
  });

  it("does not reach Core after the quota is exhausted", async () => {
    const { env, requests } = fixture({
      allowed: false,
      limit: 25,
      remaining: 0,
      retryAfter: 90,
      resetAt: 1788656400000,
    });
    const response = await edge.fetch(request(), env as never);
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("90");
    expect(requests).toHaveLength(0);
  });

  it.each(["unavailable", "malformed"])(
    "fails closed when the limiter is %s",
    async (kind) => {
      const { env, requests } = fixture({ allowed: true });
      if (kind === "unavailable")
        env.RATE_LIMITS.get = () => {
          throw new Error("unavailable");
        };
      expect((await edge.fetch(request(), env as never)).status).toBe(503);
      expect(requests).toHaveLength(0);
    }
  );
});
