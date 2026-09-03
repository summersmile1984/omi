import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SignedAuthContext } from "../workers/shared/auth-context";
import type { JobsEnv } from "../workers/jobs/env";
import {
  registerMcpAppOauthRoutes,
  type McpAppOauthDependencies,
} from "../workers/jobs/mcp-app-oauth-staging";

type SqlStatement = {
  sql: string;
  args: unknown[];
  execute(): { success: true; results: unknown[]; meta: { changes: number } };
};

function sqliteValue(value: unknown) {
  return value as never;
}

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        const statement = this.database.prepare(sql);
        if (/RETURNING\b/is.test(sql)) {
          return {
            success: true as const,
            results: statement.all(...args.map(sqliteValue)),
            meta: { changes: 0 },
          };
        }
        const result = statement.run(...args.map(sqliteValue));
        return {
          success: true as const,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  async batch(statements: SqlStatement[]) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((statement) => statement.execute());
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];

function environment(enabled = true) {
  const database = new SqliteD1();
  databases.push(database);
  database.database
    .prepare(
      `INSERT INTO cf_app_catalog
         (id, approved, status, disabled, is_popular, installs, rating_count,
          data_json, updated_at, owner_uid)
       VALUES (?, 1, 'approved', 0, 0, 0, 0, ?, ?, ?)`,
    )
    .run(
      "mcp-app",
      JSON.stringify({ id: "mcp-app", name: "Demo MCP" }),
      1_788_000_000,
      "owner-1",
    );
  const env = {
    APP_DB: database as unknown as D1Database,
    PUBLIC_API_BASE_URL: "https://edge.example.test",
    MCP_APP_OAUTH_STAGING_ENABLED: enabled ? "true" : "false",
    MCP_APP_LEGACY_EXACT_STAGING_ENABLED: enabled ? "true" : "false",
    MCP_APP_TOKEN_ENCRYPTION_SECRET:
      "mcp-oauth-test-secret-01234567890123456789",
  } as unknown as JobsEnv;
  return { database, env };
}

function testApp(
  env: JobsEnv,
  dependencies: McpAppOauthDependencies,
  authenticated = true,
  surface: "namespaced" | "legacy" = "namespaced",
) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  const requestContext = async (c: {
    req: { header(name: string): string | undefined };
  }): Promise<SignedAuthContext | null> =>
    c.req.header("authorization") === "Bearer owner-session"
      ? ({
          uid: "owner-1",
          authority: "better-auth",
          requestId: "oauth-test",
        } as SignedAuthContext)
      : null;
  registerMcpAppOauthRoutes(
    app,
    requestContext as never,
    dependencies,
    surface,
  );
  return {
    request: (pathValue: string, init: RequestInit = {}) => {
      const headers = new Headers(init.headers);
      if (authenticated) headers.set("authorization", "Bearer owner-session");
      return app.request(
        `https://jobs.test${pathValue}`,
        { ...init, headers },
        env,
      );
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  while (databases.length) databases.pop()?.close();
});

