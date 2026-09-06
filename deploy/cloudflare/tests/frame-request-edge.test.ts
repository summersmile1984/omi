import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

const routes = [
  ["GET", "/v1/frame-requests/pending", "read"],
  ["GET", "/v1/frame-requests/status/frame-1", "read"],
  ["GET", "/v1/frame-requests/temporary/frame-1/image", "read"],
  ["GET", "/v1/conversations/conversation-1/photos/frame-1/image", "read"],
  ["POST", "/v1/frame-requests", "write"],
  ["POST", "/v1/frame-requests/frame-1/state", "write"],
  ["POST", "/v1/frame-requests/frame-1/promote", "write"],
  ["POST", "/v1/frame-requests/frame-1/upload", "upload"],
] as const;

function fixture({
  authenticated = true,
  admitted = true,
  limited = false,
} = {}) {
  const seen: Request[] = [],
    policies: unknown[] = [],
    names: string[] = [];
  const env = {
    INTERNAL_ASSERTION_SECRET: "frame-edge-synthetic-secret",
    ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED: "true",
    AUTH: {
      fetch: async () =>
        authenticated
          ? Response.json({ uid: "frame-owner", authority: "better-auth" })
          : Response.json({ error: "unauthorized" }, { status: 401 }),
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
        return new Response(new Uint8Array([255, 216, 255, 217]), {
          headers: {
            "content-type": "image/jpeg",
            "cache-control": "private, no-store",
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
  return { env, seen, policies, names };
}

describe("frame request public boundary", () => {
  it.each(routes)(
    "%s %s preserves transport, signed identity and the %s quota",
    async (method, path, policy) => {
      const { env, seen, names, policies } = fixture();
      const body = new Uint8Array([0, 255, 10, 13, 4, 128]);
      const response = await edge.fetch(
        new Request(
          `https://edge.test${path}?account_generation=0&device_id=desktop`,
          {
            method,
            headers: {
              authorization: "Bearer private-session",
              cookie: "session=private",
              "x-omi-uid": "forged",
              "x-omi-auth-context": "forged",
              "content-type": "multipart/form-data; boundary=fixture",
            },
            ...(method === "POST" ? { body } : {}),
          }
        ),
        env as never
      );
      expect(response.status).toBe(200);
      expect(seen).toHaveLength(1);
      const request = seen[0];
      expect(new URL(request.url).pathname).toBe(path);
      expect(new URL(request.url).search).toBe(
        "?account_generation=0&device_id=desktop"
      );
      expect(request.method).toBe(method);
      for (const name of ["authorization", "cookie", "x-omi-uid"])
        expect(request.headers.has(name)).toBe(false);
      expect(
        (
          await verifyRequestAuthContext(
            request,
            "api-core",
            env.INTERNAL_ASSERTION_SECRET
          )
        )?.uid
      ).toBe("frame-owner");
      if (method === "POST") {
        expect(request.headers.get("content-type")).toBe(
          "multipart/form-data; boundary=fixture"
        );
        expect(new Uint8Array(await request.arrayBuffer())).toEqual(body);
      }
      expect(names).toEqual([`frame_requests:${policy}:frame-owner`]);
      expect(policies).toEqual([
        {
          policy: `frame_requests:${policy}`,
          max_requests: policy === "upload" ? 30 : 120,
          window_seconds: 3600,
        },
      ]);
      expect(response.headers.get("cache-control")).toBe("private, no-store");
      expect(new Uint8Array(await response.arrayBuffer())).toEqual(
        new Uint8Array([255, 216, 255, 217])
      );
    }
  );

  it.each(routes)(
    "%s %s rejects missing or revoked credentials before Core",
    async (method, path) => {
      for (const headers of [
        new Headers(),
        new Headers({ authorization: "Bearer revoked-session" }),
      ]) {
        const { env, seen, names } = fixture({ authenticated: false });
        const response = await edge.fetch(
          new Request(`https://edge.test${path}`, { method, headers }),
          env as never
        );
        expect(response.status).toBe(401);
        expect(seen).toEqual([]);
        expect(names).toEqual([]);
      }
    }
  );

  it("does not forward image bytes when account traffic is fenced or quota exhausted", async () => {
    for (const [options, status] of [
      [{ admitted: false }, 409],
      [{ limited: true }, 429],
    ] as const) {
      const { env, seen } = fixture(options);
      const response = await edge.fetch(
        new Request("https://edge.test/v1/frame-requests/frame-1/upload", {
          method: "POST",
          headers: { authorization: "Bearer session" },
          body: "private pixels",
        }),
        env as never
      );
      expect(response.status).toBe(status);
      expect(seen).toEqual([]);
    }
  });
});
