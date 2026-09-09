import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

const ownedRoutes = [
  ["GET", "/v1/screen-frame-egress/settings"],
  ["PATCH", "/v1/screen-frame-egress/settings"],
  ["POST", "/v1/screen-frame-egress/adjudications"],
  ["GET", "/v1/conversations/meeting%20one/screenshots"],
  ["PATCH", "/v1/conversations/meeting%20one/screenshot-sharing"],
  ["DELETE", "/v1/conversations/meeting%20one/screenshots"],
  ["DELETE", "/v1/conversations/meeting%20one/screenshots/frame-1"],
] as const;
const publicPaths = [
  "/v1/conversations/meeting%20one/shared/screenshots",
  "/v1/screen-frame-content?token=opaque-capability",
];

function fixture({
  authenticated = true,
  admitted = true,
  limited = false,
  status = 200,
} = {}) {
  const seen: Request[] = [],
    policies: unknown[] = [],
    names: string[] = [];
  let authCalls = 0;
  const bytes = new Uint8Array([255, 216, 1, 2, 255, 217]);
  const env = {
    INTERNAL_ASSERTION_SECRET: "screenshot-edge-synthetic-key",
    ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED: "true",
    AUTH: {
      fetch: async () => {
        authCalls++;
        return Response.json(
          authenticated
            ? { uid: "screen-owner", authority: "better-auth" }
            : {},
          { status: authenticated ? 200 : 401 }
        );
      },
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
        return new Response(status === 200 ? bytes : null, {
          status,
          headers: {
            "content-type": "image/jpeg",
            "cache-control": "no-store",
          },
        });
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
            return Response.json({
              allowed: !limited,
              limit: 30,
              remaining: limited ? 0 : 29,
              retryAfter: limited ? 60 : 0,
              resetAt: Date.now() + 3600000,
            });
          },
        };
      },
    },
  };
  return { env, seen, names, policies, bytes, authCalls: () => authCalls };
}
const callerHeaders = {
  authorization: "Bearer session",
  cookie: "session=private",
  "x-omi-uid": "other-owner",
  "x-omi-auth-context": "forged",
  "x-omi-internal-signature": "forged",
  "if-none-match": "stale-private-image",
  "content-type": "application/json",
};

describe("screenshot public boundary", () => {
  it.each(ownedRoutes)(
    "%s %s binds the owner and preserves transport",
    async (method, path) => {
      const f = fixture(),
        body = JSON.stringify({ bytes_base64: "AAEC/w==", enabled: false });
      const response = await edge.fetch(
        new Request("https://edge.test" + path, {
          method,
          headers: callerHeaders,
          ...(["POST", "PATCH"].includes(method) ? { body } : {}),
        }),
        f.env as never
      );
      expect(response.status).toBe(200);
      expect(f.seen).toHaveLength(1);
      const upstream = f.seen[0],
        context = await verifyRequestAuthContext(
          upstream,
          "api-core",
          f.env.INTERNAL_ASSERTION_SECRET
        );
      expect(context?.uid).toBe("screen-owner");
      expect(context?.method).toBe(method);
      expect(context?.path).toBe(path);
      for (const name of ["authorization", "cookie", "x-omi-uid"])
        expect(upstream.headers.has(name)).toBe(false);
      if (["POST", "PATCH"].includes(method))
        expect(await upstream.text()).toBe(body);
      if (method === "POST") {
        expect(f.names).toEqual(["screenshots:adjudicate:screen-owner"]);
        expect(f.policies).toEqual([
          {
            policy: "screenshots:adjudicate",
            max_requests: 30,
            window_seconds: 3600,
          },
        ]);
      } else expect(f.policies).toEqual([]);
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(new Uint8Array(await response.arrayBuffer())).toEqual(f.bytes);
    }
  );
  it.each(ownedRoutes)(
    "%s %s rejects missing and revoked sessions",
    async (method, path) => {
      for (const headers of [{}, callerHeaders]) {
        const f = fixture({ authenticated: false });
        const response = await edge.fetch(
          new Request("https://edge.test" + path, { method, headers }),
          f.env as never
        );
        expect(response.status).toBe(401);
        expect(f.seen).toEqual([]);
        expect(f.policies).toEqual([]);
      }
    }
  );
  it("rejects fenced accounts and exhausted quota before forwarding pixels", async () => {
    for (const [options, status] of [
      [{ admitted: false }, 409],
      [{ limited: true }, 429],
    ] as const) {
      const f = fixture(options);
      const response = await edge.fetch(
        new Request("https://edge.test/v1/screen-frame-egress/adjudications", {
          method: "POST",
          headers: callerHeaders,
          body: "private image transport",
        }),
        f.env as never
      );
      expect(response.status).toBe(status);
      expect(f.seen).toEqual([]);
    }
  });
  it.each(publicPaths)(
    "%s streams current visibility without forwarding caller identity",
    async (path) => {
      for (const status of [200, 404]) {
        const f = fixture({ authenticated: false, status });
        const response = await edge.fetch(
          new Request("https://edge.test" + path, { headers: callerHeaders }),
          f.env as never
        );
        expect(response.status).toBe(status);
        expect(f.authCalls()).toBe(0);
        expect(f.seen).toHaveLength(1);
        expect(f.seen[0].url).toBe("https://edge.test" + path);
        expect(Array.from(f.seen[0].headers)).toEqual([]);
        expect(f.policies).toEqual([]);
        expect(response.headers.get("cache-control")).toBe("no-store");
        expect(new Uint8Array(await response.arrayBuffer())).toEqual(
          status === 200 ? f.bytes : new Uint8Array()
        );
      }
    }
  );
});
