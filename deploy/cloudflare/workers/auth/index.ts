import { betterAuth } from "better-auth";
import { createMcpProtectedRequestHandler, mcp } from "@better-auth/mcp";
import { createAuthMiddleware } from "better-auth/api";
import { createDpopReplayStore } from "better-auth/oauth2";
import { bearer } from "better-auth/plugins/bearer";
import { jwt } from "better-auth/plugins/jwt";
import { Hono } from "hono";
import { cors } from "hono/cors";
import {
  verifyRequestAuthContext,
  type AuthContext,
} from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import type { AuthEnv } from "./env";
import {
  hashPassword,
  upgradeMigratedFirebasePassword,
  verifyPassword,
} from "./firebase-migration-password";

const app = new Hono<{ Bindings: AuthEnv }>();
const AUTH_BASE_PATH = "/api/better-auth";
const JWT_ROTATION_INTERVAL_SECONDS = 30 * 24 * 60 * 60;
const JWT_GRACE_PERIOD_SECONDS = 2 * 24 * 60 * 60;
export const MCP_SCOPES = [
  "action_items.read",
  "action_items.write",
  "chat.read",
  "conversations.read",
  "goals.read",
  "memories.read",
  "memories.write",
  "people.read",
  "screen_activity.read",
] as const;
const MCP_OAUTH_SCOPES = [...MCP_SCOPES, "offline_access"];
const MCP_OAUTH_SCOPE_SET = new Set(MCP_OAUTH_SCOPES);
const MCP_DATA_SCOPE_SET = new Set<string>(MCP_SCOPES);
const MAX_MCP_VERIFY_BODY_BYTES = 4_096;

type SocialProviderId = "google" | "apple";

type AuthIdentityResidual = {
  users: number;
  sessions: number;
  accounts: number;
  deletionVerifications: number;
  oauthClients: number;
  oauthAccessTokens: number;
  oauthRefreshTokens: number;
  oauthConsents: number;
};

