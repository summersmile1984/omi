import { createMcpHandler, McpServer } from "@modelcontextprotocol/server";
import { z } from "zod";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
  type AuthContext,
} from "../shared/auth-context";
import { requestId } from "../shared/request-id";
import type { EdgeEnv } from "./env";
import {
  edgeRateLimitPolicyForRequest,
  enforceEdgeRateLimit,
} from "./rate-limit";

export const MCP_SCOPES = [
  "action_items.read",
  "action_items.write",
  "chat.read",
  "conversations.read",
  "goals.read",
  "memories.read",
  "memories.write",
  "people.read",
  "screen_activity.read",
] as const;

type McpScope = (typeof MCP_SCOPES)[number];
type McpPrincipal = {
  uid: string;
  scopes: McpScope[];
  authType: "api_key" | "oauth";
  clientId: string | null;
  authorization: string | null;
};

type Authentication =
  | { principal: McpPrincipal; denial?: never }
  | { denial: Response; principal?: never };

type CoreCall = {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  path: string;
  query?: Record<string, string | number | boolean | string[] | undefined>;
  body?: unknown;
};

const API_KEY_PATTERN = /^Bearer omi_mcp_[0-9a-f]{32}$/;
const MCP_SCOPE_SET = new Set<string>(MCP_SCOPES);
const READ_ONLY = {
  readOnlyHint: true,
  destructiveHint: false,
  openWorldHint: false,
} as const;
const WRITE = {
  readOnlyHint: false,
  destructiveHint: false,
  openWorldHint: false,
} as const;
const DESTRUCTIVE_WRITE = {
  readOnlyHint: false,
  destructiveHint: true,
  openWorldHint: false,
} as const;
const emptyInput = z.object({});
const boundedId = z
  .string()
  .min(1)
  .max(256)
  .regex(/^[^/]+$/);
const dateString = z.string().min(1).max(64);

function resourceUrl(env: EdgeEnv): string | null {
  if (!env.MCP_RESOURCE_URL) return null;
  try {
    const parsed = new URL(env.MCP_RESOURCE_URL);
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }
    return parsed.href;
  } catch {
    return null;
  }
}

export function mcpProtectedResourceMetadataUrl(env: EdgeEnv): string | null {
  const resource = resourceUrl(env);
  if (!resource) return null;
  const parsed = new URL(resource);
  return `${parsed.origin}/.well-known/oauth-protected-resource${parsed.pathname.replace(/\/$/, "")}`;
}

export function mcpProtectedResourceMetadata(env: EdgeEnv) {
  const resource = resourceUrl(env);
  const authorizationServer = env.MCP_AUTHORIZATION_SERVER_URL;
  if (!resource || !authorizationServer) return null;
  return {
    resource,
    authorization_servers: [authorizationServer],
    scopes_supported: [...MCP_SCOPES],
    bearer_methods_supported: ["header"],
    dpop_signing_alg_values_supported: [
      "ES256",
      "ES384",
      "ES512",
      "EdDSA",
      "RS256",
      "RS384",
      "RS512",
      "PS256",
      "PS384",
      "PS512",
    ],
  };
}

function jsonRpcAuthDenial(env: EdgeEnv): Response {
  const metadata = mcpProtectedResourceMetadataUrl(env);
  const headers = new Headers({
    "content-type": "application/json",
    "cache-control": "no-store",
  });
  if (metadata) {
    headers.set("www-authenticate", `Bearer resource_metadata="${metadata}"`);
  }
  return new Response(
    JSON.stringify({
      jsonrpc: "2.0",
      error: { code: -32000, message: "Authorization required" },
      id: null,
    }),
    { status: 401, headers },
  );
}

function validPrincipal(payload: unknown): McpPrincipal | null {
  if (!payload || typeof payload !== "object") return null;
  const value = payload as Record<string, unknown>;
  const uid = value.uid;
  const scopes = value.scopes;
  const authType = value.auth_type;
  const clientId = value.client_id;
  if (
    typeof uid !== "string" ||
    uid.length === 0 ||
    uid.length > 256 ||
    !Array.isArray(scopes) ||
    scopes.length > MCP_SCOPES.length ||
    scopes.some(
      (scope) => typeof scope !== "string" || !MCP_SCOPE_SET.has(scope),
    ) ||
    new Set(scopes).size !== scopes.length ||
    (authType !== "api_key" && authType !== "oauth") ||
    (clientId !== null &&
      (typeof clientId !== "string" ||
        clientId.length === 0 ||
        clientId.length > 2_048)) ||
    (authType === "oauth" && typeof clientId !== "string") ||
    (authType === "api_key" && clientId !== null)
  ) {
    return null;
  }
  return {
    uid,
    scopes: [...(scopes as McpScope[])].sort(),
    authType,
    clientId,
    authorization: null,
  };
}

