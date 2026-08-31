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
    MCP_APP_TOKEN_ENCRYPTION_SECRET:
      "mcp-oauth-test-secret-01234567890123456789",
  } as unknown as JobsEnv;
  return { database, env };
}

function testApp(
  env: JobsEnv,
  dependencies: McpAppOauthDependencies,
  authenticated = true,
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
  registerMcpAppOauthRoutes(app, requestContext as never, dependencies);
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
    expect(rows.map((row) => row.status)).toEqual(["expired", "exchanged"]);
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

  it("requires the Better Auth-derived request context for start", async () => {
    const { env } = environment();
    const app = testApp(env, {}, false);
    const response = await app.request("/v2/cf/apps/mcp/authorize", {
      method: "POST",
      body: JSON.stringify({ app_id: "mcp-app" }),
    });
    expect(response.status).toBe(401);
  });
});