function origins(env: AuthEnv): string[] {
  return (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

function configuredSocialProviders(env: AuthEnv) {
  const providers: {
    google?: { clientId: string; clientSecret: string };
    apple?: {
      clientId: string;
      clientSecret: string;
      appBundleIdentifier?: string;
    };
  } = {};
  if (env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET) {
    providers.google = {
      clientId: env.GOOGLE_CLIENT_ID,
      clientSecret: env.GOOGLE_CLIENT_SECRET,
    };
  }
  if (env.APPLE_CLIENT_ID && env.APPLE_CLIENT_SECRET) {
    providers.apple = {
      clientId: env.APPLE_CLIENT_ID,
      clientSecret: env.APPLE_CLIENT_SECRET,
      ...(env.APPLE_APP_BUNDLE_IDENTIFIER
        ? { appBundleIdentifier: env.APPLE_APP_BUNDLE_IDENTIFIER }
        : {}),
    };
  }
  return providers;
}

function configuredSocialProviderIds(env: AuthEnv): SocialProviderId[] {
  return Object.keys(configuredSocialProviders(env)) as SocialProviderId[];
}

function buildAuth(env: AuthEnv, requestUrl: string) {
  const allowedOrigins = origins(env);
  const requestOrigin = new URL(requestUrl).origin;
  const configuredBaseURL = env.BETTER_AUTH_URL
    ? new URL(env.BETTER_AUTH_URL).origin
    : null;
  const localFallback =
    /^(https?:\/\/localhost(?::\d+)?|https?:\/\/127\.0\.0\.1(?::\d+)?)$/.test(
      requestOrigin,
    )
      ? requestOrigin
      : null;
  const baseURL = configuredBaseURL || localFallback;
  if (!baseURL)
    throw new Error(
      "BETTER_AUTH_URL must be configured outside local development",
    );
  const socialProviders = configuredSocialProviders(env);
  const trustedProviders = Object.keys(socialProviders) as SocialProviderId[];
  const resource = env.MCP_RESOURCE_URL || new URL("/v1/mcp/sse", baseURL).href;
  return betterAuth({
    database: env.AUTH_DB,
    secret: env.BETTER_AUTH_SECRET,
    baseURL,
    basePath: AUTH_BASE_PATH,
    trustedOrigins: Array.from(new Set([baseURL, ...allowedOrigins])),
    emailAndPassword: {
      enabled: true,
      password: {
        hash: hashPassword,
        verify: (credentials) => verifyPassword(credentials, env),
      },
    },
    hooks: {
      after: createAuthMiddleware(async (ctx) => {
        if (ctx.path !== "/sign-in/email") return;
        const userId = ctx.context.newSession?.user.id;
        const password = ctx.body?.password;
        if (!userId || typeof password !== "string") return;

        try {
          await upgradeMigratedFirebasePassword(
            env.AUTH_DB,
            userId,
            password,
            ctx.context.password.hash,
          );
        } catch {
          // The password was already verified and the session already exists.
          // Keep the migrated credential usable and retry on the next login.
          recordFallback({
            component: "other",
            from: "d1",
            to: "none",
            reason: "dependency_unavailable",
            outcome: "degraded",
            requestId: ctx.headers?.get("x-request-id") || undefined,
          });
        }
      }),
    },
    socialProviders,
    user: {
      // Public self-service deletion stays closed until Jobs has removed and
      // residual-checked every product-data namespace for the uid.
      deleteUser: { enabled: false },
    },
    account: {
      encryptOAuthTokens: true,
      storeStateStrategy: "database",
      accountLinking: {
        enabled: true,
        disableImplicitLinking: true,
        trustedProviders,
        allowDifferentEmails: false,
      },
    },
    rateLimit: {
      enabled: true,
      storage: "database",
      window: 60,
      max: 100,
      customRules: {
        "/get-session": (request) =>
          env.INTERNAL_ASSERTION_SECRET &&
          constantTimeEqual(
            request.headers.get("x-internal-assertion-secret") || "",
            env.INTERNAL_ASSERTION_SECRET,
          )
            ? false
            : { window: 60, max: 100 },
      },
    },
    advanced: {
      ipAddress: { ipAddressHeaders: ["cf-connecting-ip"] },
      useSecureCookies: baseURL.startsWith("https://"),
    },
    plugins: [
      bearer(),
      jwt({
        jwt: {
          jwks: {
            keyPairConfig: { alg: "ES256" },
            rotationInterval: JWT_ROTATION_INTERVAL_SECONDS,
            gracePeriod: JWT_GRACE_PERIOD_SECONDS,
          },
          expirationTime: "24h",
        },
      }),
      mcp({
        loginPage: "/login",
        consentPage: "/mcp/consent",
        resource,
        scopes: MCP_OAUTH_SCOPES,
        grantTypes: ["authorization_code", "refresh_token"],
        accessTokenExpiresIn: 60 * 60,
        refreshTokenExpiresIn: 30 * 24 * 60 * 60,
        allowPublicClientPrelogin: true,
        allowDynamicClientRegistration: true,
        allowUnauthenticatedClientRegistration: true,
        clientRegistrationRequirePKCE: true,
      }),
    ],
  });
}

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let difference = leftBytes.length ^ rightBytes.length;
  const maxLength = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < maxLength; index++) {
    difference |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return difference === 0;
}

function bearerToken(request: Request): string | null {
  const authorization = request.headers.get("authorization");
  if (!authorization) return null;
  const match = /^Bearer\s+(.+)$/i.exec(authorization);
  return match?.[1] || null;
}

function payloadUid(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const uid = (payload as { uid?: unknown }).uid;
  if (typeof uid === "string" && uid.length > 0) return uid;
  const subject = (payload as { sub?: unknown }).sub;
  return typeof subject === "string" && subject.length > 0 ? subject : null;
}

function verifiedMcpClaims(payload: unknown): {
  uid: string;
  scopes: string[];
  clientId: string;
} | null {
  if (!payload || typeof payload !== "object") return null;
  const claims = payload as Record<string, unknown>;
  const uid = claims.sub;
  const clientId = claims.client_id;
  const authorizedParty = claims.azp;
  const rawScope = claims.scope;
  if (
    typeof uid !== "string" ||
    uid.length === 0 ||
    uid.length > 256 ||
    typeof clientId !== "string" ||
    clientId.length === 0 ||
    clientId.length > 2_048 ||
    (authorizedParty !== undefined && authorizedParty !== clientId) ||
    typeof rawScope !== "string" ||
    rawScope.length > 4_096
  ) {
    return null;
  }
  const granted = rawScope.split(/\s+/).filter(Boolean);
  if (
    granted.length > MCP_OAUTH_SCOPES.length ||
    granted.length !== new Set(granted).size ||
    granted.some((scope) => !MCP_OAUTH_SCOPE_SET.has(scope))
  ) {
    return null;
  }
  return {
    uid,
    clientId,
    scopes: granted.filter((scope) => MCP_DATA_SCOPE_SET.has(scope)),
  };
}

function profileCreatedAt(value: unknown): string | null {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isFinite(value)) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  }
  return null;
}

