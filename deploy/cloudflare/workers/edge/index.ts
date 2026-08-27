import { Hono } from "hono";
import { cors } from "hono/cors";
import { requestId, withRequestId } from "../shared/request-id";
import { attachAuthContext, stripUntrustedHeaders, verifyBearer } from "./auth";
import type { EdgeEnv, EdgeVariables } from "./env";

const app = new Hono<{ Bindings: EdgeEnv; Variables: EdgeVariables }>();

app.use("*", async (c, next) => {
  const origins = (c.env.ALLOWED_ORIGINS || "").split(",").map((value) => value.trim()).filter(Boolean);
  if (!origins.length) return next();
  return cors({ origin: origins.length === 1 ? origins[0] : origins, credentials: true })(c, next);
});

app.get("/health", (c) => c.json({ status: "ok", service: "edge", version: "cf-00" }));

app.all("/api/auth/*", async (c) => {
  const id = requestId(c.req.raw);
  const response = await c.env.AUTH.fetch(new Request(c.req.raw, { headers: stripUntrustedHeaders(c.req.raw) }));
  return withRequestId(response, id);
});

app.all("/v2/voice-message/transcribe-stream", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.REALTIME.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
});

app.all("/v4/listen", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.REALTIME.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
});

app.all("/v4/web/listen", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.REALTIME.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
});

app.all("/v1/omni/relay", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.REALTIME.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
});

app.all("/*", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  const headers = stripUntrustedHeaders(c.req.raw);
  if (auth) await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);

  if (
    c.req.path.startsWith("/v1/ai/") ||
    c.req.path.startsWith("/v1/embeddings") ||
    c.req.path.startsWith("/v1/stt/")
  ) {
    const response = await c.env.API_AI.fetch(new Request(c.req.raw, { headers }));
    return withRequestId(response, id);
  }
  if (auth) {
    // Manifest owners for /v1/* (including /v1/cf/assets/*) are api-core.
    const response = await c.env.API_CORE.fetch(new Request(c.req.raw, { headers }));
    return withRequestId(response, id);
  }
  if (envLegacy(c.env)) {
    const legacy = new URL(c.req.url);
    legacy.protocol = new URL(c.env.LEGACY_BACKEND_URL!).protocol;
    legacy.host = new URL(c.env.LEGACY_BACKEND_URL!).host;
    const response = await fetch(new Request(legacy, { method: c.req.method, headers, body: c.req.raw.body }));
    return withRequestId(response, id);
  }
  return c.json({ error: "unauthorized" }, 401);
});

function envLegacy(env: EdgeEnv): env is EdgeEnv & { LEGACY_BACKEND_URL: string } {
  return typeof env.LEGACY_BACKEND_URL === "string" && env.LEGACY_BACKEND_URL.length > 0;
}

export default app;
