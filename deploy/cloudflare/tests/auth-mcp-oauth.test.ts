import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import auth from "../workers/auth/index";
import type { AuthEnv } from "../workers/auth/env";

function sqliteValue(value: unknown) {
  return value as never;
}

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/auth",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0, last_row_id: null },
      }),
      run: async () => {
        const result = this.database.prepare(sql).run(...args.map(sqliteValue));
        return {
          success: true as const,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  async exec(sql: string) {
    this.database.exec(sql);
    return { count: 1, duration: 0 };
  }

  batch(statements: Array<{ run(): Promise<unknown> }>) {
    return Promise.all(statements.map((statement) => statement.run()));
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];

function environment(): AuthEnv {
  const database = new SqliteD1();
  databases.push(database);
  return {
    AUTH_DB: database as unknown as D1Database,
    BETTER_AUTH_SECRET: "test-auth-secret-with-at-least-32-bytes",
    BETTER_AUTH_URL: "https://auth.test",
    MCP_RESOURCE_URL: "https://edge.test/v1/mcp/sse",
    ALLOWED_ORIGINS: "https://web.test",
    INTERNAL_ASSERTION_SECRET: "internal-secret",
  };
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("auth worker MCP OAuth provider", () => {
  it("publishes OAuth metadata and registers a PKCE public client for the MCP resource", async () => {
    const env = environment();
    const metadataResponse = await auth.fetch(
      new Request(
        "https://auth.test/api/better-auth/.well-known/oauth-authorization-server",
      ),
      env,
    );

    expect(metadataResponse.status).toBe(200);
    const metadata = (await metadataResponse.json()) as Record<string, unknown>;
    expect(metadata).toMatchObject({
      issuer: "https://auth.test/api/better-auth",
      authorization_endpoint:
        "https://auth.test/api/better-auth/oauth2/authorize",
      token_endpoint: "https://auth.test/api/better-auth/oauth2/token",
      registration_endpoint:
        "https://auth.test/api/better-auth/oauth2/register",
      code_challenge_methods_supported: ["S256"],
    });
    expect(metadata.scopes_supported).toEqual(
      expect.arrayContaining(["memories.read", "offline_access"]),
    );

    const registrationResponse = await auth.fetch(
      new Request("https://auth.test/api/better-auth/oauth2/register", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          client_name: "MCP test client",
          redirect_uris: ["https://client.example/callback"],
          token_endpoint_auth_method: "none",
          grant_types: ["authorization_code", "refresh_token"],
          response_types: ["code"],
          scope: "memories.read offline_access",
        }),
      }),
      env,
    );

    expect(registrationResponse.status).toBe(201);
    const registration = (await registrationResponse.json()) as Record<
      string,
      unknown
    >;
    expect(registration).toMatchObject({
      client_name: "MCP test client",
      token_endpoint_auth_method: "none",
      scope: expect.stringContaining("memories.read"),
    });
    expect(registration.client_id).toEqual(expect.any(String));

    const database = databases.at(-1)?.database;
    const resource = database
      ?.prepare(
        "SELECT identifier, allowedScopes FROM oauthResource WHERE identifier = ?",
      )
      .get("https://edge.test/v1/mcp/sse") as
      { identifier: string; allowedScopes: string | null } | undefined;
    expect(resource?.identifier).toBe("https://edge.test/v1/mcp/sse");

    const link = database
      ?.prepare("SELECT resourceId FROM oauthClientResource WHERE clientId = ?")
      .get(registration.client_id as string) as
      { resourceId: string } | undefined;
    expect(link?.resourceId).toBe("https://edge.test/v1/mcp/sse");
  });
});
