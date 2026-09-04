import { describe, expect, it, vi } from "vitest";
import edge from "../workers/edge/index";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

const query = "?app_version=1.0.0&platform=android&trigger=app_launch";
const secret = "announcement-routing-fixture-secret";
const paths = [
  ["GET", "/v1/announcements/pending" + query],
  ["POST", "/v1/announcements/fixture/dismiss"],
] as const;

function fixture() {
  const auth = vi.fn(async (request: Request) =>
    request.headers.get("authorization") === "Bearer valid-session"
      ? Response.json({ uid: "existing-user", authority: "better-auth" })
      : Response.json({ error: "unauthorized" }, { status: 401 }),
  );
  const core = vi.fn(async (request: Request) => {
    const identity = await verifyRequestAuthContext(
      request,
      "api-core",
      secret,
    );
    return identity
      ? Response.json({ uid: identity.uid })
      : Response.json({ error: "unauthorized" }, { status: 401 });
  });
  return {
    auth,
    core,
    env: {
      INTERNAL_ASSERTION_SECRET: secret,
      AUTH: { fetch: auth },
      API_CORE: { fetch: core },
    },
  };
}

describe("announcement route identity ownership", () => {
  it.each(paths)(
    "%s %s authenticates an existing user before forwarding",
    async (method, path) => {
      const { env, auth, core } = fixture();
      const response = await edge.fetch(
        new Request("https://edge.test" + path, {
          method,
          headers: {
            authorization: "Bearer valid-session",
            cookie: "session=private",
            "x-omi-uid": "forged-user",
            "x-omi-auth-context": "forged-context",
            "x-omi-internal-signature": "forged-signature",
          },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ uid: "existing-user" });
      expect(auth).toHaveBeenCalledOnce();
      expect(core).toHaveBeenCalledOnce();
      const forwarded = core.mock.calls[0][0];
      expect(
        new URL(forwarded.url).pathname + new URL(forwarded.url).search,
      ).toBe(path);
      expect(forwarded.method).toBe(method);
      for (const header of ["authorization", "cookie", "x-omi-uid"]) {
        expect(forwarded.headers.has(header)).toBe(false);
      }
      expect(
        await verifyRequestAuthContext(forwarded, "api-core", secret),
      ).toMatchObject({
        uid: "existing-user",
        authority: "better-auth",
      });
    },
  );

  it.each(paths)(
    "%s %s rejects missing and revoked identity before Core",
    async (method, path) => {
      for (const authorization of [undefined, "Bearer revoked-session"]) {
        const { env, core } = fixture();
        const response = await edge.fetch(
          new Request("https://edge.test" + path, {
            method,
            headers: authorization ? { authorization } : {},
          }),
          env as never,
        );
        expect(response.status).toBe(401);
        expect(core).not.toHaveBeenCalled();
      }
    },
  );

  it("preserves anonymous release-note reads and the Core admin secret owner", async () => {
    const { env, auth, core } = fixture();
    core.mockImplementation(async () => Response.json({ public: true }));
    const requests = [
      ["GET", "/v1/announcements/changelogs"],
      ["GET", "/v1/announcements/features"],
      ["GET", "/v1/announcements/general"],
      ["GET", "/v1/announcements/all"],
      ["GET", "/v1/announcements/fixture"],
      ["POST", "/v1/announcements"],
      ["PUT", "/v1/announcements/fixture"],
      ["DELETE", "/v1/announcements/fixture"],
    ];
    for (const [method, path] of requests) {
      const response = await edge.fetch(
        new Request("https://edge.test" + path, {
          method,
          headers: {
            "x-announcements-admin-key": "admin-fixture",
            "x-omi-auth-context": "forged-context",
            "x-omi-internal-signature": "forged-signature",
          },
        }),
        env as never,
      );
      expect(response.status).toBe(200);
      const forwarded = core.mock.calls.at(-1)![0];
      expect(forwarded.headers.get("x-announcements-admin-key")).toBe(
        "admin-fixture",
      );
      expect(forwarded.headers.has("x-omi-auth-context")).toBe(false);
      expect(forwarded.headers.has("x-omi-internal-signature")).toBe(false);
    }
    expect(auth).not.toHaveBeenCalled();
  });
});
