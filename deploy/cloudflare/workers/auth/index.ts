import { betterAuth } from "better-auth";
import { bearer } from "better-auth/plugins/bearer";
import { jwt } from "better-auth/plugins/jwt";
import { Hono } from "hono";
import { cors } from "hono/cors";
import type { AuthContext } from "../shared/auth-context";
import type { AuthEnv } from "./env";

const app = new Hono<{ Bindings: AuthEnv }>();

function origins(env: AuthEnv): string[] {
  return (env.ALLOWED_ORIGINS || "").split(",").map((value) => value.trim()).filter(Boolean);
}

function buildAuth(env: AuthEnv, requestUrl: string) {
  const allowedOrigins = origins(env);
  const requestOrigin = new URL(requestUrl).origin;
  const configuredBaseURL = env.BETTER_AUTH_URL ? new URL(env.BETTER_AUTH_URL).origin : null;
  const localFallback = /^(https?:\/\/localhost(?::\d+)?|https?:\/\/127\.0\.0\.1(?::\d+)?)$/.test(requestOrigin)
    ? requestOrigin
    : null;
  const baseURL = configuredBaseURL || localFallback;
  if (!baseURL) throw new Error("BETTER_AUTH_URL must be configured outside local development");
  return betterAuth({
    database: env.AUTH_DB,
    secret: env.BETTER_AUTH_SECRET,
    baseURL,
    trustedOrigins: Array.from(new Set([baseURL, ...allowedOrigins])),
    emailAndPassword: { enabled: true },
    plugins: [
      bearer(),
      jwt({
        jwt: {
          jwks: { keyPairConfig: { alg: "ES256" } },
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

app.use("/api/auth/*", async (c, next) => {
  const allowed = origins(c.env);
  return cors({
    origin: (origin) => (allowed.includes(origin) ? origin : allowed[0] || ""),
    credentials: true,
  })(c, next);
});

app.get("/health", (c) => c.json({ status: "ok", service: "auth", version: "cf-03" }));

app.get("/ready", async (c) => {
  try {
    await c.env.AUTH_DB.prepare("SELECT 1").run();
    return c.json({ status: "ready", database: "ok" });
  } catch {
    return c.json({ status: "not_ready", database: "error" }, 503);
  }
});

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
  const uid = typeof body === "object" && body !== null && "uid" in body ? (body as { uid?: unknown }).uid : null;
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
  if (!expected || c.req.header("x-internal-assertion-secret") !== expected) {
    return c.json({ error: "unauthorized" }, 401);
  }
  const authorization = c.req.header("authorization");
  if (!authorization) return c.json({ error: "unauthorized" }, 401);

  try {
    const auth = buildAuth(c.env, c.req.url);
    const token = bearerToken(c.req.raw);
    if (!token) return c.json({ error: "unauthorized" }, 401);
    const baseURL = c.env.BETTER_AUTH_URL || new URL(c.req.url).origin;
    const sessionRequest = new Request(new URL("/api/auth/get-session", baseURL), {
      headers: { authorization, origin: baseURL },
    });
    const response = await auth.handler(sessionRequest);
    if (response.ok) {
      const body = (await response.json()) as { user?: { id?: string } } | null;
      const sessionUid = body?.user?.id;
      if (sessionUid) {
        const result: AuthContext = {
          uid: sessionUid,
          authority: "better-auth",
          requestId: c.req.header("x-request-id") || "internal",
        };
        return c.json(result);
      }
    }

    // A JWT issued by the server-only Better Auth plugin is not a database
    // session, so `/get-session` correctly returns null for the dev bridge.
    // Verify its signature and issuer instead of treating that valid token as
    // anonymous traffic.
    const verified = await auth.api.verifyJWT({
      body: { token },
      headers: c.req.raw.headers,
    });
    const uid = payloadUid(verified?.payload);
    if (!uid) return c.json({ error: "unauthorized" }, 401);
    const result: AuthContext = { uid, authority: "better-auth", requestId: c.req.header("x-request-id") || "internal" };
    return c.json(result);
  } catch {
    return c.json({ error: "unauthorized" }, 401);
  }
});

app.on(["GET", "POST", "PUT", "PATCH", "DELETE"], "/api/auth/*", (c) => {
  const auth = buildAuth(c.env, c.req.url);
  return auth.handler(c.req.raw);
});

export default app;