async function corePrincipal(
  env: EdgeEnv,
  principal: McpPrincipal | null,
  authorization: string | null,
  id: string,
): Promise<Authentication> {
  if (!env.INTERNAL_ASSERTION_SECRET) {
    return {
      denial: Response.json(
        { error: "mcp authentication unavailable" },
        { status: 503 },
      ),
    };
  }
  const path = "/internal/mcp/principal";
  const headers = new Headers({ "x-request-id": id });
  if (principal?.authType === "oauth") {
    const signed = await createSignedAuthContext(
      {
        uid: principal.uid,
        authority: "mcp-oauth",
        scopes: principal.scopes,
        oauthClientId: principal.clientId || undefined,
        requestId: id,
      },
      "api-core",
      "GET",
      path,
      env.INTERNAL_ASSERTION_SECRET,
    );
    if (!signed) {
      return {
        denial: Response.json(
          { error: "mcp authentication unavailable" },
          { status: 503 },
        ),
      };
    }
    headers.set(AUTH_CONTEXT_HEADER, signed.encoded);
    headers.set(AUTH_SIGNATURE_HEADER, signed.signature);
  } else {
    headers.set("x-internal-assertion-secret", env.INTERNAL_ASSERTION_SECRET);
    if (authorization) headers.set("authorization", authorization);
  }
  try {
    const response = await env.API_CORE.fetch(
      new Request(`https://api-core.internal${path}`, { headers }),
    );
    if (!response.ok) return { denial: response };
    const verified = validPrincipal(await response.json());
    if (
      !verified ||
      (principal &&
        (verified.uid !== principal.uid ||
          verified.authType !== principal.authType ||
          verified.clientId !== principal.clientId ||
          verified.scopes.join("\u0000") !== principal.scopes.join("\u0000")))
    ) {
      return {
        denial: Response.json(
          { error: "mcp authentication unavailable" },
          { status: 503 },
        ),
      };
    }
    verified.authorization = authorization;
    return { principal: verified };
  } catch {
    return {
      denial: Response.json(
        { error: "mcp authentication unavailable" },
        { status: 503 },
      ),
    };
  }
}

async function authenticateMcpRequest(
  request: Request,
  env: EdgeEnv,
  id: string,
): Promise<Authentication> {
  const configuredResource = resourceUrl(env);
  if (!configuredResource || !env.INTERNAL_ASSERTION_SECRET) {
    return {
      denial: Response.json(
        { error: "mcp authentication unavailable" },
        { status: 503 },
      ),
    };
  }
  const requested = new URL(request.url);
  const canonical = new URL(configuredResource);
  if (
    requested.origin !== canonical.origin ||
    requested.pathname !== canonical.pathname
  ) {
    return { denial: Response.json({ error: "not_found" }, { status: 404 }) };
  }
  const authorization = request.headers.get("authorization");
  if (!authorization) return { denial: jsonRpcAuthDenial(env) };
  if (API_KEY_PATTERN.test(authorization)) {
    return corePrincipal(env, null, authorization, id);
  }

  const headers = new Headers({
    authorization,
    "content-type": "application/json",
    "x-internal-assertion-secret": env.INTERNAL_ASSERTION_SECRET,
    "x-request-id": id,
  });
  const dpop = request.headers.get("dpop");
  if (dpop) headers.set("dpop", dpop);
  try {
    const verification = await env.AUTH.fetch(
      new Request("https://auth.internal/internal/mcp/verify", {
        method: "POST",
        headers,
        body: JSON.stringify({
          method: request.method.toUpperCase(),
          url: configuredResource,
        }),
      }),
    );
    if (!verification.ok) return { denial: verification };
    const payload = (await verification.json()) as Record<string, unknown>;
    const principal = validPrincipal({
      uid: payload.uid,
      scopes: payload.scopes,
      auth_type: "oauth",
      client_id: payload.clientId,
    });
    if (!principal) {
      return {
        denial: Response.json(
          { error: "mcp authentication unavailable" },
          { status: 503 },
        ),
      };
    }
    return corePrincipal(env, principal, null, id);
  } catch {
    return {
      denial: Response.json(
        { error: "mcp authentication unavailable" },
        { status: 503 },
      ),
    };
  }
}