describe("namespaced MCP app OAuth staging seam", () => {
  it("gates the seam without changing the legacy owner", async () => {
    const { env } = environment(false);
    const app = testApp(env, {});
    const response = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(response.status).toBe(404);
    await expect(response.json()).resolves.toEqual({ error: "not_found" });
  });

  it("registers a provider, uses S256 PKCE, exchanges a one-time callback, and encrypts credentials", async () => {
    const { env, database } = environment();
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        calls.push({ url, init });
        if (url === "https://provider.example.test/register") {
          return Response.json({
            client_id: "registered-client",
            client_secret: "registered-secret",
          });
        }
        if (url === "https://provider.example.test/token") {
          return Response.json({
            access_token: "access-token-secret",
            refresh_token: "refresh-token-secret",
            expires_in: 3_600,
          });
        }
        throw new Error(`unexpected provider URL ${url}`);
      },
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        registration_endpoint: "https://provider.example.test/register",
        scopes: ["tools", "profile"],
      }),
    });
    expect(start.status).toBe(200);
    const startPayload = (await start.json()) as { auth_url: string };
    const authUrl = new URL(startPayload.auth_url);
    expect(authUrl.origin).toBe("https://provider.example.test");
    expect(authUrl.searchParams.get("code_challenge_method")).toBe("S256");
    expect(authUrl.searchParams.get("code_challenge")).toMatch(
      /^[A-Za-z0-9_-]+$/,
    );
    expect(authUrl.searchParams.get("redirect_uri")).toBe(
      "https://edge.example.test/v2/cf/apps/mcp/callback",
    );
    expect(calls).toHaveLength(1);
    expect(calls[0].init?.redirect).toBe("error");

    const callback = await app.request(
      `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
      { method: "GET" },
    );
    expect(callback.status).toBe(200);
    expect(await callback.text()).toContain("Authorization complete");
    expect(calls).toHaveLength(2);
    const tokenBody = String(calls[1].init?.body);
    expect(tokenBody).toContain("code_verifier=");
    expect(tokenBody).toContain("client_secret=registered-secret");

    const connection = database.database
      .prepare(
        "SELECT status, credential_envelope_enc FROM cf_mcp_app_connections WHERE app_id = ?",
      )
      .get("mcp-app") as { status: string; credential_envelope_enc: string };
    expect(connection.status).toBe("authorized");
    expect(connection.credential_envelope_enc).toMatch(/^v1\./);
    expect(connection.credential_envelope_enc).not.toContain(
      "access-token-secret",
    );
    expect(connection.credential_envelope_enc).not.toContain(
      "refresh-token-secret",
    );
    const transaction = database.database
      .prepare("SELECT status, attempts FROM cf_mcp_app_oauth_transactions")
      .get() as { status: string; attempts: number };
    expect(transaction).toEqual({ status: "exchanged", attempts: 1 });

    const replay = await app.request(
      `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
      { method: "GET" },
    );
    expect(replay.status).toBe(400);
    expect(calls).toHaveLength(2);
  });

  it("keeps app ownership and endpoint validation fail-closed", async () => {
    const { env, database } = environment();
    const app = testApp(env, {});
    database.database
      .prepare(
        `INSERT INTO cf_mcp_app_connections
           (app_id, owner_uid, server_url, status, oauth_metadata_json, created_at, updated_at)
         VALUES (?, ?, ?, 'pending', '{}', ?, ?)`,
      )
      .run(
        "mcp-app",
        "owner-2",
        "https://provider.example.test/mcp",
        1_788_000_000,
        1_788_000_000,
      );
    const ownerMismatch = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    expect(ownerMismatch.status).toBe(409);
    await expect(ownerMismatch.json()).resolves.toEqual({
      error: "app_connection_owner_mismatch",
    });
    const crossOwner = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "missing",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    expect(crossOwner.status).toBe(404);
    for (const serverUrl of [
      "http://127.0.0.1/mcp",
      "https://169.254.1.1/mcp",
      "https://100.64.0.1/mcp",
      "https://[::1]/mcp",
      "https://[::ffff:7f00:1]/mcp",
    ]) {
      const unsafe = await app.request("/v2/cf/apps/mcp/authorize", {
        method: "POST",
        body: JSON.stringify({
          app_id: "mcp-app",
          server_url: serverUrl,
          authorization_endpoint: "https://provider.example.test/authorize",
          token_endpoint: "https://provider.example.test/token",
          client_id: "client",
        }),
      });
      expect(unsafe.status).toBe(422);
      await expect(unsafe.json()).resolves.toEqual({
        error: "invalid_provider_metadata",
      });
    }
    const invalidScopes = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
        scopes: ["tools", 7],
      }),
    });
    expect(invalidScopes.status).toBe(422);
    await expect(invalidScopes.json()).resolves.toEqual({
      error: "invalid_scope",
    });
  });

  it("expires an older pending transaction so a slow callback cannot win", async () => {
    const { env, database } = environment();
    const fetchImpl = vi.fn(async () =>
      Response.json({ access_token: "access-token", expires_in: 3_600 }),
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const body = JSON.stringify({
      app_id: "mcp-app",
      server_url: "https://provider.example.test/mcp",
      authorization_endpoint: "https://provider.example.test/authorize",
      token_endpoint: "https://provider.example.test/token",
      client_id: "client",
    });
    const first = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body,
    });
    const firstUrl = new URL(
      ((await first.json()) as { auth_url: string }).auth_url,
    );
    const second = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body,
    });
    const secondUrl = new URL(
      ((await second.json()) as { auth_url: string }).auth_url,
    );
    const stale = await app.request(
      `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(firstUrl.searchParams.get("state") || "")}`,
    );
    expect(stale.status).toBe(400);
    expect(fetchImpl).not.toHaveBeenCalled();
    const current = await app.request(
      `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(secondUrl.searchParams.get("state") || "")}`,
    );
    expect(current.status).toBe(200);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const rows = database.database
      .prepare(
        "SELECT status FROM cf_mcp_app_oauth_transactions ORDER BY created_at, transaction_id",
      )
      .all() as Array<{ status: string }>;
    expect(rows.map((row) => row.status)).toEqual(
      expect.arrayContaining(["expired", "exchanged"]),
    );
    expect(rows).toHaveLength(2);
  });

  it("marks a consumed transaction failed when the token response is invalid", async () => {
    const { env, database } = environment();
    const fetchImpl = vi.fn(async () => Response.json({ not_a_token: true }));
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    const callback = await app.request(
      `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
    );
    expect(callback.status).toBe(502);
    const transaction = database.database
      .prepare("SELECT status, last_error FROM cf_mcp_app_oauth_transactions")
      .get() as { status: string; last_error: string };
    expect(transaction.status).toBe("failed");
    expect(transaction.last_error).toBe("token_response_invalid");
  });

  it("discovers tools through bounded MCP initialize/tools-list and writes the D1 projection", async () => {
    const { env, database } = environment();
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        calls.push({ url, init });
        if (url.endsWith("/token")) {
          return Response.json({
            access_token: "access-token",
            expires_in: 3_600,
          });
        }
        if (url.endsWith("/mcp")) {
          const payload = JSON.parse(String(init?.body)) as { method?: string };
          if (payload.method === "initialize") {
            return Response.json(
              {
                jsonrpc: "2.0",
                id: 1,
                result: {
                  protocolVersion: "2025-03-26",
                  capabilities: { tools: {} },
                  serverInfo: { name: "fixture", version: "1" },
                },
              },
              { headers: { "Mcp-Session-Id": "session-1" } },
            );
          }
          if (payload.method === "notifications/initialized")
            return new Response(null, { status: 202 });
          if (payload.method === "tools/list") {
            return Response.json({
              jsonrpc: "2.0",
              id: 2,
              result: {
                tools: [
                  {
                    name: "fixture_search",
                    description: "Search the fixture MCP server.",
                    inputSchema: {
                      type: "object",
                      properties: { query: { type: "string" } },
                    },
                  },
                ],
              },
            });
          }
        }
        throw new Error(`unexpected provider URL ${url}`);
      },
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    const callback = await app.request(
      `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
    );
    expect(callback.status).toBe(200);
    const discovery = await app.request("/v2/cf/apps/mcp/discover", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(discovery.status).toBe(200);
    await expect(discovery.json()).resolves.toEqual({
      app_id: "mcp-app",
      status: "ready",
      endpoint: "https://provider.example.test/mcp",
      transport: "streamable_http",
      protocol_version: "2025-03-26",
      revision: 0,
      tools_count: 1,
      tool_names: ["fixture_search"],
    });
    expect(calls).toHaveLength(4);
    expect(calls[2].init?.headers).toMatchObject({
      "mcp-session-id": "session-1",
    });
    expect(calls[3].init?.headers).toMatchObject({
      "mcp-session-id": "session-1",
    });
    const projection = database.database
      .prepare(
        "SELECT status, protocol_version, tools_json, revision FROM cf_mcp_app_discoveries WHERE app_id = ?",
      )
      .get("mcp-app") as {
      status: string;
      protocol_version: string;
      tools_json: string;
      revision: number;
    };
    expect(projection.status).toBe("ready");
    expect(projection.protocol_version).toBe("2025-03-26");
    expect(JSON.parse(projection.tools_json)).toHaveLength(1);
    expect(projection.revision).toBe(0);

    const install = await app.request("/v2/cf/apps/mcp/install", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(install.status).toBe(200);
    await expect(install.json()).resolves.toEqual({
      app_id: "mcp-app",
      status: "installed",
      discovery_revision: 0,
      tools_count: 1,
    });
    const repeatInstall = await app.request("/v2/cf/apps/mcp/install", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(repeatInstall.status).toBe(200);
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_user_enabled_apps WHERE uid = ? AND app_id = ?")
        .get("owner-1", "mcp-app"),
    ).toEqual({ count: 1 });
  });

  it("tries endpoint candidates and follows bounded tools/list cursor pagination", async () => {
    const { env } = environment();
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        calls.push({ url, init });
        if (url.endsWith("/token"))
          return Response.json({
            access_token: "access-token",
            expires_in: 3_600,
          });
        if (url === "https://primary.example.test/mcp")
          return new Response("not found", { status: 404 });
        if (url === "https://backup.example.test/mcp") {
          const payload = JSON.parse(String(init?.body)) as {
            id?: number;
            method?: string;
            params?: { cursor?: string };
          };
          if (payload.method === "initialize")
            return Response.json({
              jsonrpc: "2.0",
              id: payload.id,
              result: { protocolVersion: "2025-03-26", capabilities: {} },
            });
          if (payload.method === "notifications/initialized")
            return new Response(null, { status: 202 });
          if (payload.method === "tools/list" && !payload.params?.cursor)
            return Response.json({
              jsonrpc: "2.0",
              id: payload.id,
              result: { tools: [{ name: "first_tool" }], nextCursor: "page-2" },
            });
          if (
            payload.method === "tools/list" &&
            payload.params?.cursor === "page-2"
          )
            return Response.json({
              jsonrpc: "2.0",
              id: payload.id,
              result: { tools: [{ name: "second_tool" }] },
            });
        }
        throw new Error(`unexpected provider URL ${url}`);
      },
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://primary.example.test/mcp",
        endpoint_candidates: ["https://backup.example.test/mcp"],
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    await expect(
      app.request(
        `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
      ),
    ).resolves.toMatchObject({ status: 200 });
    const discovery = await app.request("/v2/cf/apps/mcp/discover", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(discovery.status).toBe(200);
    await expect(discovery.json()).resolves.toMatchObject({
      endpoint: "https://backup.example.test/mcp",
      tools_count: 2,
      tool_names: ["first_tool", "second_tool"],
      transport: "streamable_http",
    });
    expect(calls.map(({ url }) => url)).toEqual([
      "https://provider.example.test/token",
      "https://primary.example.test/mcp",
      "https://primary.example.test/mcp",
      "https://backup.example.test/mcp",
      "https://backup.example.test/mcp",
      "https://backup.example.test/mcp",
      "https://backup.example.test/mcp",
    ]);
    const secondPage = JSON.parse(String(calls[6].init?.body)) as {
      id: number;
      params: { cursor: string };
    };
    expect(secondPage).toMatchObject({ id: 3, params: { cursor: "page-2" } });
  });

  it("rejects a non-matching JSON-RPC response id", async () => {
    const { env } = environment();
    let calls = 0;
    const fetchImpl = vi.fn(async () => {
      calls += 1;
      if (calls === 1)
        return Response.json({
          access_token: "access-token",
          expires_in: 3_600,
        });
      return Response.json({
        jsonrpc: "2.0",
        id: 99,
        result: { protocolVersion: "2025-03-26", capabilities: {} },
      });
    });
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    await expect(
      app.request(
        `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
      ),
    ).resolves.toMatchObject({ status: 200 });
    const discovery = await app.request("/v2/cf/apps/mcp/discover", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(discovery.status).toBe(502);
    await expect(discovery.json()).resolves.toEqual({
      error: "discovery_response_invalid",
    });
  });

  it("falls back to the bounded legacy SSE transport after streamable HTTP is unavailable", async () => {
    const { env } = environment();
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const sseBody = [
      "event: endpoint",
      "data: /messages?session=sse-1",
      "",
      "event: message",
      'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{}}}',
      "",
      "event: message",
      'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"sse_tool"}]}}',
      "",
    ].join("\n");
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        calls.push({ url, init });
        if (url.endsWith("/token"))
          return Response.json({
            access_token: "access-token",
            expires_in: 3_600,
          });
        if (url === "https://sse.example.test/mcp" && init?.method === "POST")
          return new Response("method not allowed", { status: 405 });
        if (url === "https://sse.example.test/mcp" && init?.method === "GET")
          return new Response(sseBody, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        if (url === "https://sse.example.test/messages?session=sse-1")
          return new Response(null, { status: 202 });
        throw new Error(`unexpected provider URL ${url}`);
      },
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://sse.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    await expect(
      app.request(
        `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
      ),
    ).resolves.toMatchObject({ status: 200 });
    const discovery = await app.request("/v2/cf/apps/mcp/discover", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(discovery.status).toBe(200);
    await expect(discovery.json()).resolves.toMatchObject({
      endpoint: "https://sse.example.test/mcp",
      transport: "sse",
      protocol_version: "2024-11-05",
      tools_count: 1,
      tool_names: ["sse_tool"],
    });
    expect(calls.map(({ url }) => url)).toEqual([
      "https://provider.example.test/token",
      "https://sse.example.test/mcp",
      "https://sse.example.test/mcp",
      "https://sse.example.test/messages?session=sse-1",
      "https://sse.example.test/messages?session=sse-1",
      "https://sse.example.test/messages?session=sse-1",
    ]);
  });

  it("marks an authorized connection for reauthorization when the MCP server returns 401", async () => {
    const { env, database } = environment();
    let callCount = 0;
    const fetchImpl = vi.fn(async () => {
      callCount += 1;
      if (callCount === 1)
        return Response.json({
          access_token: "access-token",
          expires_in: 3_600,
        });
      return new Response("unauthorized", { status: 401 });
    });
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    await expect(
      app.request(
        `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
      ),
    ).resolves.toMatchObject({ status: 200 });
    const discovery = await app.request("/v2/cf/apps/mcp/discover", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(discovery.status).toBe(401);
    await expect(discovery.json()).resolves.toEqual({
      error: "mcp_reauthorization_required",
    });
    const connection = database.database
      .prepare(
        "SELECT status, last_error FROM cf_mcp_app_connections WHERE app_id = ?",
      )
      .get("mcp-app") as { status: string; last_error: string };
    expect(connection).toEqual({
      status: "reauthorize",
      last_error: "mcp_reauthorization_required",
    });
  });

  it("refreshes an encrypted credential and immediately re-discovers tools", async () => {
    const { env, database } = environment();
    const refreshBodies: string[] = [];
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        if (url.endsWith("/token")) {
          const body = String(init?.body);
          if (body.includes("grant_type=refresh_token")) {
            refreshBodies.push(body);
            return Response.json({
              access_token: "refreshed-access-token",
              refresh_token: "refreshed-refresh-token",
              expires_in: 7_200,
            });
          }
          return Response.json({
            access_token: "initial-access-token",
            refresh_token: "initial-refresh-token",
            expires_in: 3_600,
          });
        }
        if (url.endsWith("/mcp")) {
          const payload = JSON.parse(String(init?.body)) as { method?: string };
          if (payload.method === "initialize") {
            return Response.json(
              {
                jsonrpc: "2.0",
                id: 1,
                result: {
                  protocolVersion: "2025-03-26",
                  capabilities: { tools: {} },
                  serverInfo: { name: "refresh-fixture", version: "1" },
                },
              },
              { headers: { "Mcp-Session-Id": "refresh-session" } },
            );
          }
          if (payload.method === "notifications/initialized")
            return new Response(null, { status: 202 });
          if (payload.method === "tools/list")
            return Response.json({
              jsonrpc: "2.0",
              id: 2,
              result: {
                tools: [
                  {
                    name: "refresh_search",
                    inputSchema: { type: "object" },
                  },
                ],
              },
            });
        }
        throw new Error(`unexpected provider URL ${url}`);
      },
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    const callback = await app.request(
      `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
    );
    expect(callback.status).toBe(200);

    const refresh = await app.request("/v2/cf/apps/mcp/refresh", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(refresh.status).toBe(200);
    await expect(refresh.json()).resolves.toEqual({
      app_id: "mcp-app",
      status: "ready",
      endpoint: "https://provider.example.test/mcp",
      transport: "streamable_http",
      protocol_version: "2025-03-26",
      revision: 0,
      tools_count: 1,
      tool_names: ["refresh_search"],
    });
    expect(refreshBodies).toHaveLength(1);
    expect(refreshBodies[0]).toContain("grant_type=refresh_token");
    expect(refreshBodies[0]).toContain("refresh_token=initial-refresh-token");
    const connection = database.database
      .prepare(
        "SELECT status, revision, credential_envelope_enc FROM cf_mcp_app_connections WHERE app_id = ?",
      )
      .get("mcp-app") as {
      status: string;
      revision: number;
      credential_envelope_enc: string;
    };
    expect(connection.status).toBe("authorized");
    expect(connection.revision).toBe(3);
    expect(connection.credential_envelope_enc).toMatch(/^v1\./);
    expect(connection.credential_envelope_enc).not.toContain(
      "refreshed-access-token",
    );
    expect(connection.credential_envelope_enc).not.toContain(
      "refreshed-refresh-token",
    );
  });

  it("clears credentials and requires reauthorization when refresh returns 401", async () => {
    const { env, database } = environment();
    let tokenCalls = 0;
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        if (!url.endsWith("/token"))
          throw new Error(`unexpected provider URL ${url}`);
        tokenCalls += 1;
        if (String(init?.body).includes("grant_type=refresh_token"))
          return new Response("expired", { status: 401 });
        return Response.json({
          access_token: "initial-access-token",
          refresh_token: "initial-refresh-token",
          expires_in: 3_600,
        });
      },
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    await expect(
      app.request(
        `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
      ),
    ).resolves.toMatchObject({ status: 200 });

    const refresh = await app.request("/v2/cf/apps/mcp/refresh", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(refresh.status).toBe(401);
    await expect(refresh.json()).resolves.toEqual({
      error: "mcp_reauthorization_required",
    });
    const connection = database.database
      .prepare(
        "SELECT status, revision, credential_envelope_enc FROM cf_mcp_app_connections WHERE app_id = ?",
      )
      .get("mcp-app") as {
      status: string;
      revision: number;
      credential_envelope_enc: string | null;
    };
    expect(connection).toEqual({
      status: "reauthorize",
      revision: 2,
      credential_envelope_enc: null,
    });

    const replay = await app.request("/v2/cf/apps/mcp/refresh", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(replay.status).toBe(401);
    await expect(replay.json()).resolves.toEqual({
      error: "mcp_reauthorization_required",
    });
    expect(tokenCalls).toBe(2);
  });

  it("uses connection revision CAS so concurrent refreshes cannot both commit", async () => {
    const { env, database } = environment();
    let refreshCalls = 0;
    let releaseRefresh!: () => void;
    const refreshBarrier = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        if (url.endsWith("/token")) {
          const body = String(init?.body);
          if (body.includes("grant_type=refresh_token")) {
            refreshCalls += 1;
            if (refreshCalls === 2) releaseRefresh();
            await refreshBarrier;
            return Response.json({
              access_token: `refreshed-access-${refreshCalls}`,
              refresh_token: `refreshed-refresh-${refreshCalls}`,
              expires_in: 3_600,
            });
          }
          return Response.json({
            access_token: "initial-access-token",
            refresh_token: "initial-refresh-token",
            expires_in: 3_600,
          });
        }
        if (url.endsWith("/mcp")) {
          const payload = JSON.parse(String(init?.body)) as { method?: string };
          if (payload.method === "initialize")
            return Response.json(
              {
                jsonrpc: "2.0",
                id: 1,
                result: {
                  protocolVersion: "2025-03-26",
                  capabilities: { tools: {} },
                  serverInfo: { name: "cas-fixture", version: "1" },
                },
              },
              { headers: { "Mcp-Session-Id": "cas-session" } },
            );
          if (payload.method === "notifications/initialized")
            return new Response(null, { status: 202 });
          if (payload.method === "tools/list")
            return Response.json({
              jsonrpc: "2.0",
              id: 2,
              result: { tools: [{ name: "cas_tool" }] },
            });
        }
        throw new Error(`unexpected provider URL ${url}`);
      },
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(
      ((await start.json()) as { auth_url: string }).auth_url,
    );
    await expect(
      app.request(
        `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
      ),
    ).resolves.toMatchObject({ status: 200 });

    const refreshRequest = () =>
      app.request("/v2/cf/apps/mcp/refresh", {
        method: "POST",
        body: JSON.stringify({ app_id: "mcp-app" }),
      });
    const [first, second] = await Promise.all([
      refreshRequest(),
      refreshRequest(),
    ]);
    expect([first.status, second.status].sort((a, b) => a - b)).toEqual([
      200, 409,
    ]);
    expect(refreshCalls).toBe(2);
    const connection = database.database
      .prepare(
        "SELECT status, revision, credential_envelope_enc FROM cf_mcp_app_connections WHERE app_id = ?",
      )
      .get("mcp-app") as {
      status: string;
      revision: number;
      credential_envelope_enc: string;
    };
    expect(connection.status).toBe("authorized");
    expect(connection.revision).toBe(3);
    expect(connection.credential_envelope_enc).toMatch(/^v1\./);
  });

  it("requires the Better Auth-derived request context for start", async () => {
    const { env } = environment();
    const app = testApp(env, {}, false);
    const response = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(response.status).toBe(401);
  });

  it("executes an installed ready tool through bounded streamable MCP call", async () => {
    const { env, database } = environment();
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    let rejectCall = false;
    const fetchImpl = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        calls.push({ url, init });
        if (url.endsWith("/token"))
          return Response.json({ access_token: "call-access-token", expires_in: 3_600 });
        if (url.endsWith("/mcp")) {
          const payload = JSON.parse(String(init?.body)) as {
            id?: number;
            method?: string;
            params?: { name?: string; arguments?: Record<string, unknown> };
          };
          expect(init?.headers).toMatchObject({ authorization: "Bearer call-access-token" });
          if (payload.method === "initialize")
            return Response.json(
              {
                jsonrpc: "2.0",
                id: payload.id,
                result: { protocolVersion: "2025-03-26", capabilities: {}, serverInfo: { name: "fixture" } },
              },
              { headers: { "Mcp-Session-Id": "call-session" } },
            );
          if (payload.method === "notifications/initialized") return new Response(null, { status: 202 });
          if (payload.method === "tools/list")
            return Response.json({
              jsonrpc: "2.0",
              id: payload.id,
              result: {
                tools: [{ name: "fixture_search", inputSchema: { type: "object" } }],
              },
            });
          if (payload.method === "tools/call") {
            expect(payload.params).toEqual({ name: "fixture_search", arguments: { query: "cloudflare" } });
            expect(init?.headers).toMatchObject({ "mcp-session-id": "call-session" });
            if (rejectCall) return new Response(null, { status: 401 });
            return Response.json({
              jsonrpc: "2.0",
              id: payload.id,
              result: { content: [{ type: "text", text: "ok" }], isError: false },
            });
          }
        }
        throw new Error(`unexpected provider request ${url}`);
      },
    );
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 });
    const start = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({
        app_id: "mcp-app",
        server_url: "https://provider.example.test/mcp",
        authorization_endpoint: "https://provider.example.test/authorize",
        token_endpoint: "https://provider.example.test/token",
        client_id: "client",
      }),
    });
    const authUrl = new URL(((await start.json()) as { auth_url: string }).auth_url);
    const callback = await app.request(
      `/v2/cf/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
    );
    expect(callback.status).toBe(200);
    const discovery = await app.request("/v2/cf/apps/mcp/discover", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(discovery.status).toBe(200);
    const install = await app.request("/v2/cf/apps/mcp/install", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(install.status).toBe(200);

    const response = await app.request("/v2/cf/apps/mcp/tools/mcp-app/call", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "fixture_search", arguments: { query: "cloudflare" } }),
    });
    expect(response.status).toBe(200);
    const responsePayload = await response.json();
    expect(responsePayload).toEqual({
      app_id: "mcp-app",
      tool_name: "fixture_search",
      result: { content: [{ type: "text", text: "ok" }], isError: false },
    });
    expect(JSON.stringify(responsePayload)).not.toContain("call-access-token");
    expect(calls).toHaveLength(7);
    expect(
      database.database.prepare("SELECT status FROM cf_mcp_app_connections WHERE app_id = ?").get("mcp-app"),
    ).toEqual({ status: "authorized" });

    rejectCall = true;
    const expired = await app.request("/v2/cf/apps/mcp/tools/mcp-app/call", {
      method: "POST",
      body: JSON.stringify({ name: "fixture_search", arguments: { query: "cloudflare" } }),
    });
    expect(expired.status).toBe(401);
    await expect(expired.json()).resolves.toEqual({ error: "mcp_reauthorization_required" });
    expect(
      database.database.prepare("SELECT status, credential_envelope_enc FROM cf_mcp_app_connections WHERE app_id = ?").get("mcp-app"),
    ).toEqual({ status: "reauthorize", credential_envelope_enc: null });
  });

  it("rejects malformed tool calls before provider I/O", async () => {
    const { env } = environment();
    const fetchImpl = vi.fn(async () => Response.json({ access_token: "unused", expires_in: 3_600 }));
    const app = testApp(env, { fetchImpl });
    const response = await app.request("/v2/cf/apps/mcp/tools/mcp-app/call", {
      method: "POST",
      body: JSON.stringify({ name: "bad\u0000name", arguments: [] }),
    });
    expect(response.status).toBe(422);
    await expect(response.json()).resolves.toEqual({ error: "invalid_tool_name" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("serves the exact legacy MCP app flow with callback auto-discovery and install", async () => {
    const { env, database } = environment();
    const calls: string[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      calls.push(`${init?.method || "GET"} ${url}`);
      if (url === "https://provider.example.test/.well-known/oauth-authorization-server") {
        return Response.json({
          authorization_endpoint: "https://provider.example.test/authorize",
          token_endpoint: "https://provider.example.test/token",
          registration_endpoint: "https://provider.example.test/register",
          scopes_supported: ["tools"],
        });
      }
      if (url === "https://provider.example.test/register") {
        return Response.json({ client_id: "legacy-client", client_secret: "legacy-secret" });
      }
      if (url === "https://provider.example.test/token") {
        return Response.json({
          access_token: "legacy-access",
          refresh_token: "legacy-refresh",
          expires_in: 3_600,
        });
      }
      if (url === "https://provider.example.test/mcp") {
        const payload = JSON.parse(String(init?.body || "{}")) as { method?: string; id?: number };
        if (payload.method === "initialize") {
          return Response.json({ jsonrpc: "2.0", id: payload.id, result: { protocolVersion: "2025-03-26" } });
        }
        if (payload.method === "notifications/initialized") return new Response(null, { status: 202 });
        if (payload.method === "tools/list") {
          return Response.json({
            jsonrpc: "2.0",
            id: payload.id,
            result: { tools: [{ name: "legacy_search", description: "Search" }] },
          });
        }
      }
      throw new Error(`unexpected provider request ${url}`);
    });
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 }, true, "legacy");
    const start = await app.request("/v1/apps/mcp", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        name: "Legacy MCP",
        mcp_server_url: "https://provider.example.test/mcp",
        description: "legacy app",
      }),
    });
    expect(start.status).toBe(200);
    const startPayload = (await start.json()) as { app_id: string; auth_url: string; requires_oauth: boolean };
    expect(startPayload.app_id).toMatch(/^mcp-[A-Za-z0-9_-]+$/);
    expect(startPayload.requires_oauth).toBe(true);
    const authUrl = new URL(startPayload.auth_url);
    expect(authUrl.searchParams.get("redirect_uri")).toBe(
      "https://edge.example.test/v1/apps/mcp/callback",
    );
    const callback = await app.request(
      `/v1/apps/mcp/callback?code=provider-code&state=${encodeURIComponent(authUrl.searchParams.get("state") || "")}`,
    );
    expect(callback.status).toBe(200);
    expect(await callback.text()).toContain("connected and its tools are ready");
    expect(
      database.database
        .prepare("SELECT status FROM cf_mcp_app_connections WHERE app_id = ?")
        .get(startPayload.app_id),
    ).toEqual({ status: "authorized" });
    expect(
      database.database
        .prepare("SELECT status FROM cf_mcp_app_discoveries WHERE app_id = ?")
        .get(startPayload.app_id),
    ).toEqual({ status: "ready" });
    expect(
      database.database
        .prepare("SELECT uid, app_id FROM cf_user_enabled_apps WHERE app_id = ?")
        .get(startPayload.app_id),
    ).toEqual({ uid: "owner-1", app_id: startPayload.app_id });
    expect(calls).toEqual([
      "GET https://provider.example.test/.well-known/oauth-authorization-server",
      "POST https://provider.example.test/register",
      "POST https://provider.example.test/token",
      "POST https://provider.example.test/mcp",
      "POST https://provider.example.test/mcp",
      "POST https://provider.example.test/mcp",
    ]);

    const refresh = await app.request(`/v1/apps/${startPayload.app_id}/mcp/refresh`, {
      method: "POST",
      headers: { "content-type": "application/json" },
    });
    expect(refresh.status).toBe(200);
    await expect(refresh.json()).resolves.toEqual({
      tools_count: 1,
      tool_names: ["legacy_search"],
    });
  });

  it("maps exact legacy input errors without forwarding malformed provider URLs", async () => {
    const { env } = environment();
    const fetchImpl = vi.fn(async () => Response.json({}));
    const app = testApp(env, { fetchImpl }, true, "legacy");
    const response = await app.request("/v1/apps/mcp", {
      method: "POST",
      body: JSON.stringify({ name: "", mcp_server_url: "http://127.0.0.1/mcp" }),
    });
    expect(response.status).toBe(422);
    await expect(response.json()).resolves.toEqual({ error: "invalid_app_name" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("supports an exact legacy server without OAuth through the same bounded discovery adapter", async () => {
    const { env, database } = environment();
    const calls: Array<{ url: string; authorization: string | null }> = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      calls.push({ url, authorization: new Headers(init?.headers).get("authorization") });
      if (url === "https://public.example.test/.well-known/oauth-authorization-server") {
        return new Response("not found", { status: 404 });
      }
      if (url === "https://public.example.test/mcp") {
        const payload = JSON.parse(String(init?.body || "{}")) as { method?: string; id?: number };
        if (payload.method === "initialize") {
          return Response.json({ jsonrpc: "2.0", id: payload.id, result: { protocolVersion: "2025-03-26" } });
        }
        if (payload.method === "notifications/initialized") return new Response(null, { status: 202 });
        if (payload.method === "tools/list") {
          return Response.json({ jsonrpc: "2.0", id: payload.id, result: { tools: [{ name: "public_search" }] } });
        }
      }
      throw new Error(`unexpected provider request ${url}`);
    });
    const app = testApp(env, { fetchImpl, now: () => 1_788_000_100 }, true, "legacy");
    const response = await app.request("/v1/apps/mcp", {
      method: "POST",
      body: JSON.stringify({ name: "Public MCP", mcp_server_url: "https://public.example.test/mcp" }),
    });
    expect(response.status).toBe(200);
    const payload = (await response.json()) as { app_id: string; requires_oauth: boolean; tool_names: string[] };
    expect(payload.requires_oauth).toBe(false);
    expect(payload.tool_names).toEqual(["public_search"]);
    expect(calls).toHaveLength(4);
    expect(calls.slice(1).every((call) => call.authorization === null)).toBe(true);
    expect(
      database.database
        .prepare("SELECT uid, app_id FROM cf_user_enabled_apps WHERE app_id = ?")
        .get(payload.app_id),
    ).toEqual({ uid: "owner-1", app_id: payload.app_id });
  });

  it("fails closed before provider I/O when the exact owner envelope secret is absent", async () => {
    const { env } = environment();
    delete (env as unknown as Record<string, unknown>).MCP_APP_TOKEN_ENCRYPTION_SECRET;
    const fetchImpl = vi.fn(async () => Response.json({}));
    const app = testApp(env, { fetchImpl }, true, "legacy");
    const response = await app.request("/v1/apps/mcp", {
      method: "POST",
      body: JSON.stringify({ name: "MCP", mcp_server_url: "https://provider.example.test/mcp" }),
    });
    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toEqual({ error: "mcp_app_oauth_unavailable" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
