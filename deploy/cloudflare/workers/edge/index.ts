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
  activateByok,
  deactivateByok,
  parseByokActivationPayload,
  validateByokHeaders,
} from "./byok";
import {
  ACCOUNT_CUTOVER_CONTROL_PATH,
  cloudflareProductTrafficDenial,
} from "./cutover";
import type { EdgeEnv, EdgeVariables } from "./env";
import type { AuthAudience, AuthContext } from "../shared/auth-context";
import {
  handleMcpTransport,
  mcpProtectedResourceMetadata,
} from "./mcp-transport";
import {
  edgeRateLimitPolicyForRequest,
  enforceEdgeRateLimit,
  STT_TRANSCRIBE_RATE_LIMIT,
} from "./rate-limit";

const app = new Hono<{ Bindings: EdgeEnv; Variables: EdgeVariables }>();
const MAX_ASYNC_TRANSCRIPTION_AUDIO_BYTES = 5_000_000;
const MAX_BYOK_ACTIVATION_BODY_BYTES = 8_192;
const OPENAI_APPS_CHALLENGE_TOKEN =
  "ZsVB_wpc4R35_tHloCZCokY6H2fBkKyBJrz-4MtXjYE";

async function authenticatedHeaders(
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
  identity: AuthContext,
  audience: AuthAudience,
  target: { method: string; url: string | URL } = c.req.raw,
  options: { recoverInvalidByok?: boolean } = {},
): Promise<Headers | Response> {
  const headers = stripUntrustedHeaders(c.req.raw);
  const validation = await validateByokHeaders(c.env, identity, headers, {
    recoverInvalid: options.recoverInvalidByok,
  });
  if (validation.response) return validation.response;
  await attachAuthContext(
    headers,
    validation.context,
    c.env.INTERNAL_ASSERTION_SECRET,
    audience,
    target,
  );
  return headers;
}

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

app.on(["GET", "HEAD"], "/v1/health", (c) => {
  const headers = { "content-type": "application/json; charset=UTF-8" };
  if (c.req.method === "HEAD") return new Response(null, { headers });
  return new Response(JSON.stringify({ status: "ok" }), { headers });
});

app.get(
  "/.well-known/apple-developer-domain-association.txt",
  () =>
    new Response("", {
      headers: { "content-type": "text/plain; charset=UTF-8" },
    }),
);

app.get(
  "/.well-known/openai-apps-challenge",
  () =>
    new Response(OPENAI_APPS_CHALLENGE_TOKEN, {
      headers: { "content-type": "text/plain; charset=UTF-8" },
    }),
);

app.get("/ready", async (c) => {
  const dependencies = [
    ["auth", c.env.AUTH, "/ready"],
    ["api-core", c.env.API_CORE, "/health"],
    ["api-ai", c.env.API_AI, "/health"],
    ["realtime", c.env.REALTIME, "/health"],
    ["jobs", c.env.JOBS, "/ready"],
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

app.on(["GET", "HEAD"], "/.well-known/oauth-protected-resource", (c) => {
  const metadata = mcpProtectedResourceMetadata(c.env);
  if (!metadata) return c.json({ error: "mcp unavailable" }, 503);
  if (c.req.method === "HEAD") {
    return new Response(null, {
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
      },
    });
  }
  c.header("cache-control", "no-store");
  return c.json(metadata);
});
app.on(
  ["GET", "HEAD"],
  "/.well-known/oauth-protected-resource/v1/mcp/sse",
  (c) => {
    const metadata = mcpProtectedResourceMetadata(c.env);
    if (!metadata) return c.json({ error: "mcp unavailable" }, 503);
    if (c.req.method === "HEAD") {
      return new Response(null, {
        headers: {
          "content-type": "application/json",
          "cache-control": "no-store",
        },
      });
    }
    c.header("cache-control", "no-store");
    return c.json(metadata);
  },
);

app.on(
  ["GET", "HEAD"],
  "/.well-known/oauth-authorization-server",
  async (c) => {
    const id = requestId(c.req.raw);
    const target = new URL(
      "/api/better-auth/.well-known/oauth-authorization-server",
      "https://auth.internal",
    );
    try {
      const response = await c.env.AUTH.fetch(new Request(target));
      if (c.req.method === "HEAD") {
        await response.arrayBuffer();
        return withRequestId(
          new Response(null, {
            status: response.status,
            headers: response.headers,
          }),
          id,
        );
      }
      return withRequestId(response, id);
    } catch {
      return withRequestId(
        Response.json(
          { error: "authorization_server_unavailable" },
          { status: 503 },
        ),
        id,
      );
    }
  },
);

app.get("/v1/mcp/sse/info", (c) =>
  c.json({
    transport: "streamable-http",
    endpoint: c.env.MCP_RESOURCE_URL || null,
    protocol_versions: ["2026-07-28", "2025-03-26"],
    oauth: true,
    api_key_compatibility: true,
    migrated_tools: 22,
    pending_tools: [],
  }),
);
app.on(["GET", "POST", "DELETE"], "/v1/mcp/sse", (c) =>
  handleMcpTransport(c.req.raw, c.env),
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

const proxyPublicJobs = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const response = await c.env.JOBS.fetch(
    new Request(c.req.raw, { headers: stripUntrustedHeaders(c.req.raw) }),
  );
  return withRequestId(response, id);
};

// App integrations authenticate with an app-scoped API key, not a Better Auth
// session. Preserve only that Authorization header while still stripping all
// caller-controlled internal identity headers and cookies.
const proxyIntegrationCore = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const headers = stripUntrustedHeaders(c.req.raw, {
    preserveClientAuth: true,
  });
  headers.delete("cookie");
  const response = await c.env.API_CORE.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
};

// Developer API routes use the dedicated omi_dev_ credential family. Keep the
// raw Authorization header for API Core to verify against the D1 digest while
// removing cookies and every caller-controlled internal identity assertion.
// Valid key-shaped writes are rate-limited by an irreversible digest, never by
// the raw credential.
const proxyDeveloperCore = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const policy = edgeRateLimitPolicyForRequest(c.req.method, c.req.path);
  if (policy) {
    const subject = await externalApiKeyRateLimitSubject(
      c.req.raw.headers.get("authorization"),
      "dev",
    );
    if (subject) {
      const rateLimitDenial = await enforceEdgeRateLimit(
        c.env,
        { uid: `developer:${subject}`, authority: "internal", requestId: id },
        policy,
        id,
      );
      if (rateLimitDenial) return withRequestId(rateLimitDenial, id);
    }
  }
  const headers = stripUntrustedHeaders(c.req.raw, {
    preserveClientAuth: true,
  });
  headers.delete("cookie");
  const response = await c.env.API_CORE.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
};

