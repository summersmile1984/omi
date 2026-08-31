import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const migrationDirectory = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../migrations/app",
);
const fixturePath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "fixtures/mcp-app-oauth-contract.json",
);

function database() {
  const database = new DatabaseSync(":memory:");
  database.exec("PRAGMA foreign_keys = ON");
  for (const filename of readdirSync(migrationDirectory)
    .filter((value) => value.endsWith(".sql"))
    .sort()) {
    database.exec(readFileSync(path.join(migrationDirectory, filename), "utf8"));
  }
  return database;
}

const ENCRYPTED = `v1.${"a".repeat(22)}.${"b".repeat(22)}`;
const NOW = 1_788_000_000;

function insertApp(db: DatabaseSync, id: string, ownerUid: string) {
  db.prepare(
    `INSERT INTO cf_app_catalog
       (id, approved, status, disabled, is_popular, installs, rating_count,
        data_json, updated_at, owner_uid)
     VALUES (?, 0, 'approved', 0, 0, 0, 0, ?, ?, ?)`,
  ).run(id, JSON.stringify({ id, capabilities: ["chat"] }), NOW, ownerUid);
}

describe("external MCP app authority foundation", () => {
  it("keeps the provider wire fixture explicit (static contract tripwire)", () => {
    const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as Record<
      string,
      any
    >;
    expect(fixture.registration_request.token_endpoint_auth_method).toBe("none");
    expect(fixture.authorization_request.code_challenge_method).toBe("S256");
    expect(fixture.token_response.token_type).toBe("Bearer");
    expect(fixture.mcp_initialize_request.method).toBe("initialize");
    expect(fixture.mcp_tools_list_response.result.tools).toHaveLength(1);
    expect(fixture.negative_cases).toEqual(
      expect.arrayContaining([
        "invalid_state",
        "replayed_state",
        "cross_owner_app",
        "provider_401_refresh_then_discovery_failure",
      ]),
    );
  });

  it("provides separate encrypted OAuth, connection, and discovery state", () => {
    const db = database();
    try {
      const connectionColumns = db
        .prepare("PRAGMA table_info(cf_mcp_app_connections)")
        .all() as Array<{ name: string }>;
      expect(connectionColumns.map((column) => column.name)).toContain(
        "oauth_transaction_id",
      );
      insertApp(db, "mcp-app", "mcp-owner");
      db.prepare(
        `INSERT INTO cf_mcp_app_connections
           (app_id, owner_uid, server_url, resolved_endpoint, status,
            oauth_metadata_json, credential_envelope_enc, created_at, updated_at)
         VALUES (?, ?, ?, ?, 'authorized', ?, ?, ?, ?)`,
      ).run(
        "mcp-app",
        "mcp-owner",
        "https://mcp.example.test",
        "https://mcp.example.test/http",
        JSON.stringify({ authorization_endpoint: "https://mcp.example.test/authorize" }),
        ENCRYPTED,
        NOW,
        NOW,
      );
      db.prepare(
        `INSERT INTO cf_mcp_app_oauth_transactions
           (transaction_id, app_id, owner_uid, state_hash, code_verifier_enc,
            client_credentials_enc, authorization_endpoint, token_endpoint,
            client_id, redirect_uri, expires_at, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).run(
        "tx-1",
        "mcp-app",
        "mcp-owner",
        "a".repeat(64),
        ENCRYPTED,
        ENCRYPTED,
        "https://mcp.example.test/authorize",
        "https://mcp.example.test/token",
        "mcp-client-fixture",
        "https://edge.example.test/v1/apps/mcp/callback",
        NOW + 600,
        NOW,
        NOW,
      );
      db.prepare(
        `INSERT INTO cf_mcp_app_discoveries
           (app_id, owner_uid, endpoint, protocol_version, tools_json,
            fetched_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
      ).run(
        "mcp-app",
        "mcp-owner",
        "https://mcp.example.test/http",
        "2025-03-26",
        JSON.stringify([{ name: "fixture_search", inputSchema: { type: "object" } }]),
        NOW,
        NOW,
      );

      expect(
        db
          .prepare(
            `SELECT name FROM sqlite_master
             WHERE type = 'table' AND name LIKE 'cf_mcp_app_%'
             ORDER BY name`,
          )
          .all(),
      ).toEqual([
        { name: "cf_mcp_app_connections" },
        { name: "cf_mcp_app_discoveries" },
        { name: "cf_mcp_app_oauth_transactions" },
      ]);
      expect(
        db
          .prepare(
            "SELECT owner_uid, status, revision FROM cf_mcp_app_connections WHERE app_id = ?",
          )
          .get("mcp-app"),
      ).toEqual({ owner_uid: "mcp-owner", status: "authorized", revision: 0 });
    } finally {
      db.close();
    }
  });

  it("rejects plaintext credentials and malformed callback state", () => {
    const db = database();
    try {
      insertApp(db, "mcp-app", "mcp-owner");
      expect(() =>
        db
          .prepare(
            `INSERT INTO cf_mcp_app_connections
               (app_id, owner_uid, server_url, oauth_metadata_json,
                credential_envelope_enc, created_at, updated_at)
             VALUES (?, ?, ?, '{}', ?, ?, ?)`,
          )
          .run("mcp-app", "mcp-owner", "https://mcp.example.test", "plain-access-token", NOW, NOW),
      ).toThrow();
      expect(() =>
        db
          .prepare(
            `INSERT INTO cf_mcp_app_oauth_transactions
               (transaction_id, app_id, owner_uid, state_hash, code_verifier_enc,
                authorization_endpoint, token_endpoint, client_id, redirect_uri,
                expires_at, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          )
          .run(
            "tx-invalid",
            "mcp-app",
            "mcp-owner",
            "not-a-sha256",
            ENCRYPTED,
            "https://mcp.example.test/authorize",
            "https://mcp.example.test/token",
            "mcp-client-fixture",
            "https://edge.example.test/v1/apps/mcp/callback",
            NOW + 600,
            NOW,
            NOW,
          ),
      ).toThrow();
    } finally {
      db.close();
    }
  });

  it("fences inserts and updates while account deletion is active", () => {
    const db = database();
    try {
      insertApp(db, "mcp-app", "mcp-owner");
      insertApp(db, "mcp-app-2", "mcp-owner");
      db.prepare(
        `INSERT INTO cf_mcp_app_connections
           (app_id, owner_uid, server_url, oauth_metadata_json,
            credential_envelope_enc, created_at, updated_at)
         VALUES (?, ?, ?, '{}', ?, ?, ?)`,
      ).run("mcp-app", "mcp-owner", "https://mcp.example.test", ENCRYPTED, NOW, NOW);
      db.prepare(
        `INSERT INTO cf_account_deletion_intents
           (uid, job_id, status, phase, next_attempt_at, created_at, updated_at)
         VALUES (?, ?, 'pending', 'quiescing', ?, ?, ?)`,
      ).run("mcp-owner", "delete-mcp-owner", NOW, NOW, NOW);

      expect(() =>
        db
          .prepare(
            `INSERT INTO cf_mcp_app_oauth_transactions
               (transaction_id, app_id, owner_uid, state_hash, code_verifier_enc,
                authorization_endpoint, token_endpoint, client_id, redirect_uri,
                expires_at, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          )
          .run(
            "tx-fenced",
            "mcp-app-2",
            "mcp-owner",
            "b".repeat(64),
            ENCRYPTED,
            "https://mcp.example.test/authorize",
            "https://mcp.example.test/token",
            "mcp-client-fixture",
            "https://edge.example.test/v1/apps/mcp/callback",
            NOW + 600,
            NOW,
            NOW,
          ),
      ).toThrow(/account deletion fence/);
      expect(() =>
        db
          .prepare(
            "UPDATE cf_mcp_app_connections SET revision = revision + 1 WHERE app_id = ?",
          )
          .run("mcp-app"),
      ).toThrow(/account deletion fence/);
    } finally {
      db.close();
    }
  });
});
