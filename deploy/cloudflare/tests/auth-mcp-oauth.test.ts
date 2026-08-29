import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SignJWT, exportJWK, generateKeyPair } from "jose";
import { afterEach, describe, expect, it, vi } from "vitest";
import auth from "../workers/auth/index";
import type { AuthEnv } from "../workers/auth/env";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

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
          | T
          | undefined) ?? null,
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

function environment(overrides: Partial<AuthEnv> = {}): AuthEnv {
  const database = new SqliteD1();
  databases.push(database);
  return {
    AUTH_DB: database as unknown as D1Database,
    BETTER_AUTH_SECRET: "test-auth-secret-with-at-least-32-bytes",
    BETTER_AUTH_URL: "https://auth.test",
    MCP_RESOURCE_URL: "https://edge.test/v1/mcp/sse",
    MCP_ALLOW_UNAUTHENTICATED_DCR: "true",
    ALLOWED_ORIGINS: "https://web.test",
    INTERNAL_ASSERTION_SECRET: "internal-secret",
    ...overrides,
  };
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
  vi.unstubAllGlobals();
});

async function accessToken(scope = "memories.read offline_access") {
  const { privateKey, publicKey } = await generateKeyPair("ES256");
  const publicJwk = await exportJWK(publicKey);
  Object.assign(publicJwk, { alg: "ES256", kid: "mcp-test-key", use: "sig" });
  const token = await new SignJWT({
    client_id: "mcp-test-client",
    azp: "mcp-test-client",
    scope,
  })
    .setProtectedHeader({ alg: "ES256", kid: "mcp-test-key" })
    .setIssuer("https://auth.test/api/better-auth")
    .setAudience("https://edge.test/v1/mcp/sse")
    .setSubject("mcp-user")
    .setIssuedAt()
    .setExpirationTime("5m")
    .sign(privateKey);
  return { publicJwk, token };
}

async function internalAuthRequest(
  url: string,
  method: "GET" | "DELETE",
  uid: string,
): Promise<Request> {
  const path = new URL(url).pathname;
  const signed = await createSignedAuthContext(
    {
      uid,
      authority: "better-auth",
      requestId: "mcp-grant-test",
    },
    "auth",
    method,
    path,
    "internal-secret",
  );
  if (!signed) throw new Error("failed to sign internal test request");
  return new Request(url, {
    method,
    headers: {
      [AUTH_CONTEXT_HEADER]: signed.encoded,
      [AUTH_SIGNATURE_HEADER]: signed.signature,
    },
  });
}

