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
  "chat:initial": {
    name: "chat:initial",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "apps:generate_prompts": {
    name: "apps:generate_prompts",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "apps:generate_description": {
    name: "apps:generate_description",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "apps:generate_description_emoji": {
    name: "apps:generate_description_emoji",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "apps:generate_app": {
    name: "apps:generate_app",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "conversations:search": {
    name: "conversations:search",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "conversations:from-segments": {
    name: "conversations:from-segments",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "dev:conversations": {
    name: "dev:conversations",
    maxRequests: 25,
    windowSeconds: 3600,
  },
  "dev:goals_write": {
    name: "dev:goals_write",
    maxRequests: 120,
    windowSeconds: 3600,
  },
  "memories:create": {
    name: "memories:create",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "memories:batch": {
    name: "memories:batch",
    maxRequests: 30,
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
  "memory_imports:batch": {
    name: "memory_imports:batch",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "knowledge_graph:canonical": {
    name: "knowledge_graph:canonical",
    maxRequests: 120,
    windowSeconds: 3600,
  },
  "knowledge_graph:extract": {
    name: "knowledge_graph:extract",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "knowledge_graph:rebuild": {
    name: "knowledge_graph:rebuild",
    maxRequests: 2,
    windowSeconds: 3600,
  },
  "memories:extract": {
    name: "memories:extract",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "connectors:synthesize": {
    name: "connectors:synthesize",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "conversations:topic": {
    name: "conversations:topic",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "users:ai_profile_synthesize": {
    name: "users:ai_profile_synthesize",
    maxRequests: 8,
    windowSeconds: 86400,
  },
  "goals:suggest": {
    name: "goals:suggest",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "goals:advice": {
    name: "goals:advice",
    maxRequests: 30,
    windowSeconds: 3600,
  },
  "goals:extract": {
    name: "goals:extract",
    maxRequests: 30,
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
  "tools:mutate": {
    name: "tools:mutate",
    maxRequests: 60,
    windowSeconds: 3600,
  },
  "tools:search": {
    name: "tools:search",
    maxRequests: 60,
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
  ["POST /v2/tts/synthesize", TTS_SYNTHESIZE_RATE_LIMIT],
  ["POST /v2/messages", EDGE_RATE_LIMIT_POLICIES["chat:send_message"]],
  ["POST /v1/initial-message", EDGE_RATE_LIMIT_POLICIES["chat:initial"]],
  ["POST /v2/initial-message", EDGE_RATE_LIMIT_POLICIES["chat:initial"]],
  ["POST /v2/chat/initial-message", EDGE_RATE_LIMIT_POLICIES["chat:initial"]],
  ["POST /v2/chat/generate-title", EDGE_RATE_LIMIT_POLICIES["chat:initial"]],
  [
    "GET /v1/app/generate-prompts",
    EDGE_RATE_LIMIT_POLICIES["apps:generate_prompts"],
  ],
  [
    "POST /v1/app/generate-description",
    EDGE_RATE_LIMIT_POLICIES["apps:generate_description"],
  ],
  [
    "POST /v1/app/generate-description-emoji",
    EDGE_RATE_LIMIT_POLICIES["apps:generate_description_emoji"],
  ],
  ["POST /v1/app/generate", EDGE_RATE_LIMIT_POLICIES["apps:generate_app"]],
  ["POST /v1/stt/transcribe", STT_TRANSCRIBE_RATE_LIMIT],
  ["POST /v1/stt/transcribe-workers-ai", STT_TRANSCRIBE_RATE_LIMIT],
  ["POST /v1/stt/transcribe-async", STT_TRANSCRIBE_RATE_LIMIT],
  ["POST /v2/voice-message/transcribe", STT_TRANSCRIBE_RATE_LIMIT],
  [
    "POST /v1/tools/conversations/search",
    EDGE_RATE_LIMIT_POLICIES["tools:search"],
  ],
  [
    "POST /v1/tools/conversations/search-chunks",
    EDGE_RATE_LIMIT_POLICIES["tools:search"],
  ],
  ["POST /v1/tools/memories/search", EDGE_RATE_LIMIT_POLICIES["tools:search"]],
  ["POST /v1/tools/action-items", EDGE_RATE_LIMIT_POLICIES["tools:mutate"]],
  [
    "POST /v1/conversations/search",
    EDGE_RATE_LIMIT_POLICIES["conversations:search"],
  ],
  [
    "POST /v1/conversations/from-segments",
    EDGE_RATE_LIMIT_POLICIES["conversations:from-segments"],
  ],
  ["POST /v3/memories", EDGE_RATE_LIMIT_POLICIES["memories:create"]],
  ["POST /v3/memories/batch", EDGE_RATE_LIMIT_POLICIES["memories:batch"]],
  [
    "POST /v3/memory-imports/batch",
    EDGE_RATE_LIMIT_POLICIES["memory_imports:batch"],
  ],
  ["POST /v1/mcp/memories", EDGE_RATE_LIMIT_POLICIES["memories:create"]],
  ["POST /v1/mcp/action-items", EDGE_RATE_LIMIT_POLICIES["action_items:write"]],
  ["POST /v1/dev/user/memories", EDGE_RATE_LIMIT_POLICIES["memories:create"]],
  [
    "POST /v1/dev/user/memories/batch",
    EDGE_RATE_LIMIT_POLICIES["memories:batch"],
  ],
  [
    "POST /v1/dev/user/action-items",
    EDGE_RATE_LIMIT_POLICIES["action_items:write"],
  ],
  [
    "POST /v1/dev/user/action-items/batch",
    EDGE_RATE_LIMIT_POLICIES["action_items:write"],
  ],
  [
    "POST /v1/dev/user/conversations",
    EDGE_RATE_LIMIT_POLICIES["dev:conversations"],
  ],
  [
    "POST /v1/dev/user/conversations/from-segments",
    EDGE_RATE_LIMIT_POLICIES["dev:conversations"],
  ],
  ["POST /v1/dev/user/goals", EDGE_RATE_LIMIT_POLICIES["dev:goals_write"]],
  [
    "GET /v1/knowledge-graph/canonical",
    EDGE_RATE_LIMIT_POLICIES["knowledge_graph:canonical"],
  ],
  [
    "POST /v1/knowledge-graph/extract",
    EDGE_RATE_LIMIT_POLICIES["knowledge_graph:extract"],
  ],
  [
    "POST /v1/knowledge-graph/rebuild",
    EDGE_RATE_LIMIT_POLICIES["knowledge_graph:rebuild"],
  ],
  ["POST /v1/memories/extract", EDGE_RATE_LIMIT_POLICIES["memories:extract"]],
  [
    "POST /v1/connectors/synthesize",
    EDGE_RATE_LIMIT_POLICIES["connectors:synthesize"],
  ],
  [
    "POST /v1/conversations/topic",
    EDGE_RATE_LIMIT_POLICIES["conversations:topic"],
  ],
  [
    "POST /v1/users/ai-profile/synthesize",
    EDGE_RATE_LIMIT_POLICIES["users:ai_profile_synthesize"],
  ],
  ["GET /v1/goals/suggest", EDGE_RATE_LIMIT_POLICIES["goals:suggest"]],
  ["GET /v1/goals/advice", EDGE_RATE_LIMIT_POLICIES["goals:advice"]],
  [
    "POST /v1/goals/extract-progress",
    EDGE_RATE_LIMIT_POLICIES["goals:extract"],
  ],
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

  if (normalizedMethod === "GET" && /^\/v1\/goals\/[^/]+\/advice$/.test(path)) {
    return EDGE_RATE_LIMIT_POLICIES["goals:advice"];
  }

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
    (normalizedMethod === "PATCH" || normalizedMethod === "DELETE") &&
    /^\/v1\/dev\/user\/memories\/[^/]+$/.test(path)
  ) {
    return normalizedMethod === "DELETE"
      ? EDGE_RATE_LIMIT_POLICIES["memories:delete"]
      : EDGE_RATE_LIMIT_POLICIES["memories:modify"];
  }
  if (
    normalizedMethod === "PATCH" &&
    /^\/v1\/tools\/action-items\/[^/]+$/.test(path)
  ) {
    return EDGE_RATE_LIMIT_POLICIES["tools:mutate"];
  }
  if (
    (normalizedMethod === "POST" &&
      /^\/v1\/mcp\/action-items\/[^/]+\/complete$/.test(path)) ||
    ((normalizedMethod === "PATCH" || normalizedMethod === "DELETE") &&
      /^\/v1\/mcp\/action-items\/[^/]+$/.test(path))
  ) {
    return EDGE_RATE_LIMIT_POLICIES["action_items:write"];
  }
  if (
    (normalizedMethod === "PATCH" || normalizedMethod === "DELETE") &&
    /^\/v1\/dev\/user\/action-items\/[^/]+$/.test(path)
  ) {
    return EDGE_RATE_LIMIT_POLICIES["action_items:write"];
  }
  if (
    (normalizedMethod === "PATCH" || normalizedMethod === "DELETE") &&
    /^\/v1\/dev\/user\/conversations\/[^/]+$/.test(path)
  ) {
    return EDGE_RATE_LIMIT_POLICIES["dev:conversations"];
  }
  if (
    (normalizedMethod === "PATCH" || normalizedMethod === "DELETE") &&
    /^\/v1\/dev\/user\/goals\/[^/]+(?:\/progress)?$/.test(path)
  ) {
    return EDGE_RATE_LIMIT_POLICIES["dev:goals_write"];
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
