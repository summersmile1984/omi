import { Hono } from "hono";
import type { Context } from "hono";
import { cors } from "hono/cors";
import { requestId, withRequestId } from "../shared/request-id";
import {
  createRealtimeBootstrap,
  REALTIME_BOOTSTRAP_HEADER,
  REALTIME_BOOTSTRAP_SIGNATURE_HEADER,
} from "../shared/realtime-bootstrap";
import { createRealtimeTicket } from "../shared/realtime-ticket";
import { attachAuthContext, stripUntrustedHeaders, verifyBearer } from "./auth";
import {
  ACCOUNT_CUTOVER_CONTROL_PATH,
  cloudflareProductTrafficDenial,
} from "./cutover";
import type { EdgeEnv, EdgeVariables } from "./env";
import {
  edgeRateLimitPolicyForRequest,
  enforceEdgeRateLimit,
  STT_TRANSCRIBE_RATE_LIMIT,
} from "./rate-limit";

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
  c.json({ status: "ok", service: "edge", version: "cf-01" }),
);

app.get("/ready", async (c) => {
  const dependencies = [
    ["auth", c.env.AUTH, "/ready"],
    ["api-core", c.env.API_CORE, "/health"],
    ["api-ai", c.env.API_AI, "/health"],
    ["realtime", c.env.REALTIME, "/health"],
    ["jobs", c.env.JOBS, "/health"],
  ] as const;
  const statuses = Object.fromEntries(
    await Promise.all(
      dependencies.map(async ([name, service, path]) => {
        try {
          const response = await service.fetch(
            new Request(`https://${name}.internal${path}`),
          );
          await response.arrayBuffer();
          return [name, response.status] as const;
        } catch {
          return [name, 503] as const;
        }
      }),
    ),
  );
  try {
    const rateLimitId = c.env.RATE_LIMITS.idFromName("health");
    const response = await c.env.RATE_LIMITS.get(rateLimitId).fetch(
      new Request("https://rate-limit.internal/health"),
    );
    await response.arrayBuffer();
    statuses["rate-limit"] = response.status;
  } catch {
    statuses["rate-limit"] = 503;
  }
  const ready = Object.values(statuses).every((status) => status === 200);
  return c.json(
    {
      status: ready ? "ready" : "degraded",
      service: "edge",
      dependencies: statuses,
    },
    ready ? 200 : 503,
  );
});

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
app.get("/v1/app-categories", proxyPublicCore);
app.get("/v1/app/proactive-notification-scopes", proxyPublicCore);
app.get("/v1/app-capabilities", proxyPublicCore);
app.get("/v1/app/payment-plans", proxyPublicCore);
app.get("/v1/approved-apps", proxyPublicCore);
app.get("/v1/action-items/shared/:token", proxyPublicCore);
app.get("/v2/messages/shared/:token", proxyPublicCore);

app.all("/api/better-auth/*", async (c) => {
  const id = requestId(c.req.raw);
  const response = await c.env.AUTH.fetch(
    new Request(c.req.raw, {
      headers: stripUntrustedHeaders(c.req.raw, { preserveClientAuth: true }),
    }),
  );
  return withRequestId(response, id);
});

app.all("/v2/voice-message/transcribe-stream", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "realtime",
    c.req.raw,
  );
  const response = await c.env.REALTIME.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
});

app.all("/v4/listen", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "realtime",
    c.req.raw,
  );
  const response = await c.env.REALTIME.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
});

app.all("/v4/web/listen", async (c) => {
  const id = requestId(c.req.raw);
  if (c.req.header("upgrade")?.toLowerCase() !== "websocket") {
    return c.json({ error: "websocket upgrade required" }, 426);
  }
  const bootstrap = await createRealtimeBootstrap(
    id,
    c.env.INTERNAL_ASSERTION_SECRET,
  );
  if (!bootstrap) return c.json({ error: "realtime unavailable" }, 503);
  const headers = stripUntrustedHeaders(c.req.raw);
  headers.delete("authorization");
  headers.set(REALTIME_BOOTSTRAP_HEADER, bootstrap.encoded);
  headers.set(REALTIME_BOOTSTRAP_SIGNATURE_HEADER, bootstrap.signature);
  const response = await c.env.REALTIME.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
});

app.post("/v1/realtime/web-ticket", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const ticket = await createRealtimeTicket(
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
  );
  if (!ticket) return c.json({ error: "realtime unavailable" }, 503);
  c.header("cache-control", "no-store");
  return c.json({ ticket, expires_in: 30 });
});

