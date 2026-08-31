import { createHmac, createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { afterEach, describe, expect, it } from "vitest";
import { planChatHistoryReconciliation } from "../scripts/chat-history-reconcile.mjs";
import type { JobsEnv } from "../workers/jobs/env";
import { registerChatHistoryImportRoutes } from "../workers/jobs/chat-history-import";

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
const SIGNING_SECRET = "chat-history-plan-signing-secret-0123456789";
const UID = "chat-history-user";
const SOURCE_EXPORT = "e".repeat(64);
const SOURCE_FINGERPRINT = "f".repeat(64);

function environment(enabled = true) {
  const database = new SqliteD1();
  databases.push(database);
  database.database
    .prepare(
      `INSERT INTO cf_account_cutover
         (uid, state, account_generation, checkpoint_phase,
          destination_backend_bound, updated_at)
       VALUES (?, 'new', 7, 'completed', 1, 1700000000)`,
    )
    .run(UID);
  const env = {
    APP_DB: database as unknown as D1Database,
    ADMIN_KEY: "chat-history-admin-key",
    CHAT_HISTORY_IMPORT_STAGING_ENABLED: enabled ? "true" : "false",
    CHAT_HISTORY_IMPORT_SIGNING_SECRET: SIGNING_SECRET,
  } as unknown as JobsEnv;
  return { database, env };
}

function manifest() {
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collections: [
        "users/{uid}/chat_sessions",
        "users/{uid}/messages",
      ],
      export_sha256: SOURCE_EXPORT,
    },
    accounts: [
      { uid: UID, account_generation: 7, source_fingerprint: SOURCE_FINGERPRINT },
    ],
    sessions: [
      {
        uid: UID,
        id: "session-1",
        title: "Imported session",
        preview: "hello",
        app_id: null,
        message_count: 1,
        starred: false,
        created_at: 1700000000,
        updated_at: 1700000001,
      },
    ],
    messages: [
      {
        uid: UID,
        id: "message-1",
        app_id: null,
        created_at: 1700000001,
        message_json: {
          id: "message-1",
          text: "hello",
          sender: "human",
          type: "text",
          chat_session_id: "session-1",
        },
      },
    ],
  };
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function batchId(plan: Record<string, unknown>): string {
  const entries = plan.entries as Array<Record<string, unknown>>;
  const ordered = [...entries].sort((left, right) =>
    `${left.uid}\0${left.entityKind}\0${left.entityId}`.localeCompare(
      `${right.uid}\0${right.entityKind}\0${right.entityId}`,
    ),
  );
  return sha256(`${plan.manifestHash}\0${ordered.map((entry) => entry.importId).join("\0")}`);
}

function signedHeaders(plan: Record<string, unknown>, body: Record<string, unknown>) {
  const id = batchId(plan);
  const manifestHash = String(body.manifestHash);
  const signature = createHmac("sha256", SIGNING_SECRET)
    .update(`${id}\0${manifestHash}`)
    .digest("base64url");
  return {
    "content-type": "application/json",
    "secret-key": "chat-history-admin-key",
    "x-chat-history-plan-signature": signature,
  };
}

function requestApp(env: JobsEnv) {
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerChatHistoryImportRoutes(app);
  return (body: BodyInit | null, headers: HeadersInit = {}) =>
    app.request(
      "https://jobs.test/internal/chat-history/apply",
      { method: "POST", body, headers },
      env,
    );
}

afterEach(() => {
  while (databases.length) databases.pop()?.close();
});

describe("Cloudflare chat-history apply executor", () => {
  it("stays fail-closed unless the explicit staging gate is enabled", async () => {
    const { env } = environment(false);
    const response = await requestApp(env)(JSON.stringify({ entries: [] }));
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "chat_history_import_unavailable" });
  });

  it("requires the admin key and content-bound plan signature", async () => {
    const { env } = environment();
    const request = requestApp(env);
    const unauthorized = await request(JSON.stringify({ entries: [] }));
    expect(unauthorized.status).toBe(403);

    const plan = planChatHistoryReconciliation(manifest());
    const body = { ...plan, batch_id: batchId(plan) };
    const invalid = await request(JSON.stringify(body), {
      "content-type": "application/json",
      "secret-key": "chat-history-admin-key",
      "x-chat-history-plan-signature": "invalid",
    });
    expect(invalid.status).toBe(403);
    expect(await invalid.json()).toEqual({ error: "plan_signature_invalid" });
  });

  it("applies sessions before messages and replays idempotently", async () => {
    const { database, env } = environment();
    const plan = planChatHistoryReconciliation(manifest());
    expect(plan.stage).toBe(2);
    const body = { ...plan, batch_id: batchId(plan) };
    const request = requestApp(env);
    const applied = await request(JSON.stringify(body), signedHeaders(plan, body));
    expect(applied.status).toBe(200);
    expect(await applied.json()).toMatchObject({
      status: "applied",
      entry_count: 2,
      applied_count: 2,
      already_applied_count: 0,
    });
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_chat_history_apply_receipts")
        .get()?.count,
    ).toBe(2);
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_chat_sessions WHERE uid = ?")
        .get(UID)?.count,
    ).toBe(1);
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_chat_messages WHERE uid = ?")
        .get(UID)?.count,
    ).toBe(1);

    const replay = await request(JSON.stringify(body), signedHeaders(plan, body));
    expect(replay.status).toBe(200);
    expect(await replay.json()).toMatchObject({
      status: "applied",
      applied_count: 0,
      already_applied_count: 2,
    });
  });

  it("rejects a deletion-fenced apply before writing any ledger row", async () => {
    const { database, env } = environment();
    database.database
      .prepare(
        "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES (?, ?, 'pending', 'quiescing', ?, ?, ?)",
      )
      .run(UID, "delete-chat-history", 1700000002, 1700000002, 1700000002);
    const plan = planChatHistoryReconciliation(manifest());
    const body = { ...plan, batch_id: batchId(plan) };
    const response = await requestApp(env)(
      JSON.stringify(body),
      signedHeaders(plan, body),
    );
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "chat_history_authority_changed" });
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_chat_history_import_ledger")
        .get()?.count,
    ).toBe(0);
  });

  it("rejects malformed UTF-8 without invoking the database", async () => {
    const { env } = environment();
    const response = await requestApp(env)(
      new Uint8Array([0xc3, 0x28]),
      {
        "content-type": "application/json",
        "secret-key": "chat-history-admin-key",
      },
    );
    expect(response.status).toBe(422);
    expect(await response.json()).toEqual({ error: "invalid_request" });
  });
});