function authContextCreatedAt(value: unknown): number | undefined {
  const normalized = profileCreatedAt(value);
  if (!normalized) return undefined;
  const milliseconds = Date.parse(normalized);
  return Number.isFinite(milliseconds) && milliseconds > 0
    ? Math.floor(milliseconds / 1000)
    : undefined;
}

function lifecycleUid(value: string): string | null {
  return value.length > 0 && value.length <= 256 && !value.includes("/")
    ? value
    : null;
}

function databaseCount(value: unknown): number {
  const count = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(count) || count < 0) {
    throw new Error("invalid identity residual count");
  }
  return count;
}

async function authIdentityResidual(
  database: D1Database,
  uid: string,
): Promise<AuthIdentityResidual> {
  const row = await database
    .prepare(
      `SELECT
         (SELECT COUNT(*) FROM user WHERE id = ?) AS users,
         (SELECT COUNT(*) FROM session WHERE userId = ?) AS sessions,
         (SELECT COUNT(*) FROM account WHERE userId = ?) AS accounts,
         (SELECT COUNT(*) FROM verification WHERE value = ?) AS deletionVerifications,
         (SELECT COUNT(*) FROM oauthClient WHERE userId = ?) AS oauthClients,
         (SELECT COUNT(*) FROM oauthAccessToken WHERE userId = ?) AS oauthAccessTokens,
         (SELECT COUNT(*) FROM oauthRefreshToken WHERE userId = ?) AS oauthRefreshTokens,
         (SELECT COUNT(*) FROM oauthConsent WHERE userId = ?) AS oauthConsents`,
    )
    .bind(uid, uid, uid, uid, uid, uid, uid, uid)
    .first<Record<string, unknown>>();
  if (!row) throw new Error("identity residual query returned no row");
  return {
    users: databaseCount(row.users),
    sessions: databaseCount(row.sessions),
    accounts: databaseCount(row.accounts),
    deletionVerifications: databaseCount(row.deletionVerifications),
    oauthClients: databaseCount(row.oauthClients),
    oauthAccessTokens: databaseCount(row.oauthAccessTokens),
    oauthRefreshTokens: databaseCount(row.oauthRefreshTokens),
    oauthConsents: databaseCount(row.oauthConsents),
  };
}

function identityResidualEmpty(residual: AuthIdentityResidual): boolean {
  return Object.values(residual).every((count) => count === 0);
}

app.use(`${AUTH_BASE_PATH}/*`, async (c, next) => {
  const allowed = origins(c.env);
  return cors({
    origin: (origin) => (allowed.includes(origin) ? origin : allowed[0] || ""),
    credentials: true,
  })(c, next);
});

app.get("/health", (c) =>
  c.json({ status: "ok", service: "auth", version: "cf-04" }),
);

app.get("/ready", async (c) => {
  try {
    await c.env.AUTH_DB.prepare("SELECT 1").run();
    await c.env.AUTH_DB.prepare("SELECT id FROM oauthResource LIMIT 1").run();
    const findActiveKey = () =>
      c.env.AUTH_DB.prepare(
        `SELECT id FROM jwks
         WHERE expiresAt IS NULL
            OR (typeof(expiresAt) IN ('integer', 'real') AND expiresAt > ?)
            OR (typeof(expiresAt) = 'text' AND datetime(expiresAt) > datetime('now'))
         LIMIT 1`,
      )
        .bind(Date.now())
        .first<{ id: string }>();
    let activeKey = await findActiveKey();
    if (!activeKey) {
      const auth = buildAuth(c.env, c.req.url);
      await auth.api.signJWT({
        body: { payload: { sub: "jwks-readiness-bootstrap" } },
        headers: c.req.raw.headers,
      });
      activeKey = await findActiveKey();
    }
    if (!activeKey)
      return c.json(
        { status: "not_ready", database: "ok", signing_key: "missing" },
        503,
      );
    return c.json({ status: "ready", database: "ok", signing_key: "ok" });
  } catch {
    return c.json(
      { status: "not_ready", database: "error", signing_key: "error" },
      503,
    );
  }
});

app.get(`${AUTH_BASE_PATH}/omi-capabilities`, (c) =>
  c.json({
    social_providers: configuredSocialProviderIds(c.env),
    explicit_account_linking: true,
    implicit_account_linking: false,
  }),
);

