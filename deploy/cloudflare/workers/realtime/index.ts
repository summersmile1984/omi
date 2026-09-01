import { Hono } from "hono";
import { verifyRequestAuthContext } from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import {
  REALTIME_BOOTSTRAP_HEADER,
  REALTIME_BOOTSTRAP_SIGNATURE_HEADER,
  verifyRealtimeBootstrap,
} from "../shared/realtime-bootstrap";
import type { RealtimeEnv } from "./env";
import { RealtimeSession } from "./session";

// Per-uid admission budget for new realtime sessions. Session Durable Objects
// are named by the client-supplied x-omi-session-id, so without this window a
// client varying that header opens an unbounded number of concurrent STT
// streams, each spending Workers AI minutes. The window bounds session
// creation per uid; reconnects inside one window stay comfortably under it.
export const REALTIME_SESSION_ADMISSION = {
  policy: "realtime:session_admission",
  maxRequests: 10,
  windowSeconds: 60,
} as const;

const app = new Hono<{ Bindings: RealtimeEnv }>();

app.get("/health", (c) =>
  c.json({ status: "ok", service: "realtime", version: "cf-10" }),
);

app.all("/v2/voice-message/transcribe-stream", (c) =>
  routeSession(c.req.raw, c.env),
);
app.all("/v4/listen", (c) => routeSession(c.req.raw, c.env));
app.all("/v4/web/listen", (c) => routeSession(c.req.raw, c.env));
app.all("/v1/omni/relay", (c) => routeSession(c.req.raw, c.env));

async function enforceSessionAdmission(
  env: RealtimeEnv,
  uid: string,
  requestId?: string,
): Promise<Response | null> {
  if (!env.RATE_LIMITS) {
    recordFallback({
      component: "rate_limit",
      from: "durable_object",
      to: "unlimited",
      reason: "dependency_unavailable",
      outcome: "degraded",
      requestId,
    });
    return null;
  }
  try {
    const id = env.RATE_LIMITS.idFromName(
      `${REALTIME_SESSION_ADMISSION.policy}:${uid}`,
    );
    const response = await env.RATE_LIMITS.get(id).fetch(
      new Request("https://rate-limit.internal/check", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          policy: REALTIME_SESSION_ADMISSION.policy,
          max_requests: REALTIME_SESSION_ADMISSION.maxRequests,
          window_seconds: REALTIME_SESSION_ADMISSION.windowSeconds,
        }),
      }),
    );
    if (!response.ok) {
      throw new Error("rate limit Durable Object rejected check");
    }
    const result = (await response.json()) as {
      allowed?: unknown;
      retryAfter?: unknown;
    };
    if (typeof result.allowed !== "boolean") {
      recordFallback({
        component: "rate_limit",
        from: "durable_object",
        to: "unlimited",
        reason: "invalid_response",
        outcome: "degraded",
        requestId,
      });
      return null;
    }
    if (result.allowed) return null;
    const retryAfter =
      typeof result.retryAfter === "number" && Number.isFinite(result.retryAfter)
        ? Math.max(1, Math.ceil(result.retryAfter))
        : REALTIME_SESSION_ADMISSION.windowSeconds;
    return Response.json(
      { error: "Rate limited, retry later" },
      {
        status: 429,
        headers: {
          "retry-after": String(retryAfter),
          "cache-control": "no-store",
        },
      },
    );
  } catch {
    recordFallback({
      component: "rate_limit",
      from: "durable_object",
      to: "unlimited",
      reason: "dependency_unavailable",
      outcome: "degraded",
      requestId,
    });
    return null;
  }
}

async function routeSession(
  request: Request,
  env: RealtimeEnv,
): Promise<Response> {
  if (request.headers.get("upgrade")?.toLowerCase() !== "websocket") {
    return Response.json(
      { error: "websocket upgrade required" },
      { status: 426 },
    );
  }
  if (new URL(request.url).pathname === "/v4/web/listen") {
    const bootstrap = await verifyRealtimeBootstrap(
      request.headers.get(REALTIME_BOOTSTRAP_HEADER),
      request.headers.get(REALTIME_BOOTSTRAP_SIGNATURE_HEADER),
      env.INTERNAL_ASSERTION_SECRET,
    );
    if (!bootstrap) {
      return Response.json({ error: "unauthorized" }, { status: 401 });
    }
    const id = env.REALTIME_SESSIONS.idFromName(`web:${bootstrap.sessionId}`);
    return env.REALTIME_SESSIONS.get(id).fetch(request);
  }
  const context = await verifyRequestAuthContext(
    request,
    "realtime",
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!context) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const limited = await enforceSessionAdmission(
    env,
    context.uid,
    context.requestId,
  );
  if (limited) return limited;
  const requestedSession = request.headers.get("x-omi-session-id") || "default";
  const id = env.REALTIME_SESSIONS.idFromName(
    `${context.uid}:${requestedSession}`,
  );
  const stub = env.REALTIME_SESSIONS.get(id);
  return stub.fetch(request);
}

export { RealtimeSession };
export default app;
