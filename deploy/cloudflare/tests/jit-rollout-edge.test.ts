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