// This endpoint exists only for the non-release Flutter staging bridge. It
// mints a normal Better Auth JWT after proving possession of a secret that is
// never checked into the repository or exposed to production clients.
app.post("/auth-issue", async (c) => {
  const issuerSecret = c.env.AUTH_DEV_ISSUER_SECRET;
  if (!issuerSecret) return c.json({ error: "not_found" }, 404);
  const presented = bearerToken(c.req.raw);
  if (!presented || !constantTimeEqual(presented, issuerSecret)) {
    return c.json({ error: "unauthorized" }, 401);
  }

  let body: unknown;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "invalid_request" }, 400);
  }
  const uid =
    typeof body === "object" && body !== null && "uid" in body
      ? (body as { uid?: unknown }).uid
      : null;
  if (typeof uid !== "string" || uid.trim().length === 0 || uid.length > 256) {
    return c.json({ error: "invalid_request" }, 400);
  }

  try {
    const auth = buildAuth(c.env, c.req.url);
    const result = await auth.api.signJWT({
      body: { payload: { uid, sub: uid } },
      headers: c.req.raw.headers,
    });
    return c.json({ ...result, uid });
  } catch {
    return c.json({ error: "issuer_unavailable" }, 502);
  }
});

app.post("/internal/verify", async (c) => {
  const expected = c.env.INTERNAL_ASSERTION_SECRET;
  const presentedSecret = c.req.header("x-internal-assertion-secret") || "";
  if (!expected || !constantTimeEqual(presentedSecret, expected)) {
    return c.json({ error: "unauthorized" }, 401);
  }
  const authorization = c.req.header("authorization");
  const cookie = c.req.header("cookie");
  if (!authorization && !cookie) return c.json({ error: "unauthorized" }, 401);

  try {
    const auth = buildAuth(c.env, c.req.url);
    const token = bearerToken(c.req.raw);
    const baseURL = c.env.BETTER_AUTH_URL || new URL(c.req.url).origin;
    const sessionHeaders = new Headers({ origin: baseURL });
    sessionHeaders.set("x-internal-assertion-secret", expected);
    if (authorization) sessionHeaders.set("authorization", authorization);
    if (cookie) sessionHeaders.set("cookie", cookie);
    const sessionRequest = new Request(
      new URL(`${AUTH_BASE_PATH}/get-session`, baseURL),
      {
        headers: sessionHeaders,
      },
    );
    const response = await auth.handler(sessionRequest);
    if (response.ok) {
      const body = (await response.json()) as {
        user?: { id?: string; name?: string; createdAt?: unknown };
      } | null;
      const sessionUid = body?.user?.id;
      if (sessionUid) {
        const result: AuthContext = {
          uid: sessionUid,
          authority: "better-auth",
          displayName:
            typeof body?.user?.name === "string" && body.user.name.trim()
              ? body.user.name.trim().slice(0, 120)
              : undefined,
          accountCreatedAt: authContextCreatedAt(body?.user?.createdAt),
          requestId: c.req.header("x-request-id") || "internal",
        };
        return c.json(result);
      }
    }

    // A JWT issued by the server-only Better Auth plugin is not a database
    // session, so `/get-session` correctly returns null for the dev bridge.
    // Verify its signature and issuer instead of treating that valid token as
    // anonymous traffic.
    if (!token) return c.json({ error: "unauthorized" }, 401);
    const verified = await auth.api.verifyJWT({
      body: { token },
      headers: c.req.raw.headers,
    });
    const uid = payloadUid(verified?.payload);
    if (!uid) return c.json({ error: "unauthorized" }, 401);
    let accountCreatedAt: number | undefined;
    try {
      const user = await c.env.AUTH_DB.prepare(
        "SELECT createdAt FROM user WHERE id = ?",
      )
        .bind(uid)
        .first<{ createdAt?: unknown }>();
      accountCreatedAt = authContextCreatedAt(user?.createdAt);
    } catch {
      // Creation time is a fail-open trial input, never an auth prerequisite.
    }
    const result: AuthContext = {
      uid,
      authority: "better-auth",
      accountCreatedAt,
      requestId: c.req.header("x-request-id") || "internal",
    };
    return c.json(result);
  } catch {
    return c.json({ error: "unauthorized" }, 401);
  }
});