app.all("/v1/omni/relay", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "realtime",
    c.req.raw,
  );
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
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "jobs",
    c.req.raw,
  );
  const response = await c.env.JOBS.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
};

const proxyAuthenticatedAsyncTranscription = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const rateLimitDenial = await enforceEdgeRateLimit(
    c.env,
    auth,
    STT_TRANSCRIBE_RATE_LIMIT,
    id,
  );
  if (rateLimitDenial) return withRequestId(rateLimitDenial, id);
  const headers = new Headers();
  for (const name of ["content-type", "content-length", "idempotency-key"]) {
    const value = c.req.header(name);
    if (value) headers.set(name, value);
  }
  const target = new URL("/v1/cf/transcription-jobs", c.req.url);
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "jobs",
    { method: "POST", url: target },
  );
  const declaredLength = Number(c.req.header("content-length"));
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_ASYNC_TRANSCRIPTION_AUDIO_BYTES
  ) {
    return c.json({ error: "audio body too large" }, 413);
  }
  const response = await c.env.JOBS.fetch(
    new Request(target, {
      method: "POST",
      headers,
      body: c.req.raw.body,
      duplex: "half",
    } as RequestInit & { duplex: "half" }),
  );
  return withRequestId(response, id);
};

const proxyAuthenticatedAsyncTranscriptionStatus = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const headers = new Headers();
  const jobId = encodeURIComponent(c.req.param("jobId") || "");
  const target = new URL(`/v1/cf/transcription-jobs/${jobId}`, c.req.url);
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "jobs",
    { method: "GET", url: target },
  );
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
  if (c.req.path !== ACCOUNT_CUTOVER_CONTROL_PATH) {
    const denial = await cloudflareProductTrafficDenial(
      c.req.raw,
      c.env,
      auth,
      id,
    );
    if (denial) return withRequestId(denial, id);
  }
  const policy = edgeRateLimitPolicyForRequest(c.req.method, c.req.path);
  if (policy) {
    const rateLimitDenial = await enforceEdgeRateLimit(c.env, auth, policy, id);
    if (rateLimitDenial) return withRequestId(rateLimitDenial, id);
  }
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "api-core",
    c.req.raw,
  );
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
  const target = new URL(c.req.url);
  target.protocol = "https:";
  target.host = "auth.internal";
  target.pathname = "/internal/profile";
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "auth",
    { method: "GET", url: target },
  );
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
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const policy = edgeRateLimitPolicyForRequest(c.req.method, c.req.path);
  if (policy) {
    const rateLimitDenial = await enforceEdgeRateLimit(c.env, auth, policy, id);
    if (rateLimitDenial) return withRequestId(rateLimitDenial, id);
  }
  const headers = stripUntrustedHeaders(c.req.raw);
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "api-ai",
    c.req.raw,
  );
  const response = await c.env.API_AI.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
};

