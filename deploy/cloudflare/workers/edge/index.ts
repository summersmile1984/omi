import { Hono } from "hono";
import type { Context } from "hono";
import { cors } from "hono/cors";
import { requestId, withRequestId } from "../shared/request-id";
import { attachAuthContext, stripUntrustedHeaders, verifyBearer } from "./auth";
import type { EdgeEnv, EdgeVariables } from "./env";

const app = new Hono<{ Bindings: EdgeEnv; Variables: EdgeVariables }>();
const MAX_ASYNC_TRANSCRIPTION_AUDIO_BYTES = 5_000_000;

app.use("*", async (c, next) => {
  const origins = (c.env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (!origins.length) return next();
  return cors({
    origin: origins.length === 1 ? origins[0] : origins,
    credentials: true,
  })(c, next);
});

app.get("/health", (c) =>
  c.json({ status: "ok", service: "edge", version: "cf-00" }),
);

const proxyPublicCore = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const response = await c.env.API_CORE.fetch(
    new Request(c.req.raw, { headers: stripUntrustedHeaders(c.req.raw) }),
  );
  return withRequestId(response, id);
};

const proxyPublicFirmware = proxyPublicCore;

app.get("/v2/firmware/stable", proxyPublicFirmware);
app.get("/v2/firmware/latest", proxyPublicFirmware);
app.get("/v2/firmware/version", proxyPublicFirmware);
app.get("/v1/announcements/changelogs", proxyPublicCore);
app.get("/v1/announcements/features", proxyPublicCore);
app.get("/v1/announcements/general", proxyPublicCore);
app.get("/v1/announcements/all", proxyPublicCore);
app.get("/v1/announcements/:announcementId", proxyPublicCore);
app.post("/v1/announcements", proxyPublicCore);
app.put("/v1/announcements/:announcementId", proxyPublicCore);
app.delete("/v1/announcements/:announcementId", proxyPublicCore);

app.all("/api/auth/*", async (c) => {
  const id = requestId(c.req.raw);
  const response = await c.env.AUTH.fetch(
    new Request(c.req.raw, { headers: stripUntrustedHeaders(c.req.raw) }),
  );
  return withRequestId(response, id);
});

app.all("/v2/voice-message/transcribe-stream", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.REALTIME.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
});

app.all("/v4/listen", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.REALTIME.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
});

app.all("/v4/web/listen", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.REALTIME.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
});

app.all("/v1/omni/relay", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.REALTIME.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
});

const proxyAuthenticatedJobs = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.JOBS.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
};

const proxyAuthenticatedAsyncTranscription = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = new Headers();
  for (const name of ["content-type", "content-length", "idempotency-key"]) {
    const value = c.req.header(name);
    if (value) headers.set(name, value);
  }
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const declaredLength = Number(c.req.header("content-length"));
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_ASYNC_TRANSCRIPTION_AUDIO_BYTES
  ) {
    return c.json({ error: "audio body too large" }, 413);
  }
  const body = await c.req.raw.arrayBuffer();
  if (body.byteLength > MAX_ASYNC_TRANSCRIPTION_AUDIO_BYTES) {
    return c.json({ error: "audio body too large" }, 413);
  }
  const target = new URL("/v1/cf/transcription-jobs", c.req.url);
  const response = await c.env.JOBS.fetch(
    new Request(target, { method: "POST", headers, body }),
  );
  return withRequestId(response, id);
};

const proxyAuthenticatedAsyncTranscriptionStatus = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = new Headers();
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const jobId = encodeURIComponent(c.req.param("jobId") || "");
  const target = new URL(`/v1/cf/transcription-jobs/${jobId}`, c.req.url);
  const response = await c.env.JOBS.fetch(
    new Request(target, { method: "GET", headers }),
  );
  return withRequestId(response, id);
};

app.all("/v1/cf/jobs", proxyAuthenticatedJobs);
app.get("/v1/cf/jobs/:jobId", proxyAuthenticatedJobs);