async function externalApiKeyRateLimitSubject(
  authorization: string | null,
  family: "mcp" | "dev",
): Promise<string | null> {
  const pattern =
    family === "mcp"
      ? /^Bearer omi_mcp_([0-9a-f]{32})$/
      : /^Bearer omi_dev_([0-9a-f]{32})$/;
  const match = pattern.exec(authorization || "");
  if (!match) return null;
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(match[1]),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

// MCP REST tools authenticate with their own D1-backed key family. The Edge
// strips cookies and caller identity assertions, keeps only Authorization, and
// rate-limits valid key-shaped write traffic by an irreversible key digest.
const proxyMcpCore = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const policy = edgeRateLimitPolicyForRequest(c.req.method, c.req.path);
  if (policy) {
    const subject = await externalApiKeyRateLimitSubject(
      c.req.raw.headers.get("authorization"),
      "mcp",
    );
    if (subject) {
      const rateLimitDenial = await enforceEdgeRateLimit(
        c.env,
        { uid: `mcp:${subject}`, authority: "internal", requestId: id },
        policy,
        id,
      );
      if (rateLimitDenial) return withRequestId(rateLimitDenial, id);
    }
  }
  const headers = stripUntrustedHeaders(c.req.raw, {
    preserveClientAuth: true,
  });
  headers.delete("cookie");
  const response = await c.env.API_CORE.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
};

const proxyLegacyBackend = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  if (!envLegacy(c.env)) {
    return withRequestId(
      Response.json({ error: "route not migrated" }, { status: 404 }),
      id,
    );
  }
  const headers = stripUntrustedHeaders(c.req.raw, {
    preserveClientAuth: true,
  });
  const legacy = new URL(c.req.url);
  legacy.protocol = new URL(c.env.LEGACY_BACKEND_URL).protocol;
  legacy.host = new URL(c.env.LEGACY_BACKEND_URL).host;
  const response = await fetch(
    new Request(legacy, {
      method: c.req.method,
      headers,
      body: c.req.raw.body,
    }),
  );
  return withRequestId(response, id);
};

const proxyPublicFirmware = proxyPublicCore;

// The cloud Agent VM was retired, but released desktop clients still call
// these endpoints during startup and shutdown. Keep the unauthenticated
// tombstone contract at the edge so they never fall through to legacy (or
// trigger Better Auth's 401 sign-out behavior).
const AGENT_VM_RETIRED =
  "The cloud Agent VM has been retired and can no longer be provisioned.";

app.post("/v2/agent/provision", () =>
  Response.json({ detail: AGENT_VM_RETIRED }, { status: 410 }),
);
app.post("/v2/agent/vm/stop-self", () =>
  Response.json({ detail: AGENT_VM_RETIRED }, { status: 410 }),
);
app.get(
  "/v2/agent/status",
  () =>
    new Response("null", {
      status: 200,
      headers: { "content-type": "application/json; charset=UTF-8" },
    }),
);

