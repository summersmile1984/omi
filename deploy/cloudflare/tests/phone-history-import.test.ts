import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import {
  planPhoneHistory,
  renderPhoneHistoryLedgerSql,
} from "../scripts/phone-history-reconcile.mjs";
import type { JobsEnv } from "../workers/jobs/env";
import { registerPhoneHistoryImportRoutes } from "../workers/jobs/phone-history-import";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
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
      __sql: sql,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ?? null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async () => {
        const result = this.database.prepare(sql).run(...(args as never[]));
        return { meta: { changes: Number(result.changes) } };
      },
    });
    return build();
  }

  async batch<T = unknown>(statements: Array<ReturnType<SqliteD1["prepare"]>>) {
    const results: Array<{ success: boolean; results?: T[]; meta?: { changes: number } }> = [];
    this.database.exec("BEGIN");
    try {
      for (const statement of statements) {
        // The test adapter intentionally uses the public D1-shaped methods;
        // each statement is bound before it reaches this batch.
        const sql = statement.__sql;
        if (/^\s*SELECT\b/i.test(sql)) {
          const value = await statement.all<T>();
          results.push({ success: true, results: value.results });
        } else {
          const run = await statement.run();
          results.push({ success: true, meta: run.meta });
        }
      }
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

const exportSha256 = "b".repeat(64);
const hash = "a".repeat(64);
const ciphertext = `${btoa(String.fromCharCode(...new Uint8Array(12)))}.${btoa(String.fromCharCode(...new Uint8Array(32)))}`
  .replaceAll("+", "-")
  .replaceAll("/", "_")
  .replaceAll("=", "");

const source = {
  kind: "firestore",
  collection: "users/{uid}/phone_numbers",
  ciphertext_scheme: "cloudflare-phone-aes-gcm-v1",
  proof_scheme: "sha256-v1",
  export_sha256: exportSha256,
};

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, nested]) => `${JSON.stringify(key)}:${stableJson(nested)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function baseManifest() {
  // The planner test fixture already owns the exact source fingerprint/proof
  // contract. Constructing it through the public planner keeps this test
  // focused on the Worker apply boundary.
  const row: Record<string, unknown> = {
    uid: "phone-user",
    source_record_id: "phone-1",
    phone_number_id: "phone-1",
    phone_number_hash: hash,
    phone_number_ciphertext: ciphertext,
    twilio_sid: "PN1234567890",
    friendly_name: "home",
    verified_at: 1_700_000_000,
    is_primary: true,
    account_generation: 3,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_001,
    status: "verified",
  };
  const sourceFingerprint = sha256(stableJson({
    collection: source.collection,
    export_sha256: source.export_sha256,
    uid: row.uid,
    source_record_id: row.source_record_id,
    phone_number_hash: row.phone_number_hash,
    twilio_sid: row.twilio_sid,
    friendly_name: row.friendly_name,
    verified_at: row.verified_at,
    is_primary: 1,
    account_generation: row.account_generation,
    created_at: row.created_at,
    updated_at: row.updated_at,
  }));
  row.source_fingerprint = sourceFingerprint;
  row.proof = {
    kind: "verified-e164",
    method: "twilio-outgoing-caller-id",
    canonicalization: "E.164",
    verified: true,
    value_sha256: row.phone_number_hash,
    source_fingerprint: sourceFingerprint,
    proof_sha256: sha256(stableJson({
      kind: "verified-e164",
      method: "twilio-outgoing-caller-id",
      canonicalization: "E.164",
      verified: true,
      value_sha256: row.phone_number_hash,
      source_fingerprint: sourceFingerprint,
      attested_at: 1_700_000_002,
    })),
    attested_at: 1_700_000_002,
  };
  // Source fingerprint and proof are deterministic but intentionally opaque
  // to this test. The planner's public output is the reviewed artifact.
  const plannerModule = { schema_version: 1, source, rows: [row] };
  return planPhoneHistory(plannerModule);
}

function environment() {
  const database = new SqliteD1();
  database.database.exec(
    "INSERT INTO cf_account_cutover (uid, state, checkpoint_phase, destination_backend_bound, account_generation, updated_at) " +
      "VALUES ('phone-user', 'new', 'completed', 1, 3, 1700000000);",
  );
  const env = {
    APP_DB: database,
    ADMIN_KEY: "history-admin",
    PHONE_HISTORY_IMPORT_STAGING_ENABLED: "true",
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerPhoneHistoryImportRoutes(app);
  return { app, database, env };
}

async function stagePlan(database: SqliteD1) {
  const plan = baseManifest();
  database.database.exec(renderPhoneHistoryLedgerSql(plan, 1_700_000_010));
  return plan;
}

function reviewRequest(plan: ReturnType<typeof baseManifest>) {
  const entry = plan.entries[0];
  return new Request("https://jobs.test/internal/phone-history/reviews", {
    method: "POST",
    headers: { "secret-key": "history-admin", "content-type": "application/json" },
    body: JSON.stringify({
      manifest_sha256: plan.manifest_sha256,
      entries: [{ uid: entry.uid, import_id: entry.importId, plan_hash: entry.planHash }],
    }),
  });
}

describe("Cloudflare historical phone import executor", () => {
  it("requires the disabled gate and a reviewed ledger before applying", async () => {
    const { app, database, env } = environment();
    try {
      env.PHONE_HISTORY_IMPORT_STAGING_ENABLED = "false";
      const disabled = await app.request("/internal/phone-history/reviews", {
        method: "POST",
        headers: { "secret-key": "history-admin" },
        body: "{}",
      }, env);
      expect(disabled.status).toBe(503);

      env.PHONE_HISTORY_IMPORT_STAGING_ENABLED = "true";
      const unauthorized = await app.request("/internal/phone-history/reviews", {
        method: "POST",
        body: "{}",
      }, env);
      expect(unauthorized.status).toBe(403);

      const missing = await app.request(
        "/internal/phone-history/reviews/00000000-0000-4000-8000-000000000000/apply",
        { method: "POST", headers: { "secret-key": "history-admin" } },
        env,
      );
      expect(missing.status).toBe(404);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_phone_numbers").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });

  it("applies an approved encrypted ledger row idempotently without provider calls", async () => {
    const { app, database, env } = environment();
    try {
      const plan = await stagePlan(database);
      const reviewed = await app.request(reviewRequest(plan), {}, env);
      expect(reviewed.status).toBe(201);
      const reviewBody = (await reviewed.json()) as { review_id: string; entry_count: number };
      expect(reviewBody).toMatchObject({ entry_count: 1 });

      const applied = await app.request(
        `/internal/phone-history/reviews/${reviewBody.review_id}/apply`,
        { method: "POST", headers: { "secret-key": "history-admin" } },
        env,
      );
      expect(applied.status).toBe(200);
      expect(await applied.json()).toMatchObject({ status: "applied", applied_count: 1, already_applied_count: 0 });
      expect(database.database.prepare("SELECT uid, phone_number_hash, phone_number_ciphertext, account_generation FROM cf_phone_numbers").get()).toMatchObject({
        uid: "phone-user",
        phone_number_hash: hash,
        phone_number_ciphertext: ciphertext,
        account_generation: 3,
      });
      expect(database.database.prepare("SELECT status FROM cf_phone_number_import_applies").get()).toMatchObject({ status: "applied" });

      const repeated = await app.request(
        `/internal/phone-history/reviews/${reviewBody.review_id}/apply`,
        { method: "POST", headers: { "secret-key": "history-admin" } },
        env,
      );
      expect(repeated.status).toBe(200);
      expect(await repeated.json()).toMatchObject({ status: "applied", applied_count: 0, already_applied_count: 1 });
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_phone_numbers").get()).toMatchObject({ count: 1 });
    } finally {
      database.close();
    }
  });

  it("rechecks generation and deletion fences before the atomic promotion", async () => {
    const { app, database, env } = environment();
    try {
      const plan = await stagePlan(database);
      const reviewed = await app.request(reviewRequest(plan), {}, env);
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      database.database.exec("UPDATE cf_account_cutover SET account_generation = 4 WHERE uid = 'phone-user'");
      const generationDrift = await app.request(
        `/internal/phone-history/reviews/${reviewId}/apply`,
        { method: "POST", headers: { "secret-key": "history-admin" } },
        env,
      );
      expect(generationDrift.status).toBe(409);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_phone_numbers").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }

    const fenced = environment();
    try {
      const plan = await stagePlan(fenced.database);
      const reviewed = await fenced.app.request(reviewRequest(plan), {}, fenced.env);
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      fenced.database.database.exec(
        "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) " +
          "VALUES ('phone-user', 'delete-phone', 'pending', 'quiescing', 1, 1, 1)",
      );
      const deletionFence = await fenced.app.request(
        `/internal/phone-history/reviews/${reviewId}/apply`,
        { method: "POST", headers: { "secret-key": "history-admin" } },
        fenced.env,
      );
      expect(deletionFence.status).toBe(409);
      expect(fenced.database.database.prepare("SELECT COUNT(*) AS count FROM cf_phone_numbers").get()).toMatchObject({ count: 0 });
    } finally {
      fenced.database.close();
    }
  });

  it("rejects a changed ledger and never returns phone plaintext", async () => {
    const { app, database, env } = environment();
    try {
      const plan = await stagePlan(database);
      database.database.exec(
        "UPDATE cf_phone_number_import_ledger SET friendly_name = '+15551234567' WHERE uid = 'phone-user'",
      );
      const response = await app.request(reviewRequest(plan), {}, env);
      expect(response.status).toBe(409);
      expect(await response.text()).not.toContain("+15551234567");
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_phone_number_import_review_batches").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });
});