function appendQuery(url: URL, query: CoreCall["query"]) {
  for (const [key, value] of Object.entries(query || {})) {
    if (value === undefined) continue;
    url.searchParams.set(
      key,
      Array.isArray(value) ? value.join(",") : String(value),
    );
  }
}

async function coreToolResult(
  env: EdgeEnv,
  principal: McpPrincipal,
  call: CoreCall,
  id: string,
) {
  const url = new URL(call.path, "https://api-core.internal");
  appendQuery(url, call.query);
  const headers = new Headers({ "x-request-id": id });
  if (call.body !== undefined) headers.set("content-type", "application/json");
  if (principal.authType === "api_key") {
    if (!principal.authorization) {
      return {
        content: [{ type: "text" as const, text: "MCP credential missing" }],
        isError: true,
      };
    }
    headers.set("authorization", principal.authorization);
  } else {
    const signed = await createSignedAuthContext(
      {
        uid: principal.uid,
        authority: "mcp-oauth",
        scopes: principal.scopes,
        oauthClientId: principal.clientId || undefined,
        requestId: id,
      },
      "api-core",
      call.method,
      call.path,
      env.INTERNAL_ASSERTION_SECRET,
    );
    if (!signed) {
      return {
        content: [
          { type: "text" as const, text: "MCP authorization unavailable" },
        ],
        isError: true,
      };
    }
    headers.set(AUTH_CONTEXT_HEADER, signed.encoded);
    headers.set(AUTH_SIGNATURE_HEADER, signed.signature);
  }

  const policy = edgeRateLimitPolicyForRequest(call.method, call.path);
  if (policy) {
    const rateIdentity: AuthContext = {
      uid: principal.uid,
      authority: principal.authType === "oauth" ? "mcp-oauth" : "internal",
      ...(principal.authType === "oauth"
        ? {
            scopes: principal.scopes,
            oauthClientId: principal.clientId || undefined,
          }
        : {}),
      requestId: id,
    };
    const denial = await enforceEdgeRateLimit(env, rateIdentity, policy, id);
    if (denial) {
      return {
        content: [
          {
            type: "text" as const,
            text: `MCP tool request failed (${denial.status}): rate limit exceeded`,
          },
        ],
        isError: true,
      };
    }
  }

  try {
    const response = await env.API_CORE.fetch(
      new Request(url, {
        method: call.method,
        headers,
        body: call.body === undefined ? undefined : JSON.stringify(call.body),
      }),
    );
    const raw = (await response.text()).slice(0, 256_000);
    let payload: unknown = raw;
    try {
      payload = raw ? JSON.parse(raw) : null;
    } catch {
      // Preserve a bounded non-JSON upstream error as text.
    }
    if (!response.ok) {
      const detail =
        payload && typeof payload === "object"
          ? ((payload as Record<string, unknown>).detail ??
            (payload as Record<string, unknown>).error ??
            `HTTP ${response.status}`)
          : payload || `HTTP ${response.status}`;
      return {
        content: [
          {
            type: "text" as const,
            text: `MCP tool request failed (${response.status}): ${String(detail).slice(0, 2_000)}`,
          },
        ],
        isError: true,
      };
    }
    return {
      content: [
        {
          type: "text" as const,
          text: JSON.stringify(payload, null, 2),
        },
      ],
    };
  } catch {
    return {
      content: [
        { type: "text" as const, text: "MCP tool service unavailable" },
      ],
      isError: true,
    };
  }
}