app.get("/v2/firmware/stable", proxyPublicFirmware);
app.get("/v2/firmware/latest", proxyPublicFirmware);
app.get("/v2/firmware/version", proxyPublicFirmware);
app.get("/v1/announcements/changelogs", proxyPublicCore);
app.get("/v1/announcements/features", proxyPublicCore);
app.get("/v1/announcements/general", proxyPublicCore);
app.get("/v1/announcements/all", proxyPublicCore);
app.get("/v1/announcements/:announcementId", proxyPublicCore);
app.get("/v1/trends", proxyPublicCore);
app.post("/v1/announcements", proxyPublicCore);
app.put("/v1/announcements/:announcementId", proxyPublicCore);
app.delete("/v1/announcements/:announcementId", proxyPublicCore);
app.get("/v1/app-categories", proxyPublicCore);
app.get("/v1/app/proactive-notification-scopes", proxyPublicCore);
app.get("/v1/app-capabilities", proxyPublicCore);
app.get("/v1/app/payment-plans", proxyPublicCore);
app.get("/v1/approved-apps", proxyPublicCore);
app.get("/v1/apps/:appId/logo/:version", proxyPublicJobs);
app.get("/v1/x/oauth/callback", proxyPublicJobs);
app.get("/v2/integrations/todoist/callback", proxyPublicJobs);
app.get("/v2/integrations/asana/callback", proxyPublicJobs);
app.get("/v2/integrations/google-tasks/callback", proxyPublicJobs);
app.get("/v2/integrations/clickup/callback", proxyPublicJobs);
app.get("/v2/integrations/google-calendar/callback", proxyPublicJobs);
app.get("/v1/apps/:appId/reviews", proxyPublicCore);
app.post("/v1/apps/tester", proxyPublicJobs);
app.post("/v1/apps/tester/access", proxyPublicJobs);
app.delete("/v1/apps/tester/access", proxyPublicJobs);
app.get("/v1/apps/public/unapproved", proxyPublicJobs);
app.patch("/v1/apps/:appId/popular", proxyPublicJobs);
app.post("/v1/apps/:appId/approve", proxyPublicJobs);
app.post("/v1/apps/:appId/reject", proxyPublicJobs);
app.get("/v1/summary-app-ids", proxyPublicJobs);
app.post("/v1/summary-app-ids/:appId", proxyPublicJobs);
app.delete("/v1/summary-app-ids/:appId", proxyPublicJobs);
app.post("/v1/integrations/notification", proxyIntegrationCore);
app.post("/v1/notification", proxyPublicJobs);
app.post("/v2/integrations/:app_id/user/conversations", proxyIntegrationCore);
app.post("/v2/integrations/:app_id/user/memories", proxyIntegrationCore);
app.get("/v2/integrations/:app_id/memories", proxyIntegrationCore);
app.get("/v2/integrations/:app_id/conversations", proxyIntegrationCore);
app.post("/v2/integrations/:app_id/search/conversations", proxyIntegrationCore);
app.post("/v2/integrations/:app_id/notification", proxyIntegrationCore);
app.get("/v2/integrations/:app_id/tasks", proxyIntegrationCore);
app.get("/v1/dev/user/memories/vector/search", proxyDeveloperCore);
app.get("/v1/dev/user/memories", proxyDeveloperCore);
app.post("/v1/dev/user/memories/batch", proxyDeveloperCore);
app.post("/v1/dev/user/memories", proxyDeveloperCore);
app.patch("/v1/dev/user/memories/:memory_id", proxyDeveloperCore);
app.delete("/v1/dev/user/memories/:memory_id", proxyDeveloperCore);
app.get("/v1/dev/user/action-items", proxyDeveloperCore);
app.post("/v1/dev/user/action-items/batch", proxyDeveloperCore);
app.post("/v1/dev/user/action-items", proxyDeveloperCore);
app.patch("/v1/dev/user/action-items/:action_item_id", proxyDeveloperCore);
app.delete("/v1/dev/user/action-items/:action_item_id", proxyDeveloperCore);
app.get("/v1/dev/user/folders", proxyDeveloperCore);
app.get("/v1/dev/user/conversations", proxyDeveloperCore);
app.post("/v1/dev/user/conversations/from-segments", proxyDeveloperCore);
app.post("/v1/dev/user/conversations", proxyDeveloperCore);
app.get("/v1/dev/user/conversations/:conversationId", proxyDeveloperCore);
app.patch("/v1/dev/user/conversations/:conversation_id", proxyDeveloperCore);
app.delete("/v1/dev/user/conversations/:conversation_id", proxyDeveloperCore);
app.get("/v1/dev/user/goals", proxyDeveloperCore);
app.post("/v1/dev/user/goals", proxyDeveloperCore);
app.get("/v1/dev/user/goals/:goal_id/history", proxyDeveloperCore);
app.patch("/v1/dev/user/goals/:goal_id/progress", proxyDeveloperCore);
app.get("/v1/dev/user/goals/:goalId", proxyDeveloperCore);
app.patch("/v1/dev/user/goals/:goal_id", proxyDeveloperCore);
app.delete("/v1/dev/user/goals/:goal_id", proxyDeveloperCore);
app.post("/v1/mcp/memories", proxyMcpCore);
app.delete("/v1/mcp/memories/:memory_id", proxyMcpCore);
app.patch("/v1/mcp/memories/:memory_id", proxyMcpCore);
app.get("/v1/mcp/profile", proxyMcpCore);
app.get("/v1/mcp/memories/search", proxyMcpCore);
app.get("/v1/mcp/memories", proxyMcpCore);
app.get("/v1/mcp/x-posts/search", proxyMcpCore);
app.get("/v1/mcp/x-posts", proxyMcpCore);
app.get("/v1/mcp/conversations", proxyMcpCore);
app.get("/v1/mcp/conversations/search", proxyMcpCore);
app.get("/v1/mcp/conversations/:conversation_id", proxyMcpCore);
app.get("/v1/mcp/action-items/search", proxyMcpCore);
app.get("/v1/mcp/action-items", proxyMcpCore);
app.post("/v1/mcp/action-items", proxyMcpCore);
app.post("/v1/mcp/action-items/:action_item_id/complete", proxyMcpCore);
app.patch("/v1/mcp/action-items/:action_item_id", proxyMcpCore);
app.delete("/v1/mcp/action-items/:action_item_id", proxyMcpCore);
app.get("/v1/mcp/goals", proxyMcpCore);
app.get("/v1/mcp/chat", proxyMcpCore);
app.get("/v1/mcp/people", proxyMcpCore);
app.get("/v1/mcp/screen-activity", proxyMcpCore);
app.get("/v1/mcp/daily-summaries", proxyMcpCore);
app.get("/v1/payments/success", proxyPublicCore);
app.get("/v1/payments/cancel", proxyPublicCore);
app.get("/v1/payments/portal-return", proxyPublicCore);
app.post("/v1/stripe/webhook", proxyPublicJobs);
app.post("/v1/stripe/connect/webhook", proxyPublicJobs);
app.get("/v1/stripe/supported-countries", proxyPublicJobs);
app.get("/v1/stripe/refresh/:accountId", proxyPublicJobs);
app.get("/v1/stripe/return/:accountId", proxyPublicJobs);
app.get("/v1/action-items/shared/:token", proxyPublicCore);
app.get("/v2/messages/shared/:token", proxyPublicCore);
app.get("/v1/daily-summaries/:summaryId/shared", proxyPublicCore);
app.get("/v3/speech-profile/audio", proxyPublicCore);
app.get("/v1/fair-use/case/:case_ref/status", proxyPublicCore);
app.get("/v1/admin/fair-use/flagged", proxyPublicCore);
app.get("/v1/admin/fair-use/user/:uid", proxyPublicCore);
app.post(
  "/v1/admin/fair-use/user/:uid/resolve-event/:event_id",
  proxyPublicCore,
);
app.post("/v1/admin/fair-use/user/:uid/reset", proxyPublicCore);
app.post("/v1/admin/fair-use/user/:uid/set-stage", proxyPublicCore);
app.get("/v1/admin/fair-use/case/:case_ref", proxyPublicCore);

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
  const headers = await authenticatedHeaders(c, auth, "jobs");
  if (headers instanceof Response) return withRequestId(headers, id);
  const response = await c.env.JOBS.fetch(new Request(c.req.raw, { headers }));
  return withRequestId(response, id);
};