describe("auth worker MCP OAuth provider", () => {
  it("backfills existing Better Auth accounts onto issuer-scoped identities", () => {
    const database = new DatabaseSync(":memory:");
    try {
      const migrations = path.resolve(
        path.dirname(fileURLToPath(import.meta.url)),
        "../migrations/auth",
      );
      database.exec(
        readFileSync(path.join(migrations, "0001_better_auth.sql"), "utf8"),
      );
      database
        .prepare(
          `INSERT INTO user
             (id, name, email, emailVerified, createdAt, updatedAt)
           VALUES (?, ?, ?, ?, ?, ?)`,
        )
        .run("user-1", "User", "user@example.test", 1, 1, 1);
      const insert = database.prepare(
        `INSERT INTO account
           (id, accountId, providerId, userId, createdAt, updatedAt)
         VALUES (?, ?, ?, ?, ?, ?)`,
      );
      insert.run("credential-1", "user-1", "credential", "user-1", 1, 1);
      insert.run("google-1", "google-subject", "google", "user-1", 1, 1);
      insert.run("apple-1", "apple-subject", "apple", "user-1", 1, 1);

      database.exec(
        readFileSync(path.join(migrations, "0006_account_issuer.sql"), "utf8"),
      );

      expect(
        database
          .prepare("SELECT providerId, issuer FROM account ORDER BY providerId")
          .all(),
      ).toEqual([
        { providerId: "apple", issuer: "https://appleid.apple.com" },
        { providerId: "credential", issuer: "local:credential" },
        { providerId: "google", issuer: "https://accounts.google.com" },
      ]);
      expect(
        database
          .prepare("PRAGMA table_info(account)")
          .all()
          .find((column) => column.name === "issuer"),
      ).toMatchObject({ notnull: 1 });
      expect(() =>
        database
          .prepare(
            `INSERT INTO account
               (id, issuer, accountId, providerId, userId, createdAt, updatedAt)
             VALUES (?, ?, ?, ?, ?, ?, ?)`,
          )
          .run(
            "credential-2",
            "local:credential",
            "user-1",
            "credential",
            "user-1",
            1,
            1,
          ),
      ).toThrow();
    } finally {
      database.close();
    }
  });

  it("creates credential accounts with the Better Auth issuer identity key", async () => {
    const env = environment();
    const response = await auth.fetch(
      new Request("https://auth.test/api/better-auth/sign-up/email", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          origin: "https://web.test",
        },
        body: JSON.stringify({
          name: "MCP test user",
          email: "mcp-user@example.test",
          password: "Correct-Horse-Battery-Staple-1!",
        }),
      }),
      env,
    );

    expect(response.status).toBe(200);
    expect(
      databases
        .at(-1)
        ?.database.prepare(
          "SELECT issuer FROM account WHERE providerId = 'credential'",
        )
        .get(),
    ).toEqual({ issuer: "local:credential" });
  });

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
    expect(metadata.client_id_metadata_document_supported).toBeUndefined();
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
      | { identifier: string; allowedScopes: string | null }
      | undefined;
    expect(resource?.identifier).toBe("https://edge.test/v1/mcp/sse");

    const link = database
      ?.prepare("SELECT resourceId FROM oauthClientResource WHERE clientId = ?")
      .get(registration.client_id as string) as
      | { resourceId: string }
      | undefined;
    expect(link?.resourceId).toBe("https://edge.test/v1/mcp/sse");
  });

  it("keeps anonymous DCR closed unless the deployment explicitly enables it", async () => {
    const env = environment({ MCP_ALLOW_UNAUTHENTICATED_DCR: undefined });
    const metadataResponse = await auth.fetch(
      new Request(
        "https://auth.test/api/better-auth/.well-known/oauth-authorization-server",
      ),
      env,
    );

    expect(metadataResponse.status).toBe(200);
    const metadata = (await metadataResponse.json()) as Record<string, unknown>;
    expect(metadata.registration_endpoint).toBeUndefined();
    expect(metadata.client_id_metadata_document_supported).toBeUndefined();

    const registrationResponse = await auth.fetch(
      new Request("https://auth.test/api/better-auth/oauth2/register", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          client_name: "untrusted client",
          redirect_uris: ["https://client.example/callback"],
          token_endpoint_auth_method: "none",
        }),
      }),
      env,
    );
    expect(registrationResponse.status).toBe(403);
  });

  it("lists only the caller's Better Auth MCP grants", async () => {
    const env = environment();
    const database = databases.at(-1)?.database;
    const now = Date.now();
    const insertUser = database?.prepare(
      `INSERT INTO user
         (id, name, email, emailVerified, createdAt, updatedAt)
       VALUES (?, ?, ?, ?, ?, ?)`,
    );
    insertUser?.run(
      "grant-user",
      "Grant User",
      "grant@example.test",
      1,
      now,
      now,
    );
    insertUser?.run(
      "other-user",
      "Other User",
      "other@example.test",
      1,
      now,
      now,
    );
    const insertClient = database?.prepare(
      `INSERT INTO oauthClient (id, clientId, name, uri, redirectUris)
       VALUES (?, ?, ?, ?, ?)`,
    );
    insertClient?.run(
      "grant-client-row",
      "grant-client",
      "Grant Client",
      "https://client.example",
      JSON.stringify(["https://client.example/callback"]),
    );
    insertClient?.run(
      "other-client-row",
      "other-client",
      "Other Client",
      "https://other.example",
      JSON.stringify(["https://other.example/callback"]),
    );
    const insertConsent = database?.prepare(
      `INSERT INTO oauthConsent
         (id, clientId, userId, resources, scopes, createdAt, updatedAt)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
    );
    insertConsent?.run(
      "grant-1",
      "grant-client",
      "grant-user",
      JSON.stringify(["https://edge.test/v1/mcp/sse"]),
      JSON.stringify(["memories.read", "offline_access"]),
      now - 100,
      now,
    );
    insertConsent?.run(
      "grant-2",
      "other-client",
      "other-user",
      JSON.stringify(["https://edge.test/v1/mcp/sse"]),
      JSON.stringify(["conversations.read"]),
      now,
      now,
    );

    const response = await auth.fetch(
      await internalAuthRequest(
        "https://auth.test/internal/mcp/grants",
        "GET",
        "grant-user",
      ),
      env,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      grants: [
        expect.objectContaining({
          id: "grant-1",
          client_id: "grant-client",
          client_name: "Grant Client",
          client_uri: "https://client.example",
          resource: "https://edge.test/v1/mcp/sse",
          resources: ["https://edge.test/v1/mcp/sse"],
          scopes: ["memories.read", "offline_access"],
          status: "active",
          revoked_at: null,
        }),
      ],
    });

    const otherDelete = await auth.fetch(
      await internalAuthRequest(
        "https://auth.test/internal/mcp/grants/grant-2",
        "DELETE",
        "grant-user",
      ),
      env,
    );
    expect(otherDelete.status).toBe(404);
    expect(
      database
        ?.prepare("SELECT COUNT(*) AS count FROM oauthConsent WHERE id = ?")
        .get("grant-2"),
    ).toEqual({ count: 1 });
  });

  it("verifies an audience-bound MCP token without forwarding it to API workers", async () => {
    const env = environment();
    const { publicJwk, token } = await accessToken();
    databases
      .at(-1)
      ?.database.prepare(
        `INSERT INTO jwks
           (id, publicKey, privateKey, createdAt, alg, crv)
         VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .run(
        "mcp-test-key",
        JSON.stringify(publicJwk),
        "unused-test-private-key",
        new Date().toISOString(),
        "ES256",
        "P-256",
      );
    const database = databases.at(-1)?.database;
    database
      ?.prepare(
        `INSERT INTO user
           (id, name, email, emailVerified, createdAt, updatedAt)
         VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .run(
        "mcp-user",
        "MCP User",
        "mcp-user@example.test",
        1,
        Date.now(),
        Date.now(),
      );
    database
      ?.prepare(
        `INSERT INTO oauthClient (id, clientId, redirectUris)
         VALUES (?, ?, ?)`,
      )
      .run(
        "mcp-client-row",
        "mcp-test-client",
        JSON.stringify(["https://client.example/callback"]),
      );
    database
      ?.prepare(
        `INSERT INTO oauthConsent
           (id, clientId, userId, resources, scopes, createdAt, updatedAt)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        "mcp-consent-row",
        "mcp-test-client",
        "mcp-user",
        JSON.stringify(["https://edge.test/v1/mcp/sse"]),
        JSON.stringify(["memories.read", "offline_access"]),
        Date.now(),
        Date.now(),
      );
    const publicFetch = vi.fn(async () => {
      throw new Error("MCP verification must not fetch its own public Worker");
    });
    vi.stubGlobal("fetch", publicFetch);

    const response = await auth.fetch(
      new Request("https://auth.test/internal/mcp/verify", {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
          "x-internal-assertion-secret": "internal-secret",
        },
        body: JSON.stringify({
          method: "POST",
          url: "https://edge.test/v1/mcp/sse",
        }),
      }),
      env,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      uid: "mcp-user",
      scopes: ["memories.read"],
      clientId: "mcp-test-client",
    });
    expect(publicFetch).not.toHaveBeenCalled();

    const mismatchedResource = await auth.fetch(
      new Request("https://auth.test/internal/mcp/verify", {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
          "x-internal-assertion-secret": "internal-secret",
        },
        body: JSON.stringify({
          method: "POST",
          url: "https://attacker.example/v1/mcp/sse",
        }),
      }),
      env,
    );
    expect(mismatchedResource.status).toBe(400);

    const revoke = await auth.fetch(
      await internalAuthRequest(
        "https://auth.test/internal/mcp/grants/mcp-consent-row",
        "DELETE",
        "mcp-user",
      ),
      env,
    );
    expect(revoke.status).toBe(204);

    const revokedToken = await auth.fetch(
      new Request("https://auth.test/internal/mcp/verify", {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
          "x-internal-assertion-secret": "internal-secret",
        },
        body: JSON.stringify({
          method: "POST",
          url: "https://edge.test/v1/mcp/sse",
        }),
      }),
      env,
    );
    expect(revokedToken.status).toBe(401);
  });
});
