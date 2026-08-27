import { Hono } from "hono";
import type { Context } from "hono";
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

const proxyPublicFirmware = async (c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>) => {
  const id = requestId(c.req.raw);
  const response = await c.env.API_CORE.fetch(new Request(c.req.raw, { headers: stripUntrustedHeaders(c.req.raw) }));
  return withRequestId(response, id);
};

app.get("/v2/firmware/stable", proxyPublicFirmware);
app.get("/v2/firmware/latest", proxyPublicFirmware);
app.get("/v2/firmware/version", proxyPublicFirmware);

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

app.all("/v1/cf/jobs", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.JOBS.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
});

const proxyAuthenticatedCore = async (c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.API_CORE.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
};

const proxyAuthenticatedAI = async (c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.API_AI.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
};

app.get("/v1/config/api-keys", proxyAuthenticatedCore);
app.all("/v1/cf/probe", proxyAuthenticatedCore);
app.all("/v1/cf/assets/*", proxyAuthenticatedCore);
app.all("/v1/users/transcription-preferences", proxyAuthenticatedCore);
app.all("/v1/users/available-languages", proxyAuthenticatedCore);
app.all("/v1/users/language", proxyAuthenticatedCore);
app.all("/v1/users/onboarding", proxyAuthenticatedCore);
app.get("/v1/users/store-recording-permission", proxyAuthenticatedCore);
app.post("/v1/users/store-recording-permission", proxyAuthenticatedCore);
app.get("/v1/users/private-cloud-sync", proxyAuthenticatedCore);
app.post("/v1/users/private-cloud-sync", proxyAuthenticatedCore);
app.get("/v1/users/notification-settings", proxyAuthenticatedCore);
app.patch("/v1/users/notification-settings", proxyAuthenticatedCore);
app.get("/v1/users/location-context-consent", proxyAuthenticatedCore);
app.put("/v1/users/location-context-consent", proxyAuthenticatedCore);
app.post("/v1/tts/synthesize", proxyAuthenticatedAI);
app.get("/v1/auto/model-pick", proxyAuthenticatedAI);
app.post("/v1/stt/transcribe-workers-ai", proxyAuthenticatedAI);

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
  if (envLegacy(c.env)) {
    const legacy = new URL(c.req.url);
    legacy.protocol = new URL(c.env.LEGACY_BACKEND_URL!).protocol;
    legacy.host = new URL(c.env.LEGACY_BACKEND_URL!).host;
    const response = await fetch(new Request(legacy, { method: c.req.method, headers, body: c.req.raw.body }));
    return withRequestId(response, id);
  }
  if (auth) return c.json({ error: "route not migrated" }, 404);
  return c.json({ error: "unauthorized" }, 401);
});

function envLegacy(env: EdgeEnv): env is EdgeEnv & { LEGACY_BACKEND_URL: string } {
  return typeof env.LEGACY_BACKEND_URL === "string" && env.LEGACY_BACKEND_URL.length > 0;
}

export default app;