app.get("/v1/config/api-keys", proxyAuthenticatedCore);
app.get("/v2/messages", proxyAuthenticatedCore);
app.delete("/v2/messages", proxyAuthenticatedCore);
app.post("/v2/messages/share", proxyAuthenticatedCore);
app.post("/v2/messages", proxyAuthenticatedAI);
app.get("/v1/apps/popular", proxyAuthenticatedCore);
app.get("/v1/apps/:appId", proxyAuthenticatedCore);
app.get("/v2/apps", proxyPublicCore);
app.get("/v2/apps/capability/:capability_id/grouped", proxyPublicCore);
app.get("/v2/apps/search", proxyAuthenticatedCore);
app.get("/v1/apps/enabled", proxyAuthenticatedCore);
app.post("/v1/apps/enable", proxyAuthenticatedCore);
app.post("/v1/apps/disable", proxyAuthenticatedCore);
app.post("/v2/realtime/session", proxyAuthenticatedAI);
app.post("/v2/realtime/usage", proxyAuthenticatedAI);
app.post("/v1/stt/transcribe-async", proxyAuthenticatedAsyncTranscription);
app.get(
  "/v1/stt/transcribe-async/:jobId",
  proxyAuthenticatedAsyncTranscriptionStatus,
);
app.post("/v1/embeddings-workers-ai", proxyAuthenticatedAI);
app.post("/v1/stt/transcribe", proxyAuthenticatedAI);
app.get("/v1/account/cutover/control", proxyAuthenticatedCore);
app.all("/v1/cf/probe", proxyAuthenticatedCore);
app.all("/v1/cf/assets/*", proxyAuthenticatedCore);
app.get("/v3/memories", proxyAuthenticatedCore);
app.post("/v3/memories", proxyAuthenticatedCore);
app.delete("/v3/memories", proxyAuthenticatedCore);
app.delete("/v3/memories/batch", proxyAuthenticatedCore);
app.delete("/v3/memories/:memoryId", proxyAuthenticatedCore);
app.patch("/v3/memories/:memoryId", proxyAuthenticatedCore);
app.patch("/v3/memories/:memoryId/visibility", proxyAuthenticatedCore);
app.post("/v3/memories/:memoryId/review", proxyAuthenticatedCore);
app.get("/v1/conversations", proxyAuthenticatedCore);
app.post("/v1/conversations/search", proxyAuthenticatedCore);
app.get("/v1/conversations/count", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId", proxyAuthenticatedCore);
app.delete("/v1/conversations/:conversationId", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/photos", proxyAuthenticatedCore);
app.get(
  "/v1/conversations/:conversationId/transcripts",
  proxyAuthenticatedCore,
);
app.get("/v1/conversations/:conversationId/analytics", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/recording", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/events", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/summary", proxyAuthenticatedCore);
app.delete(
  "/v1/conversations/:conversationId/calendar-event",
  proxyAuthenticatedCore,
);
app.patch(
  "/v1/conversations/:conversationId/segments/text",
  proxyAuthenticatedCore,
);
app.patch("/v1/conversations/:conversationId/title", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/starred", proxyAuthenticatedCore);
app.get(
  "/v1/conversations/:conversationId/action-items",
  proxyAuthenticatedCore,
);
app.get(
  "/v1/conversations/:conversationId/action-items/count",
  proxyAuthenticatedCore,
);
app.patch(
  "/v1/conversations/:conversationId/action-items",
  proxyAuthenticatedCore,
);
app.delete(
  "/v1/conversations/:conversationId/action-items",
  proxyAuthenticatedCore,
);
app.patch(
  "/v1/conversations/:conversationId/action-items/:actionItemIdx",
  proxyAuthenticatedCore,
);
app.post("/v1/cf/conversations", proxyAuthenticatedCore);
app.get("/v1/cf/conversations", proxyAuthenticatedCore);
app.get("/v1/cf/conversations/count", proxyAuthenticatedCore);
app.get("/v1/cf/conversations/:conversationId", proxyAuthenticatedCore);
app.patch("/v1/cf/conversations/:conversationId/title", proxyAuthenticatedCore);
app.patch(
  "/v1/cf/conversations/:conversationId/starred",
  proxyAuthenticatedCore,
);
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
app.post("/v1/action-items/share", proxyAuthenticatedCore);
app.post("/v1/action-items/accept", proxyAuthenticatedCore);
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
app.post(
  "/v1/folders/:folderId/conversations/bulk-move",
  proxyAuthenticatedCore,
);
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
app.get("/v1/users/daily-summaries", proxyAuthenticatedCore);
app.get("/v1/users/daily-summaries/:summaryId", proxyAuthenticatedCore);
app.patch(
  "/v1/users/daily-summaries/:summaryId/visibility",
  proxyAuthenticatedCore,
);
app.delete("/v1/users/daily-summaries/:summaryId", proxyAuthenticatedCore);
app.post("/v1/users/daily-summary-settings/test", proxyAuthenticatedCore);
app.post(
  "/v1/users/daily-summaries/:summaryId/regenerate",
  proxyAuthenticatedCore,
);
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

  if (
    c.req.path.startsWith("/v1/ai/") ||
    c.req.path.startsWith("/v1/embeddings") ||
    c.req.path.startsWith("/v1/stt/")
  ) {
    const headers = stripUntrustedHeaders(c.req.raw);
    if (auth) {
      const denial = await cloudflareProductTrafficDenial(
        c.req.raw,
        c.env,
        auth,
        id,
      );
      if (denial) return withRequestId(denial, id);
      await attachAuthContext(
        headers,
        auth,
        c.env.INTERNAL_ASSERTION_SECRET,
        "api-ai",
        c.req.raw,
      );
    }
    const response = await c.env.API_AI.fetch(
      new Request(c.req.raw, { headers }),
    );
    return withRequestId(response, id);
  }
  if (envLegacy(c.env)) {
    const headers = stripUntrustedHeaders(c.req.raw, {
      preserveClientAuth: true,
    });
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
