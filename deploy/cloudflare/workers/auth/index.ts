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

app.post("/internal/verify", async (c) => {
  const expected = c.env.INTERNAL_ASSERTION_SECRET;
  if (!expected || c.req.header("x-internal-assertion-secret") !== expected) {
    return c.json({ error: "unauthorized" }, 401);
  }
  const authorization = c.req.header("authorization");
  if (!authorization) return c.json({ error: "unauthorized" }, 401);

  try {
    const auth = buildAuth(c.env, c.req.url);
    const baseURL = c.env.BETTER_AUTH_URL || new URL(c.req.url).origin;
    const sessionRequest = new Request(new URL("/api/auth/get-session", baseURL), {
      headers: { authorization, origin: baseURL },
    });
    const response = await auth.handler(sessionRequest);
    if (!response.ok) return c.json({ error: "unauthorized" }, 401);
    const body = (await response.json()) as { user?: { id?: string } };
    const uid = body.user?.id;
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
