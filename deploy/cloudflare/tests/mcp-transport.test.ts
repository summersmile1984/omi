import { describe, expect, it, vi } from "vitest";
import {
  handleMcpTransport,
  mcpProtectedResourceMetadata,
  mcpProtectedResourceMetadataUrl,
} from "../workers/edge/mcp-transport";
import type { EdgeEnv } from "../workers/edge/env";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

const resource = "https://edge.test/v1/mcp/sse";
const authorizationServer = "https://web.test/api/better-auth";
const apiKey = `Bearer omi_mcp_${"a".repeat(32)}`;

function fetcher(
  implementation: (request: Request) => Promise<Response> | Response,
) {
  return { fetch: vi.fn(implementation) } as unknown as Fetcher;
}

function environment(options: {
  apiCore: (request: Request) => Promise<Response> | Response;
  auth?: (request: Request) => Promise<Response> | Response;
}): EdgeEnv {
  return {
    AUTH: fetcher(
      options.auth ||
        (() => Response.json({ error: "unexpected" }, { status: 500 })),
    ),
    API_CORE: fetcher(options.apiCore),
    API_AI: fetcher(() => Response.json({ status: "ok" })),
    REALTIME: fetcher(() => Response.json({ status: "ok" })),
    JOBS: fetcher(() => Response.json({ status: "ok" })),
    RATE_LIMITS: {} as DurableObjectNamespace,
    INTERNAL_ASSERTION_SECRET: "mcp-internal-secret",
    MCP_RESOURCE_URL: resource,
    MCP_AUTHORIZATION_SERVER_URL: authorizationServer,
  };
}

