import type { AuthContext } from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import type { EdgeEnv } from "./env";

export type RateLimitResult = {
  allowed: boolean;
  limit: number;
  remaining: number;
  retryAfter: number;
  resetAt: number;
};

export type EdgeRateLimitPolicy = {
  name: string;
  maxRequests: number;
  windowSeconds: number;
};

export const EDGE_RATE_LIMIT_POLICIES = {
  "action_items:write": {
    name: "action_items:write",
    maxRequests: 120,
    windowSeconds: 3600,
  },
  "chat:send_message": {
    name: "chat:send_message",
    maxRequests: 120,
    windowSeconds: 3600,
  },
  "conversations:search": {
    name: "conversations:search",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "memories:create": {
    name: "memories:create",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "memories:delete": {
    name: "memories:delete",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "memories:delete_all": {
    name: "memories:delete_all",
    maxRequests: 2,
    windowSeconds: 3600,
  },
  "memories:delete_batch": {
    name: "memories:delete_batch",
    maxRequests: 10,
    windowSeconds: 3600,
  },
  "memories:modify": {
    name: "memories:modify",
    maxRequests: 120,
    windowSeconds: 3600,
  },
  "stt:transcribe": {
    name: "stt:transcribe",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "tts:synthesize": {
    name: "tts:synthesize",
    maxRequests: 300,
    windowSeconds: 3600,
  },
} as const satisfies Record<string, EdgeRateLimitPolicy>;

export const TTS_SYNTHESIZE_RATE_LIMIT =
  EDGE_RATE_LIMIT_POLICIES["tts:synthesize"];
export const STT_TRANSCRIBE_RATE_LIMIT =
  EDGE_RATE_LIMIT_POLICIES["stt:transcribe"];

const EXACT_ROUTE_POLICIES = new Map<string, EdgeRateLimitPolicy>([
  ["POST /v1/tts/synthesize", TTS_SYNTHESIZE_RATE_LIMIT],
  ["POST /v1/tts/synthesize-workers-ai", TTS_SYNTHESIZE_RATE_LIMIT],
  ["POST /v2/messages", EDGE_RATE_LIMIT_POLICIES["chat:send_message"]],
  ["POST /v1/stt/transcribe", STT_TRANSCRIBE_RATE_LIMIT],
  ["POST /v1/stt/transcribe-workers-ai", STT_TRANSCRIBE_RATE_LIMIT],
  ["POST /v1/stt/transcribe-async", STT_TRANSCRIBE_RATE_LIMIT],
  ["POST /v2/voice-message/transcribe", STT_TRANSCRIBE_RATE_LIMIT],
  [
    "POST /v1/conversations/search",
    EDGE_RATE_LIMIT_POLICIES["conversations:search"],
  ],
  ["POST /v3/memories", EDGE_RATE_LIMIT_POLICIES["memories:create"]],
  ["POST /v1/mcp/memories", EDGE_RATE_LIMIT_POLICIES["memories:create"]],
  ["POST /v1/mcp/action-items", EDGE_RATE_LIMIT_POLICIES["action_items:write"]],
  ["DELETE /v3/memories", EDGE_RATE_LIMIT_POLICIES["memories:delete_all"]],
  [
    "DELETE /v3/memories/batch",
    EDGE_RATE_LIMIT_POLICIES["memories:delete_batch"],
  ],
]);

export function edgeRateLimitPolicyForRequest(
  method: string,
  path: string,
): EdgeRateLimitPolicy | null {
  const normalizedMethod = method.toUpperCase();
  const exact = EXACT_ROUTE_POLICIES.get(`${normalizedMethod} ${path}`);
  if (exact) return exact;

  if (normalizedMethod === "DELETE" && /^\/v3\/memories\/[^/]+$/.test(path)) {
    return EDGE_RATE_LIMIT_POLICIES["memories:delete"];
  }
  if (
    (normalizedMethod === "PATCH" &&
      /^\/v3\/memories\/[^/]+(?:\/(?:visibility|read|baseline))?$/.test(
        path,
      )) ||
    (normalizedMethod === "POST" &&
      /^\/v3\/memories\/[^/]+\/review$/.test(path))
  ) {
    return EDGE_RATE_LIMIT_POLICIES["memories:modify"];
  }
  if (
    (normalizedMethod === "POST" &&
      /^\/v1\/mcp\/action-items\/[^/]+\/complete$/.test(path)) ||
    ((normalizedMethod === "PATCH" || normalizedMethod === "DELETE") &&
      /^\/v1\/mcp\/action-items\/[^/]+$/.test(path))
  ) {
    return EDGE_RATE_LIMIT_POLICIES["action_items:write"];
  }
  return null;
}

function parseResult(value: unknown): RateLimitResult | null {
  if (!value || typeof value !== "object") return null;
  const result = value as Partial<RateLimitResult>;
  if (
    typeof result.allowed !== "boolean" ||
    !Number.isInteger(result.limit) ||
    (result.limit as number) < 1 ||
    !Number.isInteger(result.remaining) ||
    (result.remaining as number) < 0 ||
    !Number.isInteger(result.retryAfter) ||
    (result.retryAfter as number) < 0 ||
    !Number.isFinite(result.resetAt)
  ) {
    return null;
  }
  return result as RateLimitResult;
}

export async function enforceEdgeRateLimit(
  env: EdgeEnv,
  auth: AuthContext,
  policy: EdgeRateLimitPolicy,
  requestId: string,
): Promise<Response | null> {
  const maxRequests = effectiveMaxRequests(env.RATE_LIMIT_BOOST, policy);
  try {
    const id = env.RATE_LIMITS.idFromName(`${policy.name}:${auth.uid}`);
    const response = await env.RATE_LIMITS.get(id).fetch(
      new Request("https://rate-limit.internal/check", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          policy: policy.name,
          max_requests: maxRequests,
          window_seconds: policy.windowSeconds,
        }),
      }),
    );
    if (!response.ok) {
      throw new Error("rate limit Durable Object rejected check");
    }
    const result = parseResult(await response.json());
    if (!result) {
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
    if (env.RATE_LIMIT_SHADOW_MODE?.toLowerCase() === "true") {
      console.warn(
        JSON.stringify({
          event: "rate_limit_shadow",
          policy: policy.name,
          retry_after: result.retryAfter,
          request_id: requestId,
        }),
      );
      return null;
    }
    return Response.json(
      {
        detail: `Rate limit exceeded. Try again in ${result.retryAfter}s.`,
      },
      {
        status: 429,
        headers: {
          "cache-control": "no-store",
          "x-ratelimit-limit": String(result.limit),
          "x-ratelimit-remaining": "0",
          "retry-after": String(result.retryAfter),
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

function effectiveMaxRequests(
  boostValue: string | undefined,
  policy: EdgeRateLimitPolicy,
): number {
  if (boostValue === undefined) return policy.maxRequests;
  const boost = Number(boostValue);
  if (!Number.isFinite(boost) || boost <= 0) return policy.maxRequests;
  return Math.max(1, Math.floor(policy.maxRequests * boost));
}