const proxyAuthenticatedCore = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.API_CORE.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
};

const proxyAuthenticatedAuthProfile = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const target = new URL(c.req.url);
  target.protocol = "https:";
  target.host = "auth.internal";
  target.pathname = "/internal/profile";
  const response = await c.env.AUTH.fetch(
    new Request(target, { method: "GET", headers }),
  );
  return withRequestId(response, id);
};

const proxyAuthenticatedAI = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);
  const response = await c.env.API_AI.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
};

app.get("/v1/config/api-keys", proxyAuthenticatedCore);
app.post("/v2/realtime/session", proxyAuthenticatedAI);
app.post("/v2/realtime/usage", proxyAuthenticatedAI);
app.post("/v1/stt/transcribe-async", proxyAuthenticatedAsyncTranscription);
app.get(
  "/v1/stt/transcribe-async/:jobId",
  proxyAuthenticatedAsyncTranscriptionStatus,
);
app.post("/v1/embeddings-workers-ai", proxyAuthenticatedAI);
app.get("/v1/account/cutover/control", proxyAuthenticatedCore);
app.all("/v1/cf/probe", proxyAuthenticatedCore);
app.all("/v1/cf/assets/*", proxyAuthenticatedCore);
app.get("/v1/conversations", proxyAuthenticatedCore);
app.get("/v1/conversations/count", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/photos", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/transcripts", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/analytics", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/recording", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/events", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/summary", proxyAuthenticatedCore);
app.delete(
  "/v1/conversations/:conversationId/calendar-event",
  proxyAuthenticatedCore,
);
app.patch("/v1/conversations/:conversationId/segments/text", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/title", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/starred", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/action-items", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/action-items/count", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/action-items", proxyAuthenticatedCore);
app.delete("/v1/conversations/:conversationId/action-items", proxyAuthenticatedCore);
app.patch(
  "/v1/conversations/:conversationId/action-items/:actionItemIdx",
  proxyAuthenticatedCore,
);
app.post("/v1/cf/conversations", proxyAuthenticatedCore);
app.get("/v1/cf/conversations", proxyAuthenticatedCore);
app.get("/v1/cf/conversations/count", proxyAuthenticatedCore);
app.get("/v1/cf/conversations/:conversationId", proxyAuthenticatedCore);
app.patch("/v1/cf/conversations/:conversationId/title", proxyAuthenticatedCore);
app.patch("/v1/cf/conversations/:conversationId/starred", proxyAuthenticatedCore);
app.all("/v1/users/transcription-preferences", proxyAuthenticatedCore);
app.all("/v1/users/available-languages", proxyAuthenticatedCore);
app.all("/v1/users/language", proxyAuthenticatedCore);
app.all("/v1/users/onboarding", proxyAuthenticatedCore);
app.get("/v1/users/store-recording-permission", proxyAuthenticatedCore);
app.post("/v1/users/store-recording-permission", proxyAuthenticatedCore);
app.get("/v1/users/private-cloud-sync", proxyAuthenticatedCore);
app.post("/v1/users/private-cloud-sync", proxyAuthenticatedCore);
app.get("/v1/users/training-data-opt-in", proxyAuthenticatedCore);
app.post("/v1/users/training-data-opt-in", proxyAuthenticatedCore);
app.post("/v1/users/fcm-token", proxyAuthenticatedCore);
app.patch("/v1/users/geolocation", proxyAuthenticatedCore);
app.get("/v1/announcements/pending", proxyAuthenticatedCore);
app.post("/v1/announcements/:announcementId/dismiss", proxyAuthenticatedCore);
app.get("/v1/action-items", proxyAuthenticatedCore);
app.post("/v1/action-items", proxyAuthenticatedCore);
app.get("/v1/action-items/ids", proxyAuthenticatedCore);
app.patch("/v1/action-items/batch", proxyAuthenticatedCore);
app.post("/v1/action-items/batch", proxyAuthenticatedCore);
app.post("/v1/action-items/batch-delete", proxyAuthenticatedCore);
app.get("/v1/action-items/pending-sync", proxyAuthenticatedCore);
app.patch("/v1/action-items/sync-batch", proxyAuthenticatedCore);
app.get("/v1/daily-score", proxyAuthenticatedCore);
app.get("/v1/scores", proxyAuthenticatedCore);
app.post("/v1/focus-sessions", proxyAuthenticatedCore);
app.get("/v1/focus-sessions", proxyAuthenticatedCore);
app.delete("/v1/focus-sessions/:sessionId", proxyAuthenticatedCore);
app.get("/v1/focus-stats", proxyAuthenticatedCore);
app.post("/v1/screen-activity/sync", proxyAuthenticatedCore);
app.get("/v1/screen-activity", proxyAuthenticatedCore);
app.get("/v1/screen-activity/summary", proxyAuthenticatedCore);
app.get("/v1/calendar/onboarding/status", proxyAuthenticatedCore);
app.post("/v1/calendar/onboarding/skip", proxyAuthenticatedCore);
app.post("/v1/calendar/onboarding/reset", proxyAuthenticatedCore);
app.post("/v1/calendar/meetings", proxyAuthenticatedCore);
app.get("/v1/calendar/meetings", proxyAuthenticatedCore);
app.get("/v1/calendar/meetings/:meetingId", proxyAuthenticatedCore);
app.get("/v1/action-items/:actionItemId", proxyAuthenticatedCore);
app.patch("/v1/action-items/:actionItemId", proxyAuthenticatedCore);
app.patch("/v1/action-items/:actionItemId/completed", proxyAuthenticatedCore);
app.delete("/v1/action-items/:actionItemId", proxyAuthenticatedCore);
app.post("/v1/users/people", proxyAuthenticatedCore);
app.get("/v1/users/people", proxyAuthenticatedCore);
app.get("/v1/users/people/:personId", proxyAuthenticatedCore);
app.patch("/v1/users/people/:personId/name", proxyAuthenticatedCore);
app.delete("/v1/users/people/:personId", proxyAuthenticatedCore);
app.get("/v1/goals", proxyAuthenticatedCore);
app.post("/v1/goals", proxyAuthenticatedCore);
app.get("/v1/goals/all", proxyAuthenticatedCore);
app.get("/v1/goals/canonical/list", proxyAuthenticatedCore);
app.post("/v1/goals/canonical", proxyAuthenticatedCore);
app.get("/v1/goals/:goalId/history", proxyAuthenticatedCore);
app.post("/v1/goals/:goalId/progress-events", proxyAuthenticatedCore);
app.get("/v1/goals/:goalId/progress-events", proxyAuthenticatedCore);
app.post("/v1/work-intents", proxyAuthenticatedCore);
app.get("/v1/workstreams/:workstreamId/events", proxyAuthenticatedCore);
app.post("/v1/workstreams/:workstreamId/events", proxyAuthenticatedCore);
app.get("/v1/workstreams/:workstreamId/artifacts", proxyAuthenticatedCore);
app.post("/v1/workstreams/:workstreamId/artifacts", proxyAuthenticatedCore);
app.get("/v1/workstreams/:workstreamId/checkpoints", proxyAuthenticatedCore);
app.put(
  "/v1/workstreams/:workstreamId/checkpoints/:runtimeId",
  proxyAuthenticatedCore,
);
app.patch(
  "/v1/workstreams/:workstreamId/artifacts/:artifactId/status",
  proxyAuthenticatedCore,
);
app.get("/v1/workstreams/:workstreamId", proxyAuthenticatedCore);
app.patch("/v1/workstreams/:workstreamId", proxyAuthenticatedCore);
app.post("/v1/goals/:goalId/focus", proxyAuthenticatedCore);
app.delete("/v1/goals/:goalId/focus", proxyAuthenticatedCore);
app.post("/v1/goals/:goalId/lifecycle", proxyAuthenticatedCore);
app.get("/v1/goals/:goalId/detail", proxyAuthenticatedCore);
app.get("/v1/goals/:goalId", proxyAuthenticatedCore);
app.patch("/v1/goals/:goalId", proxyAuthenticatedCore);
app.patch("/v1/goals/:goalId/progress", proxyAuthenticatedCore);
app.delete("/v1/goals/:goalId", proxyAuthenticatedCore);
app.get("/v1/folders", proxyAuthenticatedCore);
app.post("/v1/folders", proxyAuthenticatedCore);
app.get("/v1/folders/:folderId/conversations", proxyAuthenticatedCore);
app.post("/v1/folders/:folderId/conversations/bulk-move", proxyAuthenticatedCore);
app.get("/v1/folders/:folderId", proxyAuthenticatedCore);
app.patch("/v1/folders/:folderId", proxyAuthenticatedCore);
app.delete("/v1/folders/:folderId", proxyAuthenticatedCore);
app.post("/v1/folders/reorder", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/folder", proxyAuthenticatedCore);
app.all("/v1/users/developer/webhook/*", proxyAuthenticatedCore);
app.get("/v1/users/developer/webhooks/status", proxyAuthenticatedCore);
app.get("/v1/users/notification-settings", proxyAuthenticatedCore);
app.patch("/v1/users/notification-settings", proxyAuthenticatedCore);
app.get("/v1/users/daily-summary-settings", proxyAuthenticatedCore);
app.patch("/v1/users/daily-summary-settings", proxyAuthenticatedCore);
app.get("/v1/users/mentor-notification-settings", proxyAuthenticatedCore);
app.patch("/v1/users/mentor-notification-settings", proxyAuthenticatedCore);
app.get("/v1/users/assistant-settings", proxyAuthenticatedCore);
app.patch("/v1/users/assistant-settings", proxyAuthenticatedCore);
app.get("/v1/users/ai-profile", proxyAuthenticatedCore);
app.patch("/v1/users/ai-profile", proxyAuthenticatedCore);
app.get("/v1/users/profile", proxyAuthenticatedAuthProfile);
app.get("/v1/users/location-context-consent", proxyAuthenticatedCore);
app.put("/v1/users/location-context-consent", proxyAuthenticatedCore);
app.post("/v1/tts/synthesize", proxyAuthenticatedAI);
app.post("/v1/tts/synthesize-workers-ai", proxyAuthenticatedAI);
app.get("/v1/auto/model-pick", proxyAuthenticatedAI);
app.post("/v1/stt/transcribe-workers-ai", proxyAuthenticatedAI);
app.post("/v1/translate", proxyAuthenticatedAI);

app.all("/*", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  const headers = stripUntrustedHeaders(c.req.raw);
  if (auth)
    await attachAuthContext(headers, auth, c.env.INTERNAL_ASSERTION_SECRET);

  if (
    c.req.path.startsWith("/v1/ai/") ||
    c.req.path.startsWith("/v1/embeddings") ||
    c.req.path.startsWith("/v1/stt/")
  ) {
    const response = await c.env.API_AI.fetch(
      new Request(c.req.raw, { headers }),
    );
    return withRequestId(response, id);
  }
  if (envLegacy(c.env)) {
    const legacy = new URL(c.req.url);
    legacy.protocol = new URL(c.env.LEGACY_BACKEND_URL!).protocol;
    legacy.host = new URL(c.env.LEGACY_BACKEND_URL!).host;
    const response = await fetch(
      new Request(legacy, {
        method: c.req.method,
        headers,
        body: c.req.raw.body,
      }),
    );
    return withRequestId(response, id);
  }
  if (auth) return c.json({ error: "route not migrated" }, 404);
  return c.json({ error: "unauthorized" }, 401);
});

function envLegacy(
  env: EdgeEnv,
): env is EdgeEnv & { LEGACY_BACKEND_URL: string } {
  return (
    typeof env.LEGACY_BACKEND_URL === "string" &&
    env.LEGACY_BACKEND_URL.length > 0
  );
}

export default app;
