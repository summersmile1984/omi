import { beforeEach, describe, expect, it, vi } from "vitest";
import { encodeAuthContext, signAuthContext } from "../workers/shared/auth-context";

const signJWT = vi.fn(async () => ({ token: "jwt-from-workers" }));
const verifyJWT = vi.fn(async ({ body }: { body: { token: string } }) =>
  body.token === "bridge-token" ? { payload: { uid: "jwt-user", sub: "jwt-user" } } : { payload: null },
);

vi.mock("better-auth", () => ({
  betterAuth: vi.fn(() => ({
    api: { signJWT, verifyJWT },
    handler: vi.fn(async () => Response.json(null)),
  })),
}));

import auth from "../workers/auth/index";

const env = (issuerSecret?: string) => ({
  AUTH_DB: {} as D1Database,
  BETTER_AUTH_SECRET: "test-auth-secret",
  BETTER_AUTH_URL: "https://auth.test",
  INTERNAL_ASSERTION_SECRET: "internal-secret",
  AUTH_DEV_ISSUER_SECRET: issuerSecret,
});

function profileEnv(row: Record<string, unknown> | null) {
  const first = vi.fn(async () => row);
  const bind = vi.fn(() => ({ first }));
  const prepare = vi.fn(() => ({ bind }));
  return { ...env("issuer-secret"), AUTH_DB: { prepare } as unknown as D1Database };
}

describe("auth worker Better Auth dev issuer", () => {
  beforeEach(() => {
    signJWT.mockClear();
    verifyJWT.mockClear();
  });

  it("hides the bridge when no issuer secret is configured", async () => {
    const response = await auth.fetch(new Request("https://auth.test/auth-issue", { method: "POST" }), env());
    expect(response.status).toBe(404);
  });

  it("rejects a missing or incorrect bearer secret", async () => {
    const request = (authorization?: string) =>
      new Request("https://auth.test/auth-issue", {
        method: "POST",
        headers: { "content-type": "application/json", ...(authorization ? { authorization } : {}) },
        body: JSON.stringify({ uid: "mobile-user" }),
      });

    expect((await auth.fetch(request(), env("issuer-secret"))).status).toBe(401);
    expect((await auth.fetch(request("Bearer wrong"), env("issuer-secret"))).status).toBe(401);
    expect(signJWT).not.toHaveBeenCalled();
  });

  it("validates the uid before asking Better Auth to mint a token", async () => {
    const response = await auth.fetch(
      new Request("https://auth.test/auth-issue", {
        method: "POST",
        headers: { authorization: "Bearer issuer-secret", "content-type": "application/json" },
        body: JSON.stringify({ uid: "" }),
      }),
      env("issuer-secret"),
    );
    expect(response.status).toBe(400);
    expect(signJWT).not.toHaveBeenCalled();
  });

  it("mints a Better Auth JWT for the staging Flutter bridge", async () => {
    const response = await auth.fetch(
      new Request("https://auth.test/auth-issue", {
        method: "POST",
        headers: { authorization: "Bearer issuer-secret", "content-type": "application/json" },
        body: JSON.stringify({ uid: "mobile-user" }),
      }),
      env("issuer-secret"),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ token: "jwt-from-workers", uid: "mobile-user" });
    expect(signJWT).toHaveBeenCalledWith({
      body: { payload: { uid: "mobile-user", sub: "mobile-user" } },
      headers: expect.any(Headers),
    });
  });

  it("verifies a server-issued JWT when there is no Better Auth database session", async () => {
    const response = await auth.fetch(
      new Request("https://auth.test/internal/verify", {
        method: "POST",
        headers: {
          "x-internal-assertion-secret": "internal-secret",
          authorization: "Bearer bridge-token",
        },
      }),
      env("issuer-secret"),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ uid: "jwt-user", authority: "better-auth", requestId: "internal" });
    expect(verifyJWT).toHaveBeenCalledWith({
      body: { token: "bridge-token" },
      headers: expect.any(Headers),
    });
  });

  it("serves the Better Auth identity profile only for a signed edge context", async () => {
    const context = encodeAuthContext({ uid: "profile-user", authority: "better-auth", requestId: "req-1" });
    const signature = await signAuthContext(context, "internal-secret");
    const response = await auth.fetch(
      new Request("https://auth.test/internal/profile", {
        headers: {
          "x-omi-auth-context": context,
          "x-omi-internal-signature": signature || "",
        },
      }),
      profileEnv({
        id: "profile-user",
        name: "Staging User",
        email: "staging@example.test",
        createdAt: "2026-08-27T13:51:34.974Z",
      }),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      uid: "profile-user",
      name: "Staging User",
      email: "staging@example.test",
      created_at: "2026-08-27T13:51:34.974Z",
    });
  });

  it("does not expose a profile for an unknown Better Auth user", async () => {
    const context = encodeAuthContext({ uid: "missing-user", authority: "better-auth", requestId: "req-2" });
    const signature = await signAuthContext(context, "internal-secret");
    const response = await auth.fetch(
      new Request("https://auth.test/internal/profile", {
        headers: { "x-omi-auth-context": context, "x-omi-internal-signature": signature || "" },
      }),
      profileEnv(null),
    );
    expect(response.status).toBe(410);
  });
});