// Privacy deletion must remain reachable while ordinary product traffic is
// fenced. Jobs validates that the account is fully Cloudflare-owned before it
// persists the deletion intent, so this route intentionally skips the normal
// account-cutover denial after authenticating the caller.
const proxyAuthenticatedAccountDeletion = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return c.json({ error: "unauthorized" }, 401);
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
app.post("/v1/payments/checkout-session", proxyAuthenticatedJobs);
app.post("/v1/payments/customer-portal", proxyAuthenticatedJobs);
app.post("/v1/payments/upgrade-subscription", proxyAuthenticatedJobs);
app.delete("/v1/payments/subscription", proxyAuthenticatedJobs);
app.post("/v1/stripe/connect-accounts", proxyAuthenticatedJobs);
app.get("/v1/stripe/onboarded", proxyAuthenticatedJobs);
app.post("/v1/stripe/refresh/:accountId", proxyAuthenticatedJobs);
app.post("/v1/paypal/payment-details", proxyAuthenticatedJobs);
app.get("/v1/paypal/payment-details", proxyAuthenticatedJobs);
app.get("/v1/payment-methods/status", proxyAuthenticatedJobs);
app.post("/v1/payment-methods/default", proxyAuthenticatedJobs);
app.get("/v1/x/oauth-url", proxyAuthenticatedJobs);
app.get("/v1/x/connection-status", proxyAuthenticatedJobs);
app.get("/v1/x/posts", proxyAuthenticatedJobs);
app.post("/v1/x/sync", proxyAuthenticatedJobs);
app.post("/v1/x/disconnect", proxyAuthenticatedJobs);
app.get("/v1/task-integrations", proxyAuthenticatedJobs);
app.get("/v1/task-integrations/default", proxyAuthenticatedJobs);
app.put("/v1/task-integrations/default", proxyAuthenticatedJobs);
app.get("/v1/task-integrations/asana/workspaces", proxyAuthenticatedJobs);
app.get(
  "/v1/task-integrations/asana/projects/:workspace_gid",
  proxyAuthenticatedJobs,
);
app.get("/v1/task-integrations/clickup/teams", proxyAuthenticatedJobs);
app.get(
  "/v1/task-integrations/clickup/spaces/:team_id",
  proxyAuthenticatedJobs,
);
app.get(
  "/v1/task-integrations/clickup/lists/:space_id",
  proxyAuthenticatedJobs,
);
app.get("/v1/task-integrations/:app_key/oauth-url", proxyAuthenticatedJobs);
app.post("/v1/task-integrations/:app_key/tasks", proxyAuthenticatedJobs);
app.put("/v1/task-integrations/:app_key", proxyAuthenticatedJobs);
app.delete("/v1/task-integrations/:app_key", proxyAuthenticatedJobs);
app.get("/v1/integrations/google_calendar", proxyAuthenticatedJobs);
app.put("/v1/integrations/google_calendar", proxyAuthenticatedJobs);
app.delete("/v1/integrations/google_calendar", proxyAuthenticatedJobs);
app.get("/v1/integrations/google_calendar/oauth-url", proxyAuthenticatedJobs);
app.get("/v1/calendar/google/events", proxyAuthenticatedJobs);
app.delete("/v1/users/delete-account", proxyAuthenticatedAccountDeletion);

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
  const headers = await authenticatedHeaders(c, auth, "api-core", c.req.raw, {
    recoverInvalidByok: c.req.path === "/v1/users/me/subscription",
  });
  if (headers instanceof Response) return withRequestId(headers, id);
  const response = await c.env.API_CORE.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
};

