import { recordFallback } from "./fallback";

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

export async function enforceSessionAdmission(
  env: { RATE_LIMITS?: DurableObjectNamespace },
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
      typeof result.retryAfter === "number" &&
      Number.isFinite(result.retryAfter)
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
