import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import jobs from "../workers/jobs/index";
import type { JobsEnv } from "../workers/jobs/env";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

const MCP_SCOPES = [
  "action_items.read",
  "action_items.write",
  "chat.read",
  "conversations.read",
  "goals.read",
  "memories.read",
  "memories.write",
  "people.read",
  "screen_activity.read",
];

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
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          | T
          | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database.prepare(sql).all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => {
        const result = this.database
          .prepare(sql)
          .run(...args.map(sqliteValue));
        return {
          success: true as const,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  close() {
    this.database.close();
  }
}

const databases: SqliteD1[] = [];

function environment() {
  const database = new SqliteD1();
  databases.push(database);
  return {
    database,
    env: {
      APP_DB: database as unknown as D1Database,
      INTERNAL_ASSERTION_SECRET: "mcp-key-test-secret",
    } as unknown as JobsEnv,
  };
}

async function headers(
  method: "GET" | "POST" | "DELETE",
  pathname: string,
  uid = "mcp-owner",
) {
  const signed = await createSignedAuthContext(
    {
      uid,
      authority: "better-auth",
      requestId: `mcp-key-${method.toLowerCase()}`,
    },
    "jobs",
    method,
    pathname,
    "mcp-key-test-secret",
  );
  if (!signed) throw new Error("missing signed context");
  return {
    "content-type": "application/json",
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
  vi.useRealTimers();
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-08-30T02:00:00.000Z"));
});

describe("Cloudflare MCP API keys", () => {
  it("keeps the final prefix constraint within the D1 GLOB complexity boundary", () => {
    const { database } = environment();
    const row = database.database
      .prepare(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cf_mcp_api_keys'",
      )
      .get() as { sql: string };
    expect(row.sql).not.toContain("[0-9a-f][0-9a-f]");
    expect(row.sql).toContain(
      "substr(key_prefix, 9, 4) NOT GLOB '*[^0-9a-f]*'",
    );
  });

  it("returns a one-time full-access key, stores only its hash, lists metadata, and revokes immediately", async () => {
    const { database, env } = environment();
    const pathname = "/v1/mcp/keys";
    const created = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        method: "POST",
        headers: await headers("POST", pathname),
        body: JSON.stringify({ name: "Claude Desktop" }),
      }),
      env,
    );
    expect(created.status).toBe(200);
    expect(created.headers.get("cache-control")).toBe("no-store");
    const body = (await created.json()) as {
      id: string;
      key: string;
      key_prefix: string;
      created_at: string;
      last_used_at: null;
      name: string;
      app_id: string;
      scopes: string[];
    };
    expect(body).toMatchObject({
      name: "Claude Desktop",
      app_id: "mcp-api",
      scopes: MCP_SCOPES,
      last_used_at: null,
    });
    expect(body.key).toMatch(/^omi_mcp_[0-9a-f]{32}$/);
    expect(body.key_prefix).toMatch(
      /^omi_mcp_[0-9a-f]{4}\.\.\.[0-9a-f]{4}$/,
    );

    const stored = database.database
      .prepare(
        "SELECT uid, key_hash, key_prefix, scopes_json FROM cf_mcp_api_keys WHERE key_id = ?",
      )
      .get(body.id) as {
      uid: string;
      key_hash: string;
      key_prefix: string;
      scopes_json: string;
    };
    expect(stored.uid).toBe("mcp-owner");
    expect(stored.key_hash).toBe(
      createHash("sha256").update(body.key.slice("omi_mcp_".length)).digest("hex"),
    );
    expect(JSON.stringify(stored)).not.toContain(body.key);

    const listed = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        headers: await headers("GET", pathname),
      }),
      env,
    );
    expect(listed.status).toBe(200);
    const listedBody = (await listed.json()) as Array<Record<string, unknown>>;
    const { key: _rawKey, ...metadata } = body;
    expect(listedBody).toEqual([metadata]);
    expect(JSON.stringify(listedBody)).not.toContain(body.key);

    const deletePath = `${pathname}/${body.id}`;
    const deleted = await jobs.fetch(
      new Request(`https://jobs.test${deletePath}`, {
        method: "DELETE",
        headers: await headers("DELETE", deletePath),
      }),
      env,
    );
    expect(deleted.status).toBe(204);
    expect(await deleted.text()).toBe("");
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_mcp_api_keys")
        .get(),
    ).toEqual({ count: 0 });
  });

  it("isolates keys by uid and preserves idempotent missing-key deletion", async () => {
    const { database, env } = environment();
    database.database
      .prepare(
        `INSERT INTO cf_mcp_api_keys
           (uid, key_id, name, key_hash, key_prefix, scopes_json, created_at)
         VALUES ('other-user', 'other-key', 'Other', ?,
                 'omi_mcp_1234...abcd', ?, 1)`,
      )
      .run("a".repeat(64), JSON.stringify(MCP_SCOPES));
    const listed = await jobs.fetch(
      new Request("https://jobs.test/v1/mcp/keys", {
        headers: await headers("GET", "/v1/mcp/keys"),
      }),
      env,
    );
    await expect(listed.json()).resolves.toEqual([]);

    const path = "/v1/mcp/keys/other-key";
    const deleted = await jobs.fetch(
      new Request(`https://jobs.test${path}`, {
        method: "DELETE",
        headers: await headers("DELETE", path),
      }),
      env,
    );
    expect(deleted.status).toBe(204);
    expect(
      database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_mcp_api_keys WHERE uid = 'other-user'",
        )
        .get(),
    ).toEqual({ count: 1 });
  });

  it("rejects empty, oversized, and raw-secret-bearing names without a write", async () => {
    const { database, env } = environment();
    for (const name of ["   ", "x".repeat(257), `unsafe omi_mcp_${"a".repeat(32)}`]) {
      const response = await jobs.fetch(
        new Request("https://jobs.test/v1/mcp/keys", {
          method: "POST",
          headers: await headers("POST", "/v1/mcp/keys"),
          body: JSON.stringify({ name }),
        }),
        env,
      );
      expect(response.status).toBe(422);
    }
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_mcp_api_keys")
        .get(),
    ).toEqual({ count: 0 });
  });
});
