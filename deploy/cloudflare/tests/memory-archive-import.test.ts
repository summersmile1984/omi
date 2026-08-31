import { DatabaseSync } from "node:sqlite";
import { createHash, createHmac } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import { registerMemoryArchiveImportRoutes } from "../workers/jobs/memory-archive-import";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    const directory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../migrations/app");
    for (const filename of readdirSync(directory).filter((name) => name.endsWith(".sql")).sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const make = (args: unknown[] = []) => ({
      __sql: sql,
      bind: (...values: unknown[]) => make(values),
      first: async <T>() => (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ?? null,
      all: async <T>() => ({ results: this.database.prepare(sql).all(...(args as never[]) as never[]) as T[] }),
      run: async () => {
        const result = this.database.prepare(sql).run(...(args as never[]));
        return { meta: { changes: Number(result.changes) } };
      },
    });
    return make();
  }

  async batch(statements: Array<ReturnType<SqliteD1["prepare"]>>) {
    this.database.exec("BEGIN");
    try {
      const results = [];
      for (const statement of statements) results.push(await statement.run());
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

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(stableJson).join(",") + "]";
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return "{" + Object.keys(object).sort().map((key) => JSON.stringify(key) + ":" + stableJson(object[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function signed(payload: string): string {
  return createHmac("sha256", "archive-signing-secret-which-is-long-enough")
    .update(payload)
    .digest("base64url");
}

function fixture() {
  const row = {
    uid: "archive-user",
    memory_id: "memory-1",
    memory_tier: "archive",
    content: "A reviewed archive memory",
    version: 1,
    status: "active",
    processing_state: "processed",
    source_state: "active",
    sensitivity_labels: [],
    visibility: "private",
    user_asserted: 0,
    captured_at: 1700000000,
    updated_at: 1700000000,
    expires_at: null,
    ledger_commit_id: null,
    ledger_sequence: null,
    item_revision: 1,
    source_id: "firestore-memory-1",
    evidence: [],
    confidence: null,
    superseded_by: null,
    is_locked: 0,
    account_generation: 2,
    created_at: 1700000000,
    deleted_at: null,
  };
  const source = {
    kind: "firestore",
    collection: "users/{uid}/memories",
    export_sha256: "b".repeat(64),
  };
  const sourceFingerprint = "a".repeat(64);
  const sourceRowSha256 = sha256(stableJson({
    uid: row.uid,
    memory_id: row.memory_id,
    source_fingerprint: sourceFingerprint,
    account_generation: row.account_generation,
    row,
  }));
  const importId = sha256(row.uid + "\0archive\0" + row.memory_id + "\0" + sourceFingerprint + "\0" + sourceRowSha256);
  const planHash = sha256(stableJson({
    uid: row.uid,
    memory_id: row.memory_id,
    account_generation: row.account_generation,
    source_fingerprint: sourceFingerprint,
    source_row_sha256: sourceRowSha256,
    import_id: importId,
    action: "stage",
    last_error: null,
  }));
  const entry = {
    uid: row.uid,
    memory_id: row.memory_id,
    source_fingerprint: sourceFingerprint,
    source_row_sha256: sourceRowSha256,
    import_id: importId,
    plan_hash: planHash,
    account_generation: row.account_generation,
    row,
    action: "stage",
    status: "planned",
    last_error: null,
  };
  const manifest = sha256(stableJson({ schema_version: 1, source, entries: [sourceRowSha256] }));
  const reviewSignature = signed(stableJson({
    manifest_sha256: manifest,
    entries: [{
      uid: row.uid,
      memory_id: row.memory_id,
      source_fingerprint: sourceFingerprint,
      source_row_sha256: sourceRowSha256,
      import_id: importId,
      plan_hash: planHash,
      account_generation: row.account_generation,
    }],
  }));
  return { row, source, manifest, entry, reviewSignature };
}

function environment() {
  const database = new SqliteD1();
  database.database.exec(
    "INSERT INTO cf_memory_global_read_gate (id, source, memory_reads_enabled, kill_switch_active, updated_at) VALUES (1, 'cloudflare_operator', 1, 0, 1700000000);" +
    "INSERT INTO cf_account_cutover (uid, state, checkpoint_phase, destination_backend_bound, account_generation, updated_at) VALUES ('archive-user', 'new', 'completed', 1, 2, 1700000000);" +
    "INSERT INTO cf_memory_control (uid, source, memory_reads_enabled, default_memory_grant, archive_capability, account_generation, source_revision, updated_at) VALUES ('archive-user', 'cloudflare_cutover_projection', 1, 1, 1, 2, 'projection-1', 1700000000);",
  );
  const env = {
    APP_DB: database,
    ADMIN_KEY: "archive-admin",
    MEMORY_ARCHIVE_IMPORT_STAGING_ENABLED: "true",
    MEMORY_ARCHIVE_IMPORT_SIGNING_SECRET: "archive-signing-secret-which-is-long-enough",
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerMemoryArchiveImportRoutes(app);
  return { app, database, env };
}

describe("Cloudflare reviewed memory Archive projection", () => {
  it("fails closed until the explicit staging gate and admin key are present", async () => {
    const { app, database, env } = environment();
    try {
      env.MEMORY_ARCHIVE_IMPORT_STAGING_ENABLED = "false";
      expect((await app.request("/internal/memory-archive/reviews", { method: "POST" }, env)).status).toBe(503);
      env.MEMORY_ARCHIVE_IMPORT_STAGING_ENABLED = "true";
      expect((await app.request("/internal/memory-archive/reviews", { method: "POST" }, env)).status).toBe(403);
    } finally {
      database.close();
    }
  });

  it("reviews and applies a signed row idempotently, without a provider", async () => {
    const { app, database, env } = environment();
    try {
      const plan = fixture();
      const body = JSON.stringify({ manifest_sha256: plan.manifest, source: plan.source, entries: [plan.entry] });
      const headers = {
        "content-type": "application/json",
        "secret-key": "archive-admin",
        "x-memory-archive-plan-signature": plan.reviewSignature,
      };
      const reviewed = await app.request("/internal/memory-archive/reviews", { method: "POST", headers, body }, env);
      expect(reviewed.status).toBe(201);
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      const applyPayload = stableJson({
        review_id: reviewId,
        manifest_sha256: plan.manifest,
        entries: [{
          uid: plan.entry.uid,
          memory_id: plan.entry.memory_id,
          source_fingerprint: plan.entry.source_fingerprint,
          source_row_sha256: plan.entry.source_row_sha256,
          import_id: plan.entry.import_id,
          plan_hash: plan.entry.plan_hash,
          account_generation: plan.entry.account_generation,
        }],
      });
      const applied = await app.request("/internal/memory-archive/reviews/" + reviewId + "/apply", {
        method: "POST",
        headers: { "secret-key": "archive-admin", "x-memory-archive-plan-signature": signed(applyPayload) },
      }, env);
      expect(applied.status).toBe(200);
      expect(await applied.json()).toMatchObject({ applied_count: 1, already_applied_count: 0 });
      const repeated = await app.request("/internal/memory-archive/reviews/" + reviewId + "/apply", {
        method: "POST",
        headers: { "secret-key": "archive-admin", "x-memory-archive-plan-signature": signed(applyPayload) },
      }, env);
      expect(repeated.status).toBe(200);
      expect(await repeated.json()).toMatchObject({ applied_count: 0, already_applied_count: 1 });
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_memory_archive_items").get()).toMatchObject({ count: 1 });
      expect(database.database.prepare("SELECT status FROM cf_memory_archive_applies").get()).toMatchObject({ status: "applied" });
    } finally {
      database.close();
    }
  });

  it("rechecks account generation before apply", async () => {
    const { app, database, env } = environment();
    try {
      const plan = fixture();
      const body = JSON.stringify({ manifest_sha256: plan.manifest, source: plan.source, entries: [plan.entry] });
      const headers = {
        "content-type": "application/json",
        "secret-key": "archive-admin",
        "x-memory-archive-plan-signature": plan.reviewSignature,
      };
      const reviewed = await app.request("/internal/memory-archive/reviews", { method: "POST", headers, body }, env);
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      database.database.exec("UPDATE cf_account_cutover SET account_generation = 3 WHERE uid = 'archive-user';");
      const applyPayload = stableJson({
        review_id: reviewId,
        manifest_sha256: plan.manifest,
        entries: [{
          uid: plan.entry.uid,
          memory_id: plan.entry.memory_id,
          source_fingerprint: plan.entry.source_fingerprint,
          source_row_sha256: plan.entry.source_row_sha256,
          import_id: plan.entry.import_id,
          plan_hash: plan.entry.plan_hash,
          account_generation: plan.entry.account_generation,
        }],
      });
      const applied = await app.request("/internal/memory-archive/reviews/" + reviewId + "/apply", {
        method: "POST",
        headers: { "secret-key": "archive-admin", "x-memory-archive-plan-signature": signed(applyPayload) },
      }, env);
      expect(applied.status).toBe(409);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_memory_archive_items").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });
});
