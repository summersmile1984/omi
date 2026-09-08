import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

const path = "/v1/jit/rollout-decision";

function fixture(authenticated = true, admitted = true) {
  const seen: Request[] = [];
  const env = {
    INTERNAL_ASSERTION_SECRET: "jit-rollout-synthetic-secret",
    ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED: "true",
    AUTH: {
      fetch: async () =>
        Response.json(
          authenticated ? { uid: "owner", authority: "better-auth" } : {},
          { status: authenticated ? 200 : 401 }
        ),
    },
    API_CORE: {
      fetch: async (request: Request) => {
        if (new URL(request.url).pathname === "/v1/account/cutover/control")
          return Response.json({
            state: "new",
            client_action: "none",
            product_traffic_allowed: admitted,
            migration: { destination_backend_bound: true },
          });
        seen.push(request);
        return Response.json(
          { effective: "enabled" },
          { headers: { "cache-control": "no-store" } }
        );
      },
    },
  };
  return { env, seen };
}

function request() {
  return new Request("https://edge.test" + path, {
    headers: {
      authorization: "Bearer synthetic-session",
      cookie: "session=synthetic",
      "x-omi-uid": "other",
      "x-omi-auth-context": "forged",
      "x-omi-internal-signature": "forged",
    },
  });
}

describe("JIT rollout public boundary", () => {
  it("binds trigger snapshots to the authenticated owner and strips forged authority", async () => {
    const { env, seen } = fixture();
    const snapshotPath = "/v1/jit/trigger-snapshot";
    const incoming = new Request("https://edge.test" + snapshotPath, request());
    const response = await edge.fetch(incoming, env as never);
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(seen).toHaveLength(1);
    const context = await verifyRequestAuthContext(seen[0], "api-core", env.INTERNAL_ASSERTION_SECRET);
    expect(context).toMatchObject({ uid: "owner", method: "GET", path: snapshotPath });
    for (const name of ["cookie", "authorization", "x-omi-uid"])
      expect(seen[0].headers.has(name)).toBe(false);
  });

  it("binds the current policy read to the authenticated owner and route", async () => {
    const { env, seen } = fixture();
    const response = await edge.fetch(request(), env as never);
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(seen).toHaveLength(1);
    const context = await verifyRequestAuthContext(
      seen[0],
      "api-core",
      env.INTERNAL_ASSERTION_SECRET
    );
    expect(context).toMatchObject({ uid: "owner", method: "GET", path });
    for (const name of ["cookie", "authorization", "x-omi-uid"])
      expect(seen[0].headers.has(name)).toBe(false);
  });

  it.each([
    [false, true, 401],
    [true, false, 409],
  ] as const)(
    "denies unauthenticated or cutover-blocked access (%s, %s)",
    async (authenticated, admitted, status) => {
      const { env, seen } = fixture(authenticated, admitted);
      expect((await edge.fetch(request(), env as never)).status).toBe(status);
      expect(seen).toHaveLength(0);
    }
  );
});

describe("JIT proactivity reservation public boundary", () => {
  const reservationPath = "/v1/jit/proactivity/reservations";

  it.each([true, false])(
    "uses original paid-work rate admission, allowed=%s",
    async (allowed) => {
      const { env, seen } = fixture();
      const rateRequests: Record<string, unknown>[] = [];
      const configured = {
        ...env,
        RATE_LIMITS: {
          idFromName: (value: string) => value,
          get: () => ({
            fetch: async (request: Request) => {
              rateRequests.push(
                (await request.json()) as Record<string, unknown>,
              );
              return Response.json({
                allowed,
                limit: 120,
                remaining: allowed ? 119 : 0,
                retryAfter: 30,
                resetAt: Date.now() + 30000,
              });
            },
          }),
        },
      };
      const response = await edge.fetch(
        new Request("https://edge.test" + reservationPath, {
          method: "POST",
          headers: {
            authorization: "Bearer synthetic-session",
            "content-type": "application/json",
            "x-omi-uid": "forged",
          },
          body: JSON.stringify({ event_id: "synthetic-hash" }),
        }),
        configured as never,
      );
      expect(response.status).toBe(allowed ? 200 : 429);
      expect(rateRequests).toHaveLength(1);
      expect(rateRequests[0]).toMatchObject({
        max_requests: 120,
        window_seconds: 3600,
      });
      expect(seen).toHaveLength(allowed ? 1 : 0);
      if (allowed) {
        const context = await verifyRequestAuthContext(
          seen[0],
          "api-core",
          env.INTERNAL_ASSERTION_SECRET,
        );
        expect(context).toMatchObject({
          uid: "owner",
          method: "POST",
          path: reservationPath,
        });
        expect(await seen[0].json()).toEqual({ event_id: "synthetic-hash" });
        expect(seen[0].headers.has("authorization")).toBe(false);
        expect(seen[0].headers.has("x-omi-uid")).toBe(false);
      }
    },
  );
});