app.post("/internal/mcp/verify", async (c) => {
  const expected = c.env.INTERNAL_ASSERTION_SECRET;
  const presentedSecret = c.req.header("x-internal-assertion-secret") || "";
  if (!expected || !constantTimeEqual(presentedSecret, expected)) {
    return c.json({ error: "unauthorized" }, 401);
  }
  const contentLength = Number(c.req.header("content-length") || "0");
  if (
    !Number.isFinite(contentLength) ||
    contentLength < 0 ||
    contentLength > MAX_MCP_VERIFY_BODY_BYTES
  ) {
    return c.json({ error: "invalid_request" }, 400);
  }

  let body: unknown;
  try {
    const raw = await c.req.text();
    if (new TextEncoder().encode(raw).length > MAX_MCP_VERIFY_BODY_BYTES) {
      return c.json({ error: "invalid_request" }, 400);
    }
    body = JSON.parse(raw);
  } catch {
    return c.json({ error: "invalid_request" }, 400);
  }
  const method =
    body && typeof body === "object" && "method" in body
      ? (body as { method?: unknown }).method
      : null;
  const url =
    body && typeof body === "object" && "url" in body
      ? (body as { url?: unknown }).url
      : null;
  const baseURL = c.env.BETTER_AUTH_URL || new URL(c.req.url).origin;
  const resource =
    c.env.MCP_RESOURCE_URL || new URL("/v1/mcp/sse", baseURL).href;
  if (!["POST", "GET", "DELETE"].includes(String(method)) || url !== resource) {
    return c.json({ error: "invalid_request" }, 400);
  }
  const authorization = c.req.header("authorization");
  if (!authorization) return c.json({ error: "unauthorized" }, 401);

  try {
    const publicHeaders = new Headers({ authorization });
    const dpop = c.req.header("dpop");
    if (dpop) publicHeaders.set("dpop", dpop);
    const publicRequest = new Request(resource, {
      method: String(method),
      headers: publicHeaders,
    });
    const auth = buildAuth(c.env, c.req.url);
    const { baseURL: issuer, internalAdapter } = await auth.$context;
    if (!issuer) throw new Error("Better Auth issuer is unavailable");
    const verify = createMcpProtectedRequestHandler(
      {
        issuer,
        audience: resource,
        challengeScopes: MCP_SCOPES,
        // Better Auth's verifier accepts a function source internally. Keep
        // JWKS resolution inside this Worker so Cloudflare never has to make a
        // same-account public Worker fetch, which is rejected with error 1042.
        jwksUrl: (async () => await auth.api.getJwks()) as unknown as string,
        dpop: { replayStore: createDpopReplayStore(internalAdapter) },
      },
      async (_request, claims) => {
        const identity = verifiedMcpClaims(claims);
        if (!identity) {
          return Response.json({ error: "invalid_token" }, { status: 401 });
        }
        return Response.json(
          {
            uid: identity.uid,
            scopes: identity.scopes,
            clientId: identity.clientId,
          },
          { headers: { "cache-control": "no-store" } },
        );
      },
    );
    return await verify(publicRequest);
  } catch {
    return c.json({ error: "authorization_unavailable" }, 503);
  }
});

// The Edge has already authenticated the caller and forwards a signed context.
// Keep the identity read beside Better Auth's D1 tables instead of giving the
// Python API worker a write-capable binding to the auth database.
app.get("/internal/profile", async (c) => {
  const context = await verifyRequestAuthContext(
    c.req.raw,
    "auth",
    c.env.INTERNAL_ASSERTION_SECRET,
  );
  if (!context) return c.json({ error: "unauthorized" }, 401);

  try {
    const row = await c.env.AUTH_DB.prepare(
      "SELECT id, name, email, createdAt FROM user WHERE id = ?",
    )
      .bind(context.uid)
      .first<{
        id?: unknown;
        name?: unknown;
        email?: unknown;
        createdAt?: unknown;
      }>();
    if (!row) return c.json({ detail: "User not found" }, 410);

    const uid =
      typeof row.id === "string" && row.id.length > 0 ? row.id : context.uid;
    return c.json({
      uid,
      email: typeof row.email === "string" ? row.email : null,
      name: typeof row.name === "string" ? row.name : null,
      created_at: profileCreatedAt(row.createdAt),
    });
  } catch {
    return c.json({ error: "profile_unavailable" }, 503);
  }
});