const proxyConversationAudioDownload = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  // The core Worker verifies the HMAC token and its uid/conversation/audio
  // binding. Tokenless fallback downloads retain the normal Better Auth path.
  if (c.req.query("token")) return proxyPublicCore(c);
  return proxyAuthenticatedCore(c);
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

const proxyAuthenticatedMcpGrants = async (
  c: Context<{ Bindings: EdgeEnv; Variables: EdgeVariables }>,
) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return withRequestId(c.json({ error: "unauthorized" }, 401), id);
  const headers = stripUntrustedHeaders(c.req.raw);
  const target = new URL("/internal/mcp/grants", "https://auth.internal");
  const grantId = c.req.param("grantId");
  if (grantId) target.pathname += `/${encodeURIComponent(grantId)}`;
  await attachAuthContext(
    headers,
    auth,
    c.env.INTERNAL_ASSERTION_SECRET,
    "auth",
    { method: c.req.method, url: target },
  );
  const response = await c.env.AUTH.fetch(
    new Request(target, { method: c.req.method, headers }),
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
  const headers = await authenticatedHeaders(c, auth, "api-ai");
  if (headers instanceof Response) return withRequestId(headers, id);
  const response = await c.env.API_AI.fetch(
    new Request(c.req.raw, { headers }),
  );
  return withRequestId(response, id);
};