function buildMcpServer(env: EdgeEnv, principal: McpPrincipal, id: string) {
  const server = new McpServer({ name: "omi", version: "1.0.0" });
  const scopes = new Set(principal.scopes);

  const register = <Schema extends z.ZodObject>(
    scope: McpScope,
    name: string,
    description: string,
    inputSchema: Schema,
    annotations: typeof READ_ONLY | typeof WRITE | typeof DESTRUCTIVE_WRITE,
    invoke: (args: z.infer<Schema>) => CoreCall,
  ) => {
    if (!scopes.has(scope)) return;
    server.registerTool(
      name,
      { description, inputSchema: inputSchema.shape, annotations },
      async (args) =>
        coreToolResult(env, principal, invoke(args as z.infer<Schema>), id),
    );
  };

  register(
    "memories.read",
    "get_user_profile",
    "Get Omi's cached high-level summary of the user.",
    emptyInput,
    READ_ONLY,
    () => ({ method: "GET", path: "/v1/mcp/profile" }),
  );
  register(
    "memories.read",
    "get_memories",
    "Retrieve known facts about the user.",
    z.object({
      categories: z.array(z.string().max(64)).max(8).optional(),
      limit: z.number().int().min(1).max(500).optional(),
      offset: z.number().int().min(0).max(100_000).optional(),
      sort: z
        .enum(["scoring_desc", "created_desc", "updated_desc", "manual_first"])
        .optional(),
      reviewed: z.boolean().optional(),
      manually_added: z.boolean().optional(),
      updated_after: z.string().max(64).optional(),
      include_activity: z.boolean().optional(),
      include_sensitive: z.boolean().optional(),
    }),
    READ_ONLY,
    (args) => ({ method: "GET", path: "/v1/mcp/memories", query: args }),
  );
  register(
    "memories.write",
    "create_memory",
    "Create a new memory about the user.",
    z.object({
      content: z.string().min(1).max(50_000),
      category: z.string().max(64).optional(),
    }),
    WRITE,
    (args) => ({ method: "POST", path: "/v1/mcp/memories", body: args }),
  );
  register(
    "memories.write",
    "delete_memory",
    "Delete a memory by ID.",
    z.object({ memory_id: boundedId }),
    DESTRUCTIVE_WRITE,
    ({ memory_id }) => ({
      method: "DELETE",
      path: `/v1/mcp/memories/${encodeURIComponent(memory_id)}`,
    }),
  );
  register(
    "memories.write",
    "edit_memory",
    "Edit a memory's content.",
    z.object({ memory_id: boundedId, content: z.string().min(1).max(50_000) }),
    DESTRUCTIVE_WRITE,
    ({ memory_id, content }) => ({
      method: "PATCH",
      path: `/v1/mcp/memories/${encodeURIComponent(memory_id)}`,
      query: { value: content },
    }),
  );
  register(
    "memories.read",
    "search_memories",
    "Semantic search across the user's memories.",
    z.object({
      query: z.string().min(1).max(2_000),
      limit: z.number().int().min(1).max(20).optional(),
    }),
    READ_ONLY,
    (args) => ({
      method: "GET",
      path: "/v1/mcp/memories/search",
      query: args,
    }),
  );
  register(
    "memories.read",
    "get_x_posts",
    "Retrieve the user's imported X posts, newest first.",
    z.object({
      kind: z.enum(["tweet", "bookmark"]).optional(),
      limit: z.number().int().min(1).max(200).optional(),
    }),
    READ_ONLY,
    (args) => ({ method: "GET", path: "/v1/mcp/x-posts", query: args }),
  );
  register(
    "memories.read",
    "search_x_posts",
    "Semantic search across the user's imported X posts and bookmarks.",
    z.object({
      query: z.string().min(1).max(2_000),
      limit: z.number().int().min(1).max(100).optional(),
    }),
    READ_ONLY,
    (args) => ({
      method: "GET",
      path: "/v1/mcp/x-posts/search",
      query: args,
    }),
  );
  register(
    "conversations.read",
    "get_conversations",
    "Retrieve conversation metadata; use get_conversation_by_id for transcripts.",
    z.object({
      start_date: dateString.optional(),
      end_date: dateString.optional(),
      categories: z.array(z.string().max(64)).max(32).optional(),
      limit: z.number().int().min(1).max(1_000).optional(),
      offset: z.number().int().min(0).max(100_000).optional(),
    }),
    READ_ONLY,
    (args) => ({
      method: "GET",
      path: "/v1/mcp/conversations",
      query: args,
    }),
  );
  register(
    "conversations.read",
    "get_conversation_by_id",
    "Retrieve a conversation by ID including transcript segments.",
    z.object({ conversation_id: boundedId }),
    READ_ONLY,
    ({ conversation_id }) => ({
      method: "GET",
      path: `/v1/mcp/conversations/${encodeURIComponent(conversation_id)}`,
    }),
  );
  register(
    "conversations.read",
    "search_conversations",
    "Semantic search across conversation summaries and transcript content.",
    z.object({
      query: z.string().min(1).max(2_000),
      start_date: dateString.optional(),
      end_date: dateString.optional(),
      limit: z.number().int().min(1).max(100).optional(),
    }),
    READ_ONLY,
    (args) => ({
      method: "GET",
      path: "/v1/mcp/conversations/search",
      query: args,
    }),
  );
  register(
    "action_items.read",
    "get_action_items",
    "Retrieve the user's action items and due dates.",
    z.object({
      completed: z.boolean().optional(),
      due_start_date: dateString.optional(),
      due_end_date: dateString.optional(),
      limit: z.number().int().min(1).max(500).optional(),
      offset: z.number().int().min(0).max(1_000_000).optional(),
    }),
    READ_ONLY,
    (args) => ({
      method: "GET",
      path: "/v1/mcp/action-items",
      query: args,
    }),
  );
  register(
    "action_items.read",
    "search_action_items",
    "Semantic search across the user's action items.",
    z.object({
      query: z.string().min(1).max(2_000),
      limit: z.number().int().min(1).max(50).optional(),
    }),
    READ_ONLY,
    (args) => ({
      method: "GET",
      path: "/v1/mcp/action-items/search",
      query: args,
    }),
  );
  register(
    "action_items.write",
    "create_action_item",
    "Create a new action item for the user.",
    z.object({
      description: z.string().min(1).max(2_000),
      due_at: dateString.optional(),
      completed: z.boolean().optional(),
    }),
    WRITE,
    (args) => ({
      method: "POST",
      path: "/v1/mcp/action-items",
      body: args,
    }),
  );
  register(
    "action_items.write",
    "complete_action_item",
    "Mark an action item complete or reopen it.",
    z.object({ action_item_id: boundedId, completed: z.boolean().optional() }),
    WRITE,
    ({ action_item_id, completed }) => ({
      method: "POST",
      path: `/v1/mcp/action-items/${encodeURIComponent(action_item_id)}/complete`,
      query: { completed },
    }),
  );
  register(
    "action_items.write",
    "update_action_item",
    "Update an action item's description and/or due date.",
    z.object({
      action_item_id: boundedId,
      description: z.string().min(1).max(2_000).optional(),
      due_at: dateString.optional(),
    }),
    WRITE,
    ({ action_item_id, ...body }) => ({
      method: "PATCH",
      path: `/v1/mcp/action-items/${encodeURIComponent(action_item_id)}`,
      body,
    }),
  );
  register(
    "action_items.write",
    "delete_action_item",
    "Delete an action item by ID.",
    z.object({ action_item_id: boundedId }),
    DESTRUCTIVE_WRITE,
    ({ action_item_id }) => ({
      method: "DELETE",
      path: `/v1/mcp/action-items/${encodeURIComponent(action_item_id)}`,
    }),
  );
  register(
    "goals.read",
    "get_goals",
    "Retrieve the user's active or historical goals.",
    z.object({ include_inactive: z.boolean().optional() }),
    READ_ONLY,
    (args) => ({ method: "GET", path: "/v1/mcp/goals", query: args }),
  );
  register(
    "chat.read",
    "get_chat_messages",
    "Retrieve the user's recent Omi chat history.",
    z.object({
      limit: z.number().int().min(1).max(200).optional(),
      offset: z.number().int().min(0).max(1_000_000).optional(),
    }),
    READ_ONLY,
    (args) => ({ method: "GET", path: "/v1/mcp/chat", query: args }),
  );
  register(
    "people.read",
    "get_people",
    "Retrieve recurring people identified in the user's conversations.",
    emptyInput,
    READ_ONLY,
    () => ({ method: "GET", path: "/v1/mcp/people" }),
  );
  register(
    "screen_activity.read",
    "get_screen_activity",
    "Retrieve or summarize desktop screen activity.",
    z.object({
      start_date: dateString.optional(),
      end_date: dateString.optional(),
      app: z.string().max(512).optional(),
      summary: z.boolean().optional(),
      limit: z.number().int().min(1).max(200).optional(),
    }),
    READ_ONLY,
    (args) => ({
      method: "GET",
      path: "/v1/mcp/screen-activity",
      query: args,
    }),
  );
  register(
    "conversations.read",
    "get_daily_summaries",
    "Retrieve Omi's per-day summaries of the user's life.",
    z.object({
      start_date: dateString.optional(),
      end_date: dateString.optional(),
      limit: z.number().int().min(1).max(100).optional(),
      offset: z.number().int().min(0).max(1_000_000).optional(),
    }),
    READ_ONLY,
    (args) => ({
      method: "GET",
      path: "/v1/mcp/daily-summaries",
      query: args,
    }),
  );
  return server;
}

export async function handleMcpTransport(
  request: Request,
  env: EdgeEnv,
): Promise<Response> {
  const id = requestId(request);
  const auth = await authenticateMcpRequest(request, env, id);
  if (auth.denial) return auth.denial;
  const principal = auth.principal;
  const handler = createMcpHandler(() => buildMcpServer(env, principal, id), {
    legacy: "stateless",
    responseMode: "json",
  });
  return handler.fetch(request, {
    authInfo: {
      token: "edge-verified",
      clientId: principal.clientId || "omi-mcp-api-key",
      scopes: principal.scopes,
      resource: new URL(resourceUrl(env) as string),
      extra: { uid: principal.uid, authType: principal.authType },
    },
  });
}