// Product-data deletion is orchestrated by the Jobs Worker. These private
// endpoints give that workflow an idempotent Auth-D1 boundary without enabling
// Better Auth's public /delete-user route before the wider residual workflow is
// ready. Every request is bound to the caller's uid, method, path, and the Auth
// service audience by the signed internal context.
app.get("/internal/users/:uid/residual", async (c) => {
  const uid = lifecycleUid(c.req.param("uid"));
  if (!uid) return c.json({ error: "invalid_request" }, 400);
  const context = await verifyRequestAuthContext(
    c.req.raw,
    "auth",
    c.env.INTERNAL_ASSERTION_SECRET,
  );
  if (!context) return c.json({ error: "unauthorized" }, 401);
  if (context.uid !== uid) return c.json({ error: "forbidden" }, 403);

  try {
    const residual = await authIdentityResidual(c.env.AUTH_DB, uid);
    return c.json({ uid, empty: identityResidualEmpty(residual), residual });
  } catch {
    return c.json({ error: "identity_lifecycle_unavailable" }, 503);
  }
});

app.get("/internal/users/:uid", async (c) => {
  const uid = lifecycleUid(c.req.param("uid"));
  if (!uid) return c.json({ error: "invalid_request" }, 400);
  const context = await verifyRequestAuthContext(
    c.req.raw,
    "auth",
    c.env.INTERNAL_ASSERTION_SECRET,
  );
  if (!context) return c.json({ error: "unauthorized" }, 401);
  if (context.uid !== uid) return c.json({ error: "forbidden" }, 403);

  try {
    const row = await c.env.AUTH_DB.prepare(
      "SELECT id, name, email, createdAt FROM user WHERE id = ?",
    )
      .bind(uid)
      .first<{
        id?: unknown;
        name?: unknown;
        email?: unknown;
        createdAt?: unknown;
      }>();
    const residual = await authIdentityResidual(c.env.AUTH_DB, uid);
    if (!row) {
      return c.json({ detail: "User not found", uid, residual }, 404);
    }
    return c.json({
      uid,
      email: typeof row.email === "string" ? row.email : null,
      name: typeof row.name === "string" ? row.name : null,
      created_at: profileCreatedAt(row.createdAt),
      residual,
    });
  } catch {
    return c.json({ error: "identity_lifecycle_unavailable" }, 503);
  }
});

app.delete("/internal/users/:uid", async (c) => {
  const uid = lifecycleUid(c.req.param("uid"));
  if (!uid) return c.json({ error: "invalid_request" }, 400);
  const context = await verifyRequestAuthContext(
    c.req.raw,
    "auth",
    c.env.INTERNAL_ASSERTION_SECRET,
  );
  if (!context) return c.json({ error: "unauthorized" }, 401);
  if (context.uid !== uid) return c.json({ error: "forbidden" }, 403);

  try {
    const before = await authIdentityResidual(c.env.AUTH_DB, uid);
    await c.env.AUTH_DB.batch([
      c.env.AUTH_DB.prepare("DELETE FROM verification WHERE value = ?").bind(
        uid,
      ),
      c.env.AUTH_DB.prepare(
        "DELETE FROM oauthAccessToken WHERE userId = ?",
      ).bind(uid),
      c.env.AUTH_DB.prepare(
        "DELETE FROM oauthRefreshToken WHERE userId = ?",
      ).bind(uid),
      c.env.AUTH_DB.prepare("DELETE FROM oauthConsent WHERE userId = ?").bind(
        uid,
      ),
      c.env.AUTH_DB.prepare("DELETE FROM oauthClient WHERE userId = ?").bind(
        uid,
      ),
      c.env.AUTH_DB.prepare("DELETE FROM session WHERE userId = ?").bind(uid),
      c.env.AUTH_DB.prepare("DELETE FROM account WHERE userId = ?").bind(uid),
      c.env.AUTH_DB.prepare("DELETE FROM user WHERE id = ?").bind(uid),
    ]);
    const residual = await authIdentityResidual(c.env.AUTH_DB, uid);
    if (!identityResidualEmpty(residual)) {
      return c.json({ error: "identity_residual", uid, residual }, 503);
    }
    return c.json({
      uid,
      status: identityResidualEmpty(before) ? "already_absent" : "deleted",
      before,
      residual,
    });
  } catch {
    return c.json({ error: "identity_lifecycle_unavailable" }, 503);
  }
});

app.on(
  ["GET", "POST", "PUT", "PATCH", "DELETE"],
  `${AUTH_BASE_PATH}/*`,
  (c) => {
    const auth = buildAuth(c.env, c.req.url);
    return auth.handler(c.req.raw);
  },
);

export default app;
