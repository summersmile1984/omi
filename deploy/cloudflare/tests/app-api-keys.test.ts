import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import jobs from "../workers/jobs/index";
import type { JobsEnv } from "../workers/jobs/env";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

type BoundStatement = {
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
          | T
          | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database.prepare(sql).all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        const statement = this.database.prepare(sql);
        if (/^SELECT\b/i.test(sql.trimStart())) {
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

  async batch(statements: BoundStatement[]) {
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

function environment() {
  const database = new SqliteD1();
  databases.push(database);
  const env = {
    APP_DB: database as unknown as D1Database,
    INTERNAL_ASSERTION_SECRET: "app-api-key-test-secret",
  } as unknown as JobsEnv;
  database.database
    .prepare(
      "INSERT INTO cf_app_catalog " +
        "(id, approved, status, disabled, data_json, updated_at, owner_uid) " +
        "VALUES ('integration-app', 1, 'approved', 0, ?, 1, 'owner-user')",
    )
    .run(
      JSON.stringify({
        id: "integration-app",
        name: "Integration App",
        capabilities: ["external_integration"],
      }),
    );
  return { database, env };
}

async function headers(
  method: "GET" | "POST" | "DELETE",
  pathname: string,
  uid = "owner-user",
) {
  const signed = await createSignedAuthContext(
    {
      uid,
      authority: "better-auth",
      requestId: `app-key-${method.toLowerCase()}`,
    },
    "jobs",
    method,
    pathname,
    "app-api-key-test-secret",
  );
  if (!signed) throw new Error("missing signed context");
  return {
    [AUTH_CONTEXT_HEADER]: signed.encoded,
    [AUTH_SIGNATURE_HEADER]: signed.signature,
  };
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("Cloudflare app API keys", () => {
  it("creates a one-time secret, stores only its hash, lists metadata, and deletes it", async () => {
    const { database, env } = environment();
    const pathname = "/v1/apps/integration-app/keys";
    const created = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        method: "POST",
        headers: await headers("POST", pathname),
      }),
      env,
    );
    expect(created.status).toBe(200);
    expect(created.headers.get("cache-control")).toBe("no-store");
    const body = (await created.json()) as {
      id: string;
      secret: string;
      label: string;
      created_at: string;
    };
    expect(body.secret).toMatch(/^sk_[0-9a-f]{32}$/);
    expect(body.label).toMatch(/^sk_[0-9a-f]{4}\.\.\.[0-9a-f]{4}$/);

    const stored = database.database
      .prepare(
        "SELECT key_id, key_hash, label FROM cf_app_api_keys WHERE app_id = 'integration-app'",
      )
      .get() as { key_id: string; key_hash: string; label: string };
    expect(stored.key_id).toBe(body.id);
    expect(stored.label).toBe(body.label);
    expect(stored.key_hash).toBe(
      createHash("sha256").update(body.secret.slice(3)).digest("hex"),
    );
    expect(JSON.stringify(stored)).not.toContain(body.secret);

    const listed = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        headers: await headers("GET", pathname),
      }),
      env,
    );
    expect(await listed.json()).toEqual([
      { id: body.id, label: body.label, created_at: body.created_at },
    ]);

    const stranger = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        headers: await headers("GET", pathname, "stranger"),
      }),
      env,
    );
    expect(stranger.status).toBe(403);

    const deletePath = `${pathname}/${body.id}`;
    const deleted = await jobs.fetch(
      new Request(`https://jobs.test${deletePath}`, {
        method: "DELETE",
        headers: await headers("DELETE", deletePath),
      }),
      env,
    );
    expect(await deleted.json()).toEqual({
      status: "ok",
      message: "API key deleted",
    });
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_app_api_keys")
        .get(),
    ).toEqual({ count: 0 });
  });

  it("cascades keys when the owning app is deleted", async () => {
    const { database, env } = environment();
    const pathname = "/v1/apps/integration-app/keys";
    const created = await jobs.fetch(
      new Request(`https://jobs.test${pathname}`, {
        method: "POST",
        headers: await headers("POST", pathname),
      }),
      env,
    );
    expect(created.status).toBe(200);
    database.database
      .prepare("DELETE FROM cf_app_catalog WHERE id = 'integration-app'")
      .run();
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_app_api_keys")
        .get(),
    ).toEqual({ count: 0 });
  });
});