function request(
  method: string,
  params: Record<string, unknown> = {},
  authorization = apiKey,
) {
  return new Request(resource, {
    method: "POST",
    headers: {
      accept: "application/json, text/event-stream",
      authorization,
      "content-type": "application/json",
      "mcp-protocol-version": "2025-03-26",
      "x-request-id": "mcp-transport-test",
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
}

async function jsonRpc(response: Response) {
  const body = await response.text();
  if (!response.headers.get("content-type")?.includes("text/event-stream")) {
    return JSON.parse(body) as Record<string, unknown>;
  }
  const data = body
    .split("\n")
    .find((line) => line.startsWith("data:"))
    ?.slice(5)
    .trim();
  if (!data) throw new Error(`missing SSE data: ${body}`);
  return JSON.parse(data) as Record<string, unknown>;
}

describe("Cloudflare MCP transport", () => {
  it("publishes path-aware protected-resource metadata", () => {
    const env = environment({
      apiCore: () => Response.json({ error: "unexpected" }, { status: 500 }),
    });
    expect(mcpProtectedResourceMetadataUrl(env)).toBe(
      "https://edge.test/.well-known/oauth-protected-resource/v1/mcp/sse",
    );
    expect(mcpProtectedResourceMetadata(env)).toMatchObject({
      resource,
      authorization_servers: [authorizationServer],
      scopes_supported: expect.arrayContaining([
        "memories.read",
        "conversations.read",
      ]),
      bearer_methods_supported: ["header"],
    });
  });

  it("returns an OAuth discovery challenge before calling internal services", async () => {
    const apiCore = vi.fn(() => Response.json({ error: "unexpected" }));
    const auth = vi.fn(() => Response.json({ error: "unexpected" }));
    const env = environment({ apiCore, auth });
    const response = await handleMcpTransport(
      new Request(resource, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
      }),
      env,
    );
    expect(response.status).toBe(401);
    expect(response.headers.get("www-authenticate")).toContain(
      'resource_metadata="https://edge.test/.well-known/oauth-protected-resource/v1/mcp/sse"',
    );
    expect(apiCore).not.toHaveBeenCalled();
    expect(auth).not.toHaveBeenCalled();
  });

  it("authenticates an API key once and advertises only its scoped tools", async () => {
    const apiCore = vi.fn(async (incoming: Request) => {
      expect(new URL(incoming.url).pathname).toBe("/internal/mcp/principal");
      expect(incoming.headers.get("authorization")).toBe(apiKey);
      expect(incoming.headers.get("x-internal-assertion-secret")).toBe(
        "mcp-internal-secret",
      );
      return Response.json({
        uid: "mcp-user",
        scopes: ["memories.read"],
        auth_type: "api_key",
        client_id: null,
      });
    });
    const env = environment({ apiCore });
    const response = await handleMcpTransport(request("tools/list"), env);
    expect(response.status).toBe(200);
    const payload = await jsonRpc(response);
    const result = payload.result as { tools: Array<{ name: string }> };
    expect(result.tools.map((tool) => tool.name).sort()).toEqual([
      "get_memories",
      "get_user_profile",
      "get_x_posts",
      "search_memories",
      "search_x_posts",
    ]);
    expect(apiCore).toHaveBeenCalledTimes(1);
  });

  it("converts a scoped tool call to the API Core REST contract", async () => {
    const apiCore = vi.fn(async (incoming: Request) => {
      const url = new URL(incoming.url);
      if (url.pathname === "/internal/mcp/principal") {
        return Response.json({
          uid: "mcp-user",
          scopes: ["memories.read"],
          auth_type: "api_key",
          client_id: null,
        });
      }
      expect(url.pathname).toBe("/v1/mcp/memories");
      expect(url.searchParams.get("limit")).toBe("2");
      expect(incoming.headers.get("authorization")).toBe(apiKey);
      return Response.json([{ id: "memory-1", content: "Likes tea" }]);
    });
    const env = environment({ apiCore });
    const response = await handleMcpTransport(
      request("tools/call", {
        name: "get_memories",
        arguments: { limit: 2 },
      }),
      env,
    );
    expect(response.status).toBe(200);
    const payload = await jsonRpc(response);
    const result = payload.result as {
      content: Array<{ type: string; text: string }>;
    };
    expect(JSON.parse(result.content[0]?.text || "null")).toEqual([
      { id: "memory-1", content: "Likes tea" },
    ]);
  });

  it("replaces an OAuth token with request-bound contexts for API Core", async () => {
    const auth = vi.fn(async (incoming: Request) => {
      expect(new URL(incoming.url).pathname).toBe("/internal/mcp/verify");
      expect(incoming.headers.get("authorization")).toBe("Bearer oauth-token");
      return Response.json({
        uid: "oauth-user",
        scopes: ["goals.read"],
        clientId: "oauth-client",
      });
    });
    const apiCore = vi.fn(async (incoming: Request) => {
      const url = new URL(incoming.url);
      expect(incoming.headers.get("authorization")).toBeNull();
      const context = await verifyRequestAuthContext(
        incoming,
        "api-core",
        "mcp-internal-secret",
      );
      expect(context).toMatchObject({
        uid: "oauth-user",
        authority: "mcp-oauth",
        scopes: ["goals.read"],
        oauthClientId: "oauth-client",
      });
      if (url.pathname === "/internal/mcp/principal") {
        return Response.json({
          uid: "oauth-user",
          scopes: ["goals.read"],
          auth_type: "oauth",
          client_id: "oauth-client",
        });
      }
      expect(url.pathname).toBe("/v1/mcp/goals");
      return Response.json([{ id: "goal-1", title: "Ship Workers" }]);
    });
    const env = environment({ apiCore, auth });
    const response = await handleMcpTransport(
      request(
        "tools/call",
        { name: "get_goals", arguments: {} },
        "Bearer oauth-token",
      ),
      env,
    );
    expect(response.status).toBe(200);
    const payload = await jsonRpc(response);
    const result = payload.result as {
      content: Array<{ type: string; text: string }>;
    };
    expect(JSON.parse(result.content[0]?.text || "null")).toEqual([
      { id: "goal-1", title: "Ship Workers" },
    ]);
    expect(auth).toHaveBeenCalledOnce();
    expect(apiCore).toHaveBeenCalledTimes(2);
  });
});
