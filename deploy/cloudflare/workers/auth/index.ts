import { betterAuth } from "better-auth";
import { bearer } from "better-auth/plugins/bearer";
import { jwt } from "better-auth/plugins/jwt";
import { Hono } from "hono";
import { cors } from "hono/cors";
import {
  verifyRequestAuthContext,
  type AuthContext,
} from "../shared/auth-context";
import type { AuthEnv } from "./env";

const app = new Hono<{ Bindings: AuthEnv }>();
const AUTH_BASE_PATH = "/api/better-auth";
const JWT_ROTATION_INTERVAL_SECONDS = 30 * 24 * 60 * 60;
const JWT_GRACE_PERIOD_SECONDS = 2 * 24 * 60 * 60;

type SocialProviderId = "google" | "apple";

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
  return betterAuth({
    database: env.AUTH_DB,
    secret: env.BETTER_AUTH_SECRET,
    baseURL,
    basePath: AUTH_BASE_PATH,
    trustedOrigins: Array.from(new Set([baseURL, ...allowedOrigins])),
    emailAndPassword: { enabled: true },
    socialProviders,
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

function profileCreatedAt(value: unknown): string | null {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isFinite(value)) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  }
  return null;
}

app.use(`${AUTH_BASE_PATH}/*`, async (c, next) => {
  const allowed = origins(c.env);
  return cors({
    origin: (origin) => (allowed.includes(origin) ? origin : allowed[0] || ""),
    credentials: true,
  })(c, next);
});

app.get("/health", (c) =>
  c.json({ status: "ok", service: "auth", version: "cf-03" }),
);

app.get("/ready", async (c) => {
  try {
    await c.env.AUTH_DB.prepare("SELECT 1").run();
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
        user?: { id?: string; name?: string };
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
    const result: AuthContext = {
      uid,
      authority: "better-auth",
      requestId: c.req.header("x-request-id") || "internal",
    };
    return c.json(result);
  } catch {
    return c.json({ error: "unauthorized" }, 401);
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

app.on(
  ["GET", "POST", "PUT", "PATCH", "DELETE"],
  `${AUTH_BASE_PATH}/*`,
  (c) => {
    const auth = buildAuth(c.env, c.req.url);
    return auth.handler(c.req.raw);
  },
);

export default app;
