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

const READ_ONLY_SCOPES = [
  "conversations:read",
  "memories:read",
  "action_items:read",
  "goals:read",
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
          T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
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
      INTERNAL_ASSERTION_SECRET: "developer-key-test-secret",
    } as unknown as JobsEnv,
  };
}

async function headers(
  method: "GET" | "POST" | "DELETE",
  pathname: string,
  uid = "developer-owner",
) {
  const signed = await createSignedAuthContext(
    {
      uid,
      authority: "better-auth",
      requestId: `developer-key-${method.toLowerCase()}`,
    },
    "jobs",
    method,
    pathname,
    "developer-key-test-secret",
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
  vi.setSystemTime(new Date("2026-08-30T05:00:00.000Z"));
});

describe("Cloudflare Developer API keys", () => {
  it("returns a one-time key, stores only its digest, lists metadata, and revokes immediately", async () => {
    const { database, env } = environment();
    const pathname = "/v1/dev/keys";
    const created = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        method: "POST",
        headers: await headers("POST", pathname),
        body: JSON.stringify({ name: "Local automation" }),
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
      scopes: string[];
    };
    expect(body).toMatchObject({
      name: "Local automation",
      scopes: READ_ONLY_SCOPES,
      last_used_at: null,
    });
    expect(body.key).toMatch(/^omi_dev_[0-9a-f]{32}$/);
    expect(body.key_prefix).toMatch(/^omi_dev_[0-9a-f]{4}\.\.\.[0-9a-f]{4}$/);

    const stored = database.database
      .prepare(
        "SELECT uid, key_hash, key_prefix, scopes_json FROM cf_developer_api_keys WHERE key_id = ?",
      )
      .get(body.id) as {
      uid: string;
      key_hash: string;
      key_prefix: string;
      scopes_json: string;
    };
    expect(stored.uid).toBe("developer-owner");
    expect(stored.key_hash).toBe(
      createHash("sha256")
        .update(body.key.slice("omi_dev_".length))
        .digest("hex"),
    );
    expect(JSON.stringify(stored)).not.toContain(body.key);

    const listed = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        headers: await headers("GET", pathname),
      }),
      env,
    );
    expect(listed.status).toBe(200);
    const { key: _rawKey, ...metadata } = body;
    await expect(listed.json()).resolves.toEqual([metadata]);

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
        .prepare("SELECT COUNT(*) AS count FROM cf_developer_api_keys")
        .get(),
    ).toEqual({ count: 0 });
  });

  it("preserves custom scopes and rejects invalid bodies, names, and scopes", async () => {
    const { database, env } = environment();
    const pathname = "/v1/dev/keys";
    const custom = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        method: "POST",
        headers: await headers("POST", pathname),
        body: JSON.stringify({
          name: "Memory writer",
          scopes: ["memories:write"],
        }),
      }),
      env,
    );
    expect(custom.status).toBe(200);
    await expect(custom.json()).resolves.toMatchObject({
      scopes: ["memories:write"],
    });

    for (const testCase of [
      { body: "not-json", status: 422 },
      { body: JSON.stringify({ name: "   " }), status: 422 },
      {
        body: JSON.stringify({
          name: `unsafe omi_dev_${"a".repeat(32)}`,
        }),
        status: 422,
      },
      {
        body: JSON.stringify({ name: "Invalid scope", scopes: ["admin"] }),
        status: 400,
      },
    ]) {
      const response = await jobs.fetch(
        new Request(`https://jobs.test${pathname}`, {
          method: "POST",
          headers: await headers("POST", pathname),
          body: testCase.body,
        }),
        env,
      );
      expect(response.status).toBe(testCase.status);
    }
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_developer_api_keys")
        .get(),
    ).toEqual({ count: 1 });
  });

  it("isolates uid ownership, fences deleting accounts, and requires a signed Better Auth context", async () => {
    const { database, env } = environment();
    database.database
      .prepare(
        `INSERT INTO cf_developer_api_keys
           (uid, key_id, name, key_hash, key_prefix, scopes_json, created_at)
         VALUES ('other-user', 'other-key', 'Other', ?,
                 'omi_dev_1234...abcd', ?, 1)`,
      )
      .run("a".repeat(64), JSON.stringify(READ_ONLY_SCOPES));

    const listed = await jobs.fetch(
      new Request("https://jobs.test/v1/dev/keys", {
        headers: await headers("GET", "/v1/dev/keys"),
      }),
      env,
    );
    await expect(listed.json()).resolves.toEqual([]);

    const deletePath = "/v1/dev/keys/other-key";
    const deleted = await jobs.fetch(
      new Request(`https://jobs.test${deletePath}`, {
        method: "DELETE",
        headers: await headers("DELETE", deletePath),
      }),
      env,
    );
    expect(deleted.status).toBe(204);
    expect(
      database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_developer_api_keys WHERE uid = 'other-user'",
        )
        .get(),
    ).toEqual({ count: 1 });

    database.database
      .prepare(
        `INSERT INTO cf_account_deletion_intents
           (uid, job_id, status, phase, next_attempt_at, created_at, updated_at)
         VALUES ('deleting-user', 'delete-job', 'pending', 'quiescing', 1, 1, 1)`,
      )
      .run();
    const fenced = await jobs.fetch(
      new Request("https://jobs.test/v1/dev/keys", {
        method: "POST",
        headers: await headers("POST", "/v1/dev/keys", "deleting-user"),
        body: JSON.stringify({ name: "Must not persist" }),
      }),
      env,
    );
    expect(fenced.status).toBe(503);

    const unsigned = await jobs.fetch(
      new Request("https://jobs.test/v1/dev/keys"),
      env,
    );
    expect(unsigned.status).toBe(401);
  });
});
