import { beforeEach, describe, expect, it, vi } from "vitest";
import { createSignedAuthContext } from "../workers/shared/auth-context";
import { betterAuth } from "better-auth";

const signJWT = vi.fn(async () => ({ token: "jwt-from-workers" }));
const verifyJWT = vi.fn(async ({ body }: { body: { token: string } }) =>
  body.token === "bridge-token" ? { payload: { uid: "jwt-user", sub: "jwt-user" } } : { payload: null },
);
const authHandler = vi.fn(async (_request: Request) => Response.json(null));

vi.mock("better-auth", () => ({
  betterAuth: vi.fn(() => ({
    api: { signJWT, verifyJWT },
    handler: authHandler,
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

function readyEnv(active = true) {
  let activeKey = active;
  const run = vi.fn(async () => ({ success: true }));
  const first = vi.fn(async () => (activeKey ? { id: "active-key" } : null));
  const bind = vi.fn(() => ({ first }));
  const prepare = vi.fn((query: string) =>
    query === "SELECT 1" ? { run } : { bind },
  );
  return {
    environment: {
      ...env(),
      AUTH_DB: { prepare } as unknown as D1Database,
    },
    activate: () => {
      activeKey = true;
    },
  };
}

describe("auth worker Better Auth dev issuer", () => {
  beforeEach(() => {
    vi.mocked(betterAuth).mockClear();
    signJWT.mockClear();
    verifyJWT.mockClear();
    authHandler.mockReset();
    authHandler.mockResolvedValue(Response.json(null));
  });

  it("hides the bridge when no issuer secret is configured", async () => {
    const response = await auth.fetch(new Request("https://auth.test/auth-issue", { method: "POST" }), env());
    expect(response.status).toBe(404);
  });

  it("reports readiness only when D1 has an active signing key", async () => {
    const ready = readyEnv();
    const response = await auth.fetch(
      new Request("https://auth.test/ready"),
      ready.environment,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      status: "ready",
      database: "ok",
      signing_key: "ok",
    });
    expect(signJWT).not.toHaveBeenCalled();
  });

  it("bootstraps a missing signing key before reporting readiness", async () => {
    const ready = readyEnv(false);
    signJWT.mockImplementationOnce(async () => {
      ready.activate();
      return { token: "bootstrap-token" };
    });
    const response = await auth.fetch(
      new Request("https://auth.test/ready"),
      ready.environment,
    );

    expect(response.status).toBe(200);
    expect(signJWT).toHaveBeenCalledWith({
      body: { payload: { sub: "jwks-readiness-bootstrap" } },
      headers: expect.any(Headers),
    });
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

  it("verifies an httpOnly Better Auth session cookie", async () => {
    authHandler.mockResolvedValueOnce(
      Response.json({ user: { id: "cookie-user" }, session: { id: "session-1" } }),
    );
    const response = await auth.fetch(
      new Request("https://auth.test/internal/verify", {
        method: "POST",
        headers: {
          "x-internal-assertion-secret": "internal-secret",
          cookie: "__Secure-better-auth.session_token=cookie-session",
          "x-request-id": "cookie-request",
        },
      }),
      env(),
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      uid: "cookie-user",
      authority: "better-auth",
      requestId: "cookie-request",
    });
    const sessionRequest = authHandler.mock.calls[0][0] as Request;
    expect(new URL(sessionRequest.url).pathname).toBe(
      "/api/better-auth/get-session",
    );
    expect(sessionRequest.headers.get("cookie")).toBe(
      "__Secure-better-auth.session_token=cookie-session",
    );
  });

  it("uses the same-origin public path, D1 rate limits, and rotating ES256 keys", async () => {
    const response = await auth.fetch(
      new Request("https://auth.test/api/better-auth/get-session"),
      env(),
    );

    expect(response.status).toBe(200);
    const options = vi.mocked(betterAuth).mock.calls.at(-1)?.[0];
    expect(options).toMatchObject({
      baseURL: "https://auth.test",
      basePath: "/api/better-auth",
      rateLimit: {
        enabled: true,
        storage: "database",
        window: 60,
        max: 100,
      },
      account: {
        encryptOAuthTokens: true,
        storeStateStrategy: "database",
        accountLinking: {
          enabled: true,
          disableImplicitLinking: true,
          trustedProviders: [],
          allowDifferentEmails: false,
        },
      },
    });
    const jwtPlugin = options?.plugins?.find((plugin) => plugin.id === "jwt");
    expect(jwtPlugin).toMatchObject({
      options: {
        jwt: {
          jwks: {
            keyPairConfig: { alg: "ES256" },
            rotationInterval: 2_592_000,
            gracePeriod: 172_800,
          },
          expirationTime: "24h",
        },
      },
    });
  });

  it("advertises and configures only providers with complete credentials", async () => {
    const providerEnv = {
      ...env(),
      GOOGLE_CLIENT_ID: "google-id",
      GOOGLE_CLIENT_SECRET: "google-secret",
      APPLE_CLIENT_ID: "incomplete-apple-id",
    };
    const capabilities = await auth.fetch(
      new Request("https://auth.test/api/better-auth/omi-capabilities"),
      providerEnv,
    );
    expect(await capabilities.json()).toEqual({
      social_providers: ["google"],
      explicit_account_linking: true,
      implicit_account_linking: false,
    });

    await auth.fetch(
      new Request("https://auth.test/api/better-auth/get-session"),
      providerEnv,
    );
    const options = vi.mocked(betterAuth).mock.calls.at(-1)?.[0];
    expect(options?.socialProviders).toEqual({
      google: { clientId: "google-id", clientSecret: "google-secret" },
    });
    expect(options?.account?.accountLinking?.trustedProviders).toEqual([
      "google",
    ]);
  });

  it("serves the Better Auth identity profile only for a signed edge context", async () => {
    const signed = await createSignedAuthContext(
      { uid: "profile-user", authority: "better-auth", requestId: "req-1" },
      "auth",
      "GET",
      "/internal/profile",
      "internal-secret",
    );
    const response = await auth.fetch(
      new Request("https://auth.test/internal/profile", {
        headers: {
          "x-omi-auth-context": signed?.encoded || "",
          "x-omi-internal-signature": signed?.signature || "",
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
    const signed = await createSignedAuthContext(
      { uid: "missing-user", authority: "better-auth", requestId: "req-2" },
      "auth",
      "GET",
      "/internal/profile",
      "internal-secret",
    );
    const response = await auth.fetch(
      new Request("https://auth.test/internal/profile", {
        headers: {
          "x-omi-auth-context": signed?.encoded || "",
          "x-omi-internal-signature": signed?.signature || "",
        },
      }),
      profileEnv(null),
    );
    expect(response.status).toBe(410);
  });
});
