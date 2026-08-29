import { beforeEach, describe, expect, it, vi } from "vitest";
import { createSignedAuthContext } from "../workers/shared/auth-context";
import { betterAuth } from "better-auth";

const signJWT = vi.fn(async () => ({ token: "jwt-from-workers" }));
const verifyJWT = vi.fn(async ({ body }: { body: { token: string } }) =>
  body.token === "bridge-token"
    ? { payload: { uid: "jwt-user", sub: "jwt-user" } }
    : { payload: null },
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
  MCP_RESOURCE_URL: "https://edge.test/v1/mcp/sse",
  INTERNAL_ASSERTION_SECRET: "internal-secret",
  AUTH_DEV_ISSUER_SECRET: issuerSecret,
});

function profileEnv(row: Record<string, unknown> | null) {
  const first = vi.fn(async () => row);
  const bind = vi.fn(() => ({ first }));
  const prepare = vi.fn(() => ({ bind }));
  return {
    ...env("issuer-secret"),
    AUTH_DB: { prepare } as unknown as D1Database,
  };
}

function readyEnv(active = true) {
  let activeKey = active;
  const run = vi.fn(async () => ({ success: true }));
  const first = vi.fn(async () => (activeKey ? { id: "active-key" } : null));
  const bind = vi.fn(() => ({ first }));
  const prepare = vi.fn((query: string) =>
    query === "SELECT 1" || query === "SELECT id FROM oauthResource LIMIT 1"
      ? { run }
      : { bind },
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

function passwordUpgradeEnv(options: { failUpdate?: boolean } = {}) {
  const state = {
    password: "firebase-scrypt-v1$fingerprint$salt$hash$",
  };
  const statement = (query: string, values: unknown[] = []) => ({
    bind: (...nextValues: unknown[]) => statement(query, nextValues),
    all: vi.fn(async () => ({
      success: true,
      results: [
        {
          id: "credential-row",
          password: state.password,
        },
      ],
      meta: {},
    })),
    first: vi.fn(async () => ({
      id: "credential-row",
      password: state.password,
    })),
    run: vi.fn(async () => {
      if (options.failUpdate) throw new Error("D1 unavailable");
      const [replacement, , id, expected] = values;
      const changes =
        id === "credential-row" && expected === state.password ? 1 : 0;
      if (changes) state.password = String(replacement);
      return { success: true, meta: { changes } };
    }),
  });
  const prepare = vi.fn((query: string) => statement(query));
  return {
    environment: {
      ...env(),
      AUTH_DB: { prepare } as unknown as D1Database,
    },
    state,
    prepare,
  };
}

type AuthLifecycleState = {
  user: {
    id: string;
    name: string;
    email: string;
    createdAt: string;
  } | null;
  sessions: string[];
  accounts: string[];
  deletionVerifications: string[];
  oauthClients: string[];
  oauthAccessTokens: string[];
  oauthRefreshTokens: string[];
  oauthConsents: string[];
};

function lifecycleEnv(options: { failBatch?: boolean } = {}) {
  const state: AuthLifecycleState = {
    user: {
      id: "lifecycle-user",
      name: "Lifecycle User",
      email: "lifecycle@example.test",
      createdAt: "2026-08-29T00:00:00.000Z",
    },
    sessions: ["lifecycle-user", "lifecycle-user", "other-user"],
    accounts: ["lifecycle-user", "other-user"],
    deletionVerifications: ["lifecycle-user", "other-user"],
    oauthClients: ["lifecycle-user", "other-user"],
    oauthAccessTokens: ["lifecycle-user", "lifecycle-user", "other-user"],
    oauthRefreshTokens: ["lifecycle-user", "other-user"],
    oauthConsents: ["lifecycle-user", "other-user"],
  };

  const statement = (query: string, values: unknown[] = []) => {
    const normalized = query.replace(/\s+/g, " ").trim();
    const prepared = {
      bind: (...nextValues: unknown[]) => statement(query, nextValues),
      first: vi.fn(async () => {
        const uid = String(values[0] || "");
        if (
          normalized.startsWith("SELECT id, name, email, createdAt FROM user")
        ) {
          return state.user?.id === uid ? { ...state.user } : null;
        }
        if (normalized.includes("AS deletionVerifications")) {
          return {
            users: state.user?.id === uid ? 1 : 0,
            sessions: state.sessions.filter((value) => value === uid).length,
            accounts: state.accounts.filter((value) => value === uid).length,
            deletionVerifications: state.deletionVerifications.filter(
              (value) => value === uid,
            ).length,
            oauthClients: state.oauthClients.filter((value) => value === uid)
              .length,
            oauthAccessTokens: state.oauthAccessTokens.filter(
              (value) => value === uid,
            ).length,
            oauthRefreshTokens: state.oauthRefreshTokens.filter(
              (value) => value === uid,
            ).length,
            oauthConsents: state.oauthConsents.filter((value) => value === uid)
              .length,
          };
        }
        throw new Error(`unexpected first query: ${normalized}`);
      }),
      run: vi.fn(async () => {
        const uid = String(values[0] || "");
        if (normalized === "DELETE FROM verification WHERE value = ?") {
          state.deletionVerifications = state.deletionVerifications.filter(
            (value) => value !== uid,
          );
        } else if (
          normalized === "DELETE FROM oauthAccessToken WHERE userId = ?"
        ) {
          state.oauthAccessTokens = state.oauthAccessTokens.filter(
            (value) => value !== uid,
          );
        } else if (
          normalized === "DELETE FROM oauthRefreshToken WHERE userId = ?"
        ) {
          state.oauthRefreshTokens = state.oauthRefreshTokens.filter(
            (value) => value !== uid,
          );
        } else if (normalized === "DELETE FROM oauthConsent WHERE userId = ?") {
          state.oauthConsents = state.oauthConsents.filter(
            (value) => value !== uid,
          );
        } else if (normalized === "DELETE FROM oauthClient WHERE userId = ?") {
          state.oauthClients = state.oauthClients.filter(
            (value) => value !== uid,
          );
        } else if (normalized === "DELETE FROM session WHERE userId = ?") {
          state.sessions = state.sessions.filter((value) => value !== uid);
        } else if (normalized === "DELETE FROM account WHERE userId = ?") {
          state.accounts = state.accounts.filter((value) => value !== uid);
        } else if (normalized === "DELETE FROM user WHERE id = ?") {
          if (state.user?.id === uid) state.user = null;
        } else {
          throw new Error(`unexpected run query: ${normalized}`);
        }
        return { success: true, meta: { changes: 1 } };
      }),
    };
    return prepared;
  };

  const prepare = vi.fn((query: string) => statement(query));
  const batch = vi.fn(
    async (statements: Array<{ run(): Promise<unknown> }>) => {
      if (options.failBatch) throw new Error("batch unavailable");
      const snapshot = structuredClone(state);
      try {
        const results = [];
        for (const prepared of statements) results.push(await prepared.run());
        return results;
      } catch (error) {
        state.user = snapshot.user;
        state.sessions = snapshot.sessions;
        state.accounts = snapshot.accounts;
        state.deletionVerifications = snapshot.deletionVerifications;
        state.oauthClients = snapshot.oauthClients;
        state.oauthAccessTokens = snapshot.oauthAccessTokens;
        state.oauthRefreshTokens = snapshot.oauthRefreshTokens;
        state.oauthConsents = snapshot.oauthConsents;
        throw error;
      }
    },
  );

  return {
    environment: {
      ...env(),
      AUTH_DB: { prepare, batch } as unknown as D1Database,
    },
    state,
    batch,
  };
}

async function lifecycleHeaders(
  method: "GET" | "DELETE",
  path: string,
  uid = "lifecycle-user",
) {
  const signed = await createSignedAuthContext(
    { uid, authority: "better-auth", requestId: "lifecycle-request" },
    "auth",
    method,
    path,
    "internal-secret",
  );
  return {
    "x-omi-auth-context": signed?.encoded || "",
    "x-omi-internal-signature": signed?.signature || "",
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
    const response = await auth.fetch(
      new Request("https://auth.test/auth-issue", { method: "POST" }),
      env(),
    );
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
        headers: {
          "content-type": "application/json",
          ...(authorization ? { authorization } : {}),
        },
        body: JSON.stringify({ uid: "mobile-user" }),
      });

    expect((await auth.fetch(request(), env("issuer-secret"))).status).toBe(
      401,
    );
    expect(
      (await auth.fetch(request("Bearer wrong"), env("issuer-secret"))).status,
    ).toBe(401);
    expect(signJWT).not.toHaveBeenCalled();
  });

  it("validates the uid before asking Better Auth to mint a token", async () => {
    const response = await auth.fetch(
      new Request("https://auth.test/auth-issue", {
        method: "POST",
        headers: {
          authorization: "Bearer issuer-secret",
          "content-type": "application/json",
        },
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
        headers: {
          authorization: "Bearer issuer-secret",
          "content-type": "application/json",
        },
        body: JSON.stringify({ uid: "mobile-user" }),
      }),
      env("issuer-secret"),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      token: "jwt-from-workers",
      uid: "mobile-user",
    });
    expect(signJWT).toHaveBeenCalledWith({
      body: { payload: { uid: "mobile-user", sub: "mobile-user" } },
      headers: expect.any(Headers),
    });
  });

  it("verifies a server-issued JWT when there is no Better Auth database session", async () => {
    const jwtEnv = profileEnv({ createdAt: "2026-08-29T00:00:00.000Z" });
    const response = await auth.fetch(
      new Request("https://auth.test/internal/verify", {
        method: "POST",
        headers: {
          "x-internal-assertion-secret": "internal-secret",
          authorization: "Bearer bridge-token",
        },
      }),
      jwtEnv,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      uid: "jwt-user",
      authority: "better-auth",
      accountCreatedAt: 1_787_961_600,
      requestId: "internal",
    });
    expect(verifyJWT).toHaveBeenCalledWith({
      body: { token: "bridge-token" },
      headers: expect.any(Headers),
    });
  });

  it("verifies an httpOnly Better Auth session cookie", async () => {
    authHandler.mockResolvedValueOnce(
      Response.json({
        user: {
          id: "cookie-user",
          name: "Alice",
          createdAt: "2026-08-29T00:00:00.000Z",
        },
        session: { id: "session-1" },
      }),
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
      displayName: "Alice",
      accountCreatedAt: 1_787_961_600,
      requestId: "cookie-request",
    });
    const sessionRequest = authHandler.mock.calls[0][0] as Request;
    expect(new URL(sessionRequest.url).pathname).toBe(
      "/api/better-auth/get-session",
    );
    expect(sessionRequest.headers.get("cookie")).toBe(
      "__Secure-better-auth.session_token=cookie-session",
    );
    expect(sessionRequest.headers.get("x-internal-assertion-secret")).toBe(
      "internal-secret",
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
      emailAndPassword: {
        enabled: true,
        password: {
          hash: expect.any(Function),
          verify: expect.any(Function),
        },
      },
      rateLimit: {
        enabled: true,
        storage: "database",
        window: 60,
        max: 100,
        customRules: { "/get-session": expect.any(Function) },
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
      user: { deleteUser: { enabled: false } },
    });
    const getSessionRateLimit = options?.rateLimit?.customRules?.[
      "/get-session"
    ] as (request: Request) => false | { window: number; max: number };
    expect(
      getSessionRateLimit(
        new Request("https://auth.test/api/better-auth/get-session"),
      ),
    ).toEqual({ window: 60, max: 100 });
    expect(
      getSessionRateLimit(
        new Request("https://auth.test/api/better-auth/get-session", {
          headers: { "x-internal-assertion-secret": "internal-secret" },
        }),
      ),
    ).toBe(false);
    expect(
      getSessionRateLimit(
        new Request("https://auth.test/api/better-auth/get-session", {
          headers: { "x-internal-assertion-secret": "wrong" },
        }),
      ),
    ).toEqual({ window: 60, max: 100 });
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

  it("upgrades a migrated password only after a successful email sign-in", async () => {
    const upgrade = passwordUpgradeEnv();
    await auth.fetch(
      new Request("https://auth.test/api/better-auth/get-session"),
      upgrade.environment,
    );
    const options = vi.mocked(betterAuth).mock.calls.at(-1)?.[0];
    const after = options?.hooks?.after as unknown as (
      context: Record<string, unknown>,
    ) => Promise<unknown>;
    const nativeHash = vi.fn(async () => "native-better-auth-hash");

    await after({
      path: "/sign-in/email",
      body: { password: "verified-password" },
      headers: new Headers({ "x-request-id": "password-upgrade-request" }),
      context: {
        newSession: { user: { id: "firebase-user" } },
        password: { hash: nativeHash },
      },
    });

    expect(nativeHash).toHaveBeenCalledWith("verified-password");
    expect(upgrade.state.password).toBe("native-better-auth-hash");
  });

  it("does not inspect a password when email sign-in did not create a session", async () => {
    const upgrade = passwordUpgradeEnv();
    await auth.fetch(
      new Request("https://auth.test/api/better-auth/get-session"),
      upgrade.environment,
    );
    const options = vi.mocked(betterAuth).mock.calls.at(-1)?.[0];
    const after = options?.hooks?.after as unknown as (
      context: Record<string, unknown>,
    ) => Promise<unknown>;

    await after({
      path: "/sign-in/email",
      body: { password: "wrong-password" },
      context: {
        newSession: null,
        password: { hash: vi.fn() },
      },
    });

    expect(upgrade.prepare).not.toHaveBeenCalled();
  });

  it("records a bounded fallback and retries later when password upgrade persistence fails", async () => {
    const upgrade = passwordUpgradeEnv({ failUpdate: true });
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    await auth.fetch(
      new Request("https://auth.test/api/better-auth/get-session"),
      upgrade.environment,
    );
    const options = vi.mocked(betterAuth).mock.calls.at(-1)?.[0];
    const after = options?.hooks?.after as unknown as (
      context: Record<string, unknown>,
    ) => Promise<unknown>;

    await expect(
      after({
        path: "/sign-in/email",
        body: { password: "verified-password" },
        headers: new Headers({ "x-request-id": "password-upgrade-request" }),
        context: {
          newSession: { user: { id: "firebase-user" } },
          password: { hash: async () => "native-better-auth-hash" },
        },
      }),
    ).resolves.toBeUndefined();

    expect(upgrade.state.password).toMatch(/^firebase-scrypt-v1\$/);
    expect(warning).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(warning.mock.calls[0][0]))).toEqual({
      event: "fallback",
      component: "other",
      from: "d1",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "degraded",
      request_id: "password-upgrade-request",
    });
    warning.mockRestore();
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

  it("rejects unsigned and cross-user lifecycle requests", async () => {
    const lifecycle = lifecycleEnv();
    const unsigned = await auth.fetch(
      new Request("https://auth.test/internal/users/lifecycle-user/residual"),
      lifecycle.environment,
    );
    expect(unsigned.status).toBe(401);

    const path = "/internal/users/lifecycle-user";
    const crossUser = await auth.fetch(
      new Request(`https://auth.test${path}`, {
        headers: await lifecycleHeaders("DELETE", path, "other-user"),
        method: "DELETE",
      }),
      lifecycle.environment,
    );
    expect(crossUser.status).toBe(403);
    expect(lifecycle.batch).not.toHaveBeenCalled();
    expect(lifecycle.state.user?.id).toBe("lifecycle-user");
  });

  it("returns the signed user's identity and residual counts", async () => {
    const lifecycle = lifecycleEnv();
    const path = "/internal/users/lifecycle-user";
    const response = await auth.fetch(
      new Request(`https://auth.test${path}`, {
        headers: await lifecycleHeaders("GET", path),
      }),
      lifecycle.environment,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      uid: "lifecycle-user",
      name: "Lifecycle User",
      email: "lifecycle@example.test",
      created_at: "2026-08-29T00:00:00.000Z",
      residual: {
        users: 1,
        sessions: 2,
        accounts: 1,
        deletionVerifications: 1,
        oauthClients: 1,
        oauthAccessTokens: 2,
        oauthRefreshTokens: 1,
        oauthConsents: 1,
      },
    });
  });

  it("deletes Better Auth identity rows atomically and is replay-safe", async () => {
    const lifecycle = lifecycleEnv();
    const path = "/internal/users/lifecycle-user";
    const firstHeaders = await lifecycleHeaders("DELETE", path);
    const first = await auth.fetch(
      new Request(`https://auth.test${path}`, {
        headers: firstHeaders,
        method: "DELETE",
      }),
      lifecycle.environment,
    );
    expect(first.status).toBe(200);
    expect(await first.json()).toEqual({
      uid: "lifecycle-user",
      status: "deleted",
      before: {
        users: 1,
        sessions: 2,
        accounts: 1,
        deletionVerifications: 1,
        oauthClients: 1,
        oauthAccessTokens: 2,
        oauthRefreshTokens: 1,
        oauthConsents: 1,
      },
      residual: {
        users: 0,
        sessions: 0,
        accounts: 0,
        deletionVerifications: 0,
        oauthClients: 0,
        oauthAccessTokens: 0,
        oauthRefreshTokens: 0,
        oauthConsents: 0,
      },
    });
    expect(lifecycle.state).toEqual({
      user: null,
      sessions: ["other-user"],
      accounts: ["other-user"],
      deletionVerifications: ["other-user"],
      oauthClients: ["other-user"],
      oauthAccessTokens: ["other-user"],
      oauthRefreshTokens: ["other-user"],
      oauthConsents: ["other-user"],
    });

    const residualPath = `${path}/residual`;
    const residual = await auth.fetch(
      new Request(`https://auth.test${residualPath}`, {
        headers: await lifecycleHeaders("GET", residualPath),
      }),
      lifecycle.environment,
    );
    expect(residual.status).toBe(200);
    expect(await residual.json()).toEqual({
      uid: "lifecycle-user",
      empty: true,
      residual: {
        users: 0,
        sessions: 0,
        accounts: 0,
        deletionVerifications: 0,
        oauthClients: 0,
        oauthAccessTokens: 0,
        oauthRefreshTokens: 0,
        oauthConsents: 0,
      },
    });

    const secondHeaders = await lifecycleHeaders("DELETE", path);
    const second = await auth.fetch(
      new Request(`https://auth.test${path}`, {
        headers: secondHeaders,
        method: "DELETE",
      }),
      lifecycle.environment,
    );
    expect(second.status).toBe(200);
    expect(await second.json()).toMatchObject({
      uid: "lifecycle-user",
      status: "already_absent",
    });
    expect(lifecycle.batch).toHaveBeenCalledTimes(2);
  });

  it("fails closed when the Auth D1 deletion batch cannot commit", async () => {
    const lifecycle = lifecycleEnv({ failBatch: true });
    const path = "/internal/users/lifecycle-user";
    const response = await auth.fetch(
      new Request(`https://auth.test${path}`, {
        headers: await lifecycleHeaders("DELETE", path),
        method: "DELETE",
      }),
      lifecycle.environment,
    );

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: "identity_lifecycle_unavailable",
    });
    expect(lifecycle.state.user?.id).toBe("lifecycle-user");
    expect(lifecycle.state.sessions).toHaveLength(3);
  });
});
