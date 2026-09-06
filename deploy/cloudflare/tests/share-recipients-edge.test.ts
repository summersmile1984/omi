import { describe, expect, it } from "vitest";
import edge from "../workers/edge/index";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

const path = "/v1/conversations/meeting/share-recipients";

function fixture(authenticated = true, admitted = true) {
  const seen: Request[] = [];
  const env = {
    INTERNAL_ASSERTION_SECRET: "share-recipient-synthetic-secret",
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
          { recipients: [] },
          {
            headers: { "cache-control": "private, no-store" },
          }
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

describe("share recipient public boundary", () => {
  it("binds identity and route to the authenticated owner", async () => {
    const { env, seen } = fixture();
    const response = await edge.fetch(request(), env as never);
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("private, no-store");
    expect(seen).toHaveLength(1);
    const context = await verifyRequestAuthContext(
      seen[0],
      "api-core",
      env.INTERNAL_ASSERTION_SECRET
    );
    expect(context?.uid).toBe("owner");
    expect(context?.path).toBe(path);
    expect(context?.method).toBe("GET");
    expect(seen[0].headers.has("cookie")).toBe(false);
    expect(seen[0].headers.has("authorization")).toBe(false);
    expect(seen[0].headers.has("x-omi-uid")).toBe(false);
  });

  it("does not forward an unauthenticated request", async () => {
    const { env, seen } = fixture(false);
    expect((await edge.fetch(request(), env as never)).status).toBe(401);
    expect(seen).toHaveLength(0);
  });

  it("does not bypass account cutover admission", async () => {
    const { env, seen } = fixture(true, false);
    expect((await edge.fetch(request(), env as never)).status).toBe(409);
    expect(seen).toHaveLength(0);
  });
});