app.get("/v1/config/api-keys", proxyAuthenticatedCore);
app.post("/v1/users/me/byok-active", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return withRequestId(c.json({ error: "unauthorized" }, 401), id);
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const contentLength = Number(c.req.header("content-length"));
  if (
    Number.isFinite(contentLength) &&
    contentLength > MAX_BYOK_ACTIVATION_BODY_BYTES
  ) {
    return withRequestId(
      c.json({ detail: "Invalid BYOK activation payload" }, 400),
      id,
    );
  }
  let payload: unknown;
  try {
    const body = await c.req.arrayBuffer();
    if (body.byteLength > MAX_BYOK_ACTIVATION_BODY_BYTES) throw new Error();
    payload = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return withRequestId(
      c.json({ detail: "Invalid BYOK activation payload" }, 400),
      id,
    );
  }
  const parsed = parseByokActivationPayload(payload);
  if (parsed instanceof Response) return withRequestId(parsed, id);
  const response = await activateByok(c.env, auth.uid, parsed.fingerprints);
  response.headers.set("cache-control", "no-store");
  return withRequestId(response, id);
});
app.delete("/v1/users/me/byok-active", async (c) => {
  const id = requestId(c.req.raw);
  const auth = await verifyBearer(c.req.raw, c.env, id);
  if (!auth) return withRequestId(c.json({ error: "unauthorized" }, 401), id);
  const denial = await cloudflareProductTrafficDenial(
    c.req.raw,
    c.env,
    auth,
    id,
  );
  if (denial) return withRequestId(denial, id);
  const response = await deactivateByok(c.env, auth.uid);
  response.headers.set("cache-control", "no-store");
  return withRequestId(response, id);
});
app.get("/v1/users/me/usage", proxyAuthenticatedCore);
app.get("/v1/users/me/subscription", proxyAuthenticatedCore);
app.get("/v1/users/me/usage-quota", proxyAuthenticatedCore);
app.get("/v1/users/me/paywall", proxyAuthenticatedCore);
app.get("/v1/users/me/trial", proxyAuthenticatedCore);
app.get("/v1/users/me/llm-usage", proxyAuthenticatedCore);
app.post("/v1/users/me/llm-usage", proxyAuthenticatedCore);
app.get("/v1/users/me/llm-usage/top-features", proxyAuthenticatedCore);
app.get("/v1/users/me/llm-usage/total", proxyAuthenticatedCore);
app.get("/v1/users/export", proxyAuthenticatedCore);
app.get("/v1/payments/available-plans", proxyAuthenticatedCore);
app.get("/v1/payments/overage-info", proxyAuthenticatedCore);
app.get("/v1/fair-use/status", proxyAuthenticatedCore);
app.get("/v2/messages", proxyAuthenticatedCore);
app.delete("/v2/messages", proxyAuthenticatedCore);
app.delete("/v1/messages", proxyAuthenticatedCore);
app.post("/v1/messages/:messageId/report", proxyAuthenticatedCore);
app.post("/v2/messages/:messageId/report", proxyAuthenticatedCore);
app.patch("/v2/messages/:messageId/rating", proxyAuthenticatedCore);
app.post("/v2/messages/share", proxyAuthenticatedCore);
app.post("/v2/messages", proxyAuthenticatedAI);
app.post("/v1/initial-message", proxyAuthenticatedAI);
app.post("/v2/initial-message", proxyAuthenticatedAI);
app.post("/v2/chat/initial-message", proxyAuthenticatedAI);
app.post("/v2/chat/generate-title", proxyAuthenticatedAI);
app.get("/v1/users/stats/chat-messages", proxyAuthenticatedCore);
app.post("/v2/chat-sessions", proxyAuthenticatedCore);
app.get("/v2/chat-sessions", proxyAuthenticatedCore);
app.get("/v2/chat-sessions/:sessionId", proxyAuthenticatedCore);
app.patch("/v2/chat-sessions/:sessionId", proxyAuthenticatedCore);
app.delete("/v2/chat-sessions/:sessionId", proxyAuthenticatedCore);
app.post("/v2/desktop/messages", proxyAuthenticatedCore);
app.get("/v2/desktop/messages", proxyAuthenticatedCore);
app.get("/v2/desktop/messages/reconcile", proxyAuthenticatedCore);
app.delete("/v2/desktop/messages", proxyAuthenticatedCore);
app.patch("/v2/desktop/messages/:messageId/rating", proxyAuthenticatedCore);
app.get("/v1/apps/popular", proxyAuthenticatedCore);
app.get("/v1/apps", proxyAuthenticatedCore);
app.get("/v1/app/plans", proxyAuthenticatedCore);
app.get("/v1/app/generate-prompts", proxyAuthenticatedAI);
app.post("/v1/app/generate-description", proxyAuthenticatedAI);
app.post("/v1/app/generate-description-emoji", proxyAuthenticatedAI);
app.post("/v1/app/generate", proxyAuthenticatedAI);
app.get("/v1/apps/tester/check", proxyAuthenticatedCore);
app.get("/v1/apps/:appId", proxyAuthenticatedCore);
app.post("/v1/apps", proxyAuthenticatedJobs);
app.patch("/v1/apps/:appId", proxyAuthenticatedJobs);
app.patch("/v1/apps/:app_id/change-visibility", proxyAuthenticatedJobs);
app.post("/v1/apps/:app_id/refresh-manifest", proxyAuthenticatedJobs);
app.delete("/v1/apps/:appId", proxyAuthenticatedJobs);
app.post("/v1/apps/:app_id/keys", proxyAuthenticatedJobs);
app.get("/v1/apps/:app_id/keys", proxyAuthenticatedJobs);
app.delete("/v1/apps/:app_id/keys/:key_id", proxyAuthenticatedJobs);
app.post("/v1/mcp/keys", proxyAuthenticatedJobs);
app.get("/v1/mcp/keys", proxyAuthenticatedJobs);
app.delete("/v1/mcp/keys/:keyId", proxyAuthenticatedJobs);
app.post("/v1/dev/keys", proxyAuthenticatedJobs);
app.get("/v1/dev/keys", proxyAuthenticatedJobs);
app.delete("/v1/dev/keys/:keyId", proxyAuthenticatedJobs);
app.get("/v1/apps/:app_id/subscription", proxyAuthenticatedJobs);
app.delete("/v1/apps/:app_id/subscription", proxyAuthenticatedJobs);
app.post("/v1/apps/review", proxyAuthenticatedCore);
app.patch("/v1/apps/:appId/review", proxyAuthenticatedCore);
app.patch("/v1/apps/:appId/review/reply", proxyAuthenticatedCore);
app.get("/v2/apps", proxyPublicCore);
app.get("/v2/apps/capability/:capability_id/grouped", proxyPublicCore);
app.get("/v2/apps/search", proxyAuthenticatedCore);
app.get("/v1/apps/enabled", proxyAuthenticatedCore);
app.post("/v1/apps/enable", proxyAuthenticatedCore);
app.post("/v1/apps/disable", proxyAuthenticatedCore);
app.put("/v1/users/preferences/app", proxyAuthenticatedCore);
app.post("/v2/realtime/session", proxyAuthenticatedAI);
app.post("/v2/realtime/usage", proxyAuthenticatedAI);
app.post("/v2/voice-message/transcribe", proxyAuthenticatedAI);
app.post("/v1/stt/transcribe-async", proxyAuthenticatedAsyncTranscription);
app.get(
  "/v1/stt/transcribe-async/:jobId",
  proxyAuthenticatedAsyncTranscriptionStatus,
);
app.post("/v2/sync-capture-manifest", proxyAuthenticatedJobs);
app.post("/v2/sync-local-files", proxyAuthenticatedJobs);
app.get("/v2/sync-local-files/:jobId", proxyAuthenticatedJobs);
app.post("/v1/sync/audio/:conversationId/precache", proxyAuthenticatedJobs);
app.get("/v1/sync/audio/:conversationId/urls", proxyAuthenticatedCore);
app.get("/v3/speech-profile", proxyAuthenticatedCore);
app.get("/v4/speech-profile", proxyAuthenticatedCore);
app.get("/v3/speech-profile/status", proxyAuthenticatedCore);
app.post("/v3/upload-audio", proxyAuthenticatedCore);
app.get("/v3/speech-profile/expand", proxyAuthenticatedCore);
app.delete("/v3/speech-profile/expand", proxyAuthenticatedCore);
app.get(
  "/v1/sync/audio/:conversationId/:audioFileId",
  proxyConversationAudioDownload,
);
app.post("/v1/embeddings-workers-ai", proxyAuthenticatedAI);
app.post("/v1/stt/transcribe", proxyAuthenticatedAI);
app.get("/v1/account/cutover/control", proxyAuthenticatedCore);
app.all("/v1/cf/probe", proxyAuthenticatedCore);
app.all("/v1/cf/assets/*", proxyAuthenticatedCore);
app.get("/v1/advice", proxyAuthenticatedCore);
app.post("/v1/advice", proxyAuthenticatedCore);
app.post("/v1/advice/mark-all-read", proxyAuthenticatedCore);
app.patch("/v1/advice/:adviceId", proxyAuthenticatedCore);
app.delete("/v1/advice/:adviceId", proxyAuthenticatedCore);
app.get("/v3/memories", proxyAuthenticatedCore);
app.post("/v3/memories", proxyAuthenticatedCore);
app.post("/v3/memories/batch", proxyAuthenticatedCore);
app.delete("/v3/memories", proxyAuthenticatedCore);
app.delete("/v3/memories/batch", proxyAuthenticatedCore);
app.delete("/v3/memories/:memoryId", proxyAuthenticatedCore);
app.patch("/v3/memories/:memoryId", proxyAuthenticatedCore);
app.patch("/v3/memories/:memoryId/visibility", proxyAuthenticatedCore);
app.patch("/v3/memories/:memoryId/read", proxyAuthenticatedCore);
app.patch("/v3/memories/:memoryId/baseline", proxyAuthenticatedCore);
app.post("/v3/memories/:memoryId/review", proxyAuthenticatedCore);
app.get("/v1/conversations/:conversationId/shared", proxyPublicCore);
app.get("/v1/conversations", proxyAuthenticatedCore);
app.post("/v1/conversations/from-segments", proxyAuthenticatedCore);
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
app.get(
  "/v1/conversations/:conversationId/suggested-apps",
  proxyAuthenticatedCore,
);
app.get("/v1/conversations/:conversationId/recording", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/events", proxyAuthenticatedCore);
app.patch("/v1/conversations/:conversationId/summary", proxyAuthenticatedCore);
app.delete(
  "/v1/conversations/:conversationId/calendar-event",
  proxyAuthenticatedCore,
);
app.post(
  "/v1/conversations/:conversationId/calendar-event",
  proxyAuthenticatedJobs,
);
app.post(
  "/v1/conversations/:conversationId/calendar-event/auto-link",
  proxyAuthenticatedJobs,
);
app.post("/v1/tools/calendar-events", proxyAuthenticatedJobs);
app.patch(
  "/v1/conversations/:conversationId/segments/text",
  proxyAuthenticatedCore,
);
app.patch(
  "/v1/conversations/:conversationId/segments/:segmentIdx/assign",
  proxyAuthenticatedCore,
);
app.patch(
  "/v1/conversations/:conversationId/assign-speaker/:speakerId",
  proxyAuthenticatedCore,
);
app.patch(
  "/v1/conversations/:conversationId/visibility",
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
app.delete("/v1/users/store-recording-permission", proxyAuthenticatedJobs);
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
app.get("/v1/action-items/search", proxyAuthenticatedCore);
app.patch("/v1/action-items/batch", proxyAuthenticatedCore);
app.post("/v1/action-items/batch", proxyAuthenticatedCore);
app.post("/v1/action-items/batch-delete", proxyAuthenticatedCore);
app.get("/v1/action-items/pending-sync", proxyAuthenticatedCore);
app.patch("/v1/action-items/sync-batch", proxyAuthenticatedCore);
app.post("/v1/action-items/share", proxyAuthenticatedCore);
app.post("/v1/action-items/accept", proxyAuthenticatedCore);
app.post(
  "/v1/action-items/restore-legacy-conversation-items",
  proxyAuthenticatedCore,
);
app.delete("/v1/import/limitless/conversations", proxyAuthenticatedCore);
app.post("/v1/staged-tasks/migrate", proxyAuthenticatedCore);
app.post("/v1/staged-tasks/migrate-conversation-items", proxyAuthenticatedCore);
app.post("/v1/chat-first/blocks/validate", proxyAuthenticatedCore);
app.post("/v1/chat/deferrals", proxyAuthenticatedCore);
app.get("/v1/tools/conversations", proxyAuthenticatedCore);
app.post("/v1/tools/conversations/search", proxyAuthenticatedCore);
app.post("/v1/tools/conversations/search-chunks", proxyAuthenticatedCore);
app.get("/v1/tools/memories", proxyAuthenticatedCore);
app.post("/v1/tools/memories/search", proxyAuthenticatedCore);
app.get("/v1/tools/action-items", proxyAuthenticatedCore);
app.post("/v1/tools/action-items", proxyAuthenticatedCore);
app.patch("/v1/tools/action-items/:actionItemId", proxyAuthenticatedCore);
app.get("/v1/knowledge-graph", proxyAuthenticatedCore);
app.delete("/v1/knowledge-graph", proxyAuthenticatedCore);
app.get("/v1/knowledge-graph/canonical", proxyAuthenticatedCore);
app.post("/v1/knowledge-graph/extract", proxyAuthenticatedCore);
app.post("/v1/knowledge-graph/rebuild", proxyAuthenticatedCore);
app.post("/v1/memories/extract", proxyAuthenticatedCore);
app.post("/v1/connectors/synthesize", proxyAuthenticatedCore);
app.post("/v1/conversations/topic", proxyAuthenticatedCore);
app.get("/v1/daily-score", proxyAuthenticatedCore);
app.get("/v1/scores", proxyAuthenticatedCore);
app.post("/v1/focus-sessions", proxyAuthenticatedCore);
app.get("/v1/focus-sessions", proxyAuthenticatedCore);
app.delete("/v1/focus-sessions/:sessionId", proxyAuthenticatedCore);
app.get("/v1/focus-stats", proxyAuthenticatedCore);
app.post("/v1/screen-activity/sync", proxyAuthenticatedCore);
app.get("/v1/screen-activity", proxyAuthenticatedCore);
app.get("/v1/screen-activity/summary", proxyAuthenticatedCore);
app.get("/v1/crisp/unread", proxyAuthenticatedCore);
app.get("/v1/integrations/:app_key", proxyAuthenticatedCore);
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
app.delete(
  "/v1/users/people/:personId/speech-samples/:sampleIndex",
  proxyAuthenticatedCore,
);
app.get("/v1/goals", proxyAuthenticatedCore);
app.post("/v1/goals", proxyAuthenticatedCore);
app.get("/v1/goals/all", proxyAuthenticatedCore);
app.get("/v1/goals/suggest", proxyAuthenticatedCore);
app.get("/v1/goals/advice", proxyAuthenticatedCore);
app.post("/v1/goals/extract-progress", proxyAuthenticatedCore);
app.get("/v1/goals/canonical/list", proxyAuthenticatedCore);
app.post("/v1/goals/canonical", proxyAuthenticatedCore);
app.get("/v1/goals/:goalId/advice", proxyAuthenticatedCore);
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
app.get("/v1/users/analytics/memory_summary", proxyAuthenticatedCore);
app.post("/v1/users/analytics/memory_summary", proxyAuthenticatedCore);
app.post("/v1/users/analytics/chat_message", proxyAuthenticatedCore);
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
app.post("/v1/users/ai-profile/synthesize", proxyAuthenticatedCore);
app.get("/v1/users/profile", proxyAuthenticatedAuthProfile);
app.get("/v1/mcp/oauth/grants", proxyAuthenticatedMcpGrants);
app.delete("/v1/mcp/oauth/grants/:grantId", proxyAuthenticatedMcpGrants);
app.get("/v1/users/location-context-consent", proxyAuthenticatedCore);
app.put("/v1/users/location-context-consent", proxyAuthenticatedCore);
app.post("/v1/tts/synthesize", proxyAuthenticatedAI);
app.post("/v1/tts/synthesize-workers-ai", proxyAuthenticatedAI);
app.post("/v2/tts/synthesize", proxyAuthenticatedAI);
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
    return proxyLegacyBackend(c);
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
