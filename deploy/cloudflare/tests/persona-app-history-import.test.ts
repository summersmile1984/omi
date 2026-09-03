import { createHash, createHmac } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import {
  personaAppHistoryImportConstants,
  registerPersonaAppHistoryImportRoutes,
} from "../workers/jobs/persona-app-history-import";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const directory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../migrations/app");
    for (const filename of readdirSync(directory).filter((name) => name.endsWith(".sql")).sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const make = (args: unknown[] = []) => ({
      __sql: sql,
      __args: args,
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
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = [];
      for (const statement of statements) {
        const result = this.database.prepare(statement.__sql).run(...(statement.__args as never[]));
        results.push({ meta: { changes: Number(result.changes) } });
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

const ADMIN_KEY = "persona-app-history-admin";
const SIGNING_SECRET = "persona-app-history-signing-secret-long-enough";
const SOURCE_UID_HASH = "b".repeat(64);
const SOURCE_REF = `fb-anon-${SOURCE_UID_HASH}`;
const SOURCE_REVISION = "c".repeat(64);
const SOURCE_EXPORT = "e".repeat(64);
const SOURCE_FINGERPRINT = "f".repeat(64);
const UID = "target-user";
const APP_ID = "persona-1";

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function signature(payload: string): string {
  return createHmac("sha256", SIGNING_SECRET).update(payload).digest("base64url");
}

function fixture() {
  const source = { kind: "firestore", collection: "plugins_data", export_sha256: SOURCE_EXPORT };
  const publicMetadataJson = stableJson({
    capabilities: ["persona"],
    description: "Reviewed public persona metadata",
    id: APP_ID,
    name: "Historical Persona",
  });
  const base = {
    sourceRef: SOURCE_REF,
    sourceUidHash: SOURCE_UID_HASH,
    uid: UID,
    appId: APP_ID,
    sourceProjectionRevision: SOURCE_REVISION,
    targetAccountGeneration: 7,
    sourceFingerprint: SOURCE_FINGERPRINT,
    sourceExportSha256: SOURCE_EXPORT,
    publicMetadataJson,
    privateEnvelope: null,
    imageObject: null,
    createdAt: 1700000000,
    updatedAt: 1700000000,
  };
  const sourceRowSha256 = sha256(stableJson(base));
  const requestFingerprint = sha256(`persona-app-history\0${SOURCE_REF}\0${UID}\0${APP_ID}\0${sourceRowSha256}`);
  const entry = {
    ...base,
    requestFingerprint,
    idempotencyKey: `persona-app-history-${requestFingerprint.slice(0, 40)}`,
    sourceRowSha256,
    action: "stage",
    status: "planned",
    lastError: null,
  };
  const manifest_sha256 = sha256(stableJson({ schema_version: 1, source, rows: [sourceRowSha256] }));
  return {
    mode: "dry-run",
    schema_version: 1,
    source,
    total: 1,
    stage: 1,
    blocked: 0,
    entries: [entry],
    manifest_sha256,
  };
}

function environment() {
  const database = new SqliteD1();
  database.database.exec(
    "INSERT INTO cf_account_cutover (uid, state, account_generation, checkpoint_phase, destination_backend_bound, updated_at) VALUES ('target-user', 'new', 7, 'completed', 1, 1000);" +
      "INSERT INTO cf_app_owner_migration_sources (source_uid, source_uid_hash, source_provider, source_proof_hash, source_projection_revision, projection_status, app_projection_count, memory_projection_count, data_projection_status, data_projection_revision, memory_reencryption_status, memory_reencryption_revision, target_uid, target_account_generation, source_credential_generation, attestation_expires_at, imported_at, updated_at) VALUES ('" + SOURCE_REF + "', '" + SOURCE_UID_HASH + "', 'firebase-anonymous', '" + "a".repeat(64) + "', '" + SOURCE_REVISION + "', 'imported', 1, 0, 'verified', '" + "d".repeat(64) + "', 'not_required', NULL, 'target-user', 7, 100, 4000000000, 1000, 1000);",
  );
  const env = {
    APP_DB: database,
    APPS_ADMIN_KEY: ADMIN_KEY,
    PERSONA_APP_HISTORY_IMPORT_STAGING_ENABLED: "true",
    PERSONA_APP_HISTORY_IMPORT_SIGNING_SECRET: SIGNING_SECRET,
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerPersonaAppHistoryImportRoutes(app);
  return { app, database, env };
}

function request(pathname: string, body: unknown, planSignature: string | undefined) {
  const headers: Record<string, string> = {
    "content-type": "application/json",
    "secret-key": ADMIN_KEY,
  };
  if (planSignature) headers["x-persona-app-plan-signature"] = planSignature;
  return new Request(`https://jobs.test${pathname}`, { method: "POST", headers, body: JSON.stringify(body) });
}

describe("Cloudflare reviewed Persona/App history projection", () => {
  it("fails closed until the explicit staging gate and admin key are present", async () => {
    const { app, database, env } = environment();
    try {
      env.PERSONA_APP_HISTORY_IMPORT_STAGING_ENABLED = "false";
      expect((await app.fetch(request(personaAppHistoryImportConstants.reviewPath, {}, ""), env)).status).toBe(503);
      env.PERSONA_APP_HISTORY_IMPORT_STAGING_ENABLED = "true";
      expect((await app.fetch(new Request("https://jobs.test" + personaAppHistoryImportConstants.reviewPath, { method: "POST" }), env)).status).toBe(403);
    } finally {
      database.close();
    }
  });

  it("reviews and applies only signed public metadata idempotently", async () => {
    const { app, database, env } = environment();
    try {
      const plan = fixture();
      const reviewSignature = signature(personaAppHistoryImportConstants.reviewSignaturePayload(plan as never));
      const reviewed = await app.fetch(request(personaAppHistoryImportConstants.reviewPath, plan, reviewSignature), env);
      expect(reviewed.status).toBe(201);
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      const repeatedReview = await app.fetch(request(personaAppHistoryImportConstants.reviewPath, plan, reviewSignature), env);
      expect(repeatedReview.status).toBe(200);
      expect(((await repeatedReview.json()) as { review_id: string }).review_id).toBe(reviewId);
      const applyBody = { review_id: reviewId, ...plan };
      const applied = await app.fetch(
        request(`${personaAppHistoryImportConstants.reviewPath}/${reviewId}/apply`, applyBody, signature(personaAppHistoryImportConstants.applySignaturePayload(reviewId, plan as never))),
        env,
      );
      expect(applied.status).toBe(200);
      expect(await applied.json()).toMatchObject({ applied_count: 1, already_applied_count: 0 });
      expect(database.database.prepare("SELECT owner_uid, owner_account_generation, approved, status, data_json FROM cf_app_catalog WHERE id = ?").get(APP_ID)).toMatchObject({
        owner_uid: UID,
        owner_account_generation: 7,
        approved: 0,
        status: "historical_import",
        data_json: plan.entries[0].publicMetadataJson,
      });
      const repeatedApply = await app.fetch(
        request(`${personaAppHistoryImportConstants.reviewPath}/${reviewId}/apply`, applyBody, signature(personaAppHistoryImportConstants.applySignaturePayload(reviewId, plan as never))),
        env,
      );
      expect(repeatedApply.status).toBe(200);
      expect(await repeatedApply.json()).toMatchObject({ applied_count: 0, already_applied_count: 1 });
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_persona_app_history_applies").get()).toMatchObject({ count: 1 });
    } finally {
      database.close();
    }
  });

  it("rejects private/image projections before creating review state", async () => {
    const { app, database, env } = environment();
    try {
      const plan = fixture();
      plan.entries[0].privateEnvelope = { encrypted: true } as never;
      const response = await app.fetch(request(personaAppHistoryImportConstants.reviewPath, plan, ""), env);
      expect(response.status).toBe(422);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_persona_app_history_review_batches").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });

  it("rechecks the deletion fence before apply", async () => {
    const { app, database, env } = environment();
    try {
      const plan = fixture();
      const reviewed = await app.fetch(request(personaAppHistoryImportConstants.reviewPath, plan, signature(personaAppHistoryImportConstants.reviewSignaturePayload(plan as never))), env);
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      database.database.prepare("INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES (?, ?, 'pending', 'quiescing', ?, ?, ?)").run(UID, "delete-persona-user", 1700000000, 1700000000, 1700000000);
      const applyBody = { review_id: reviewId, ...plan };
      const applied = await app.fetch(
        request(`${personaAppHistoryImportConstants.reviewPath}/${reviewId}/apply`, applyBody, signature(personaAppHistoryImportConstants.applySignaturePayload(reviewId, plan as never))),
        env,
      );
      expect(applied.status).toBe(409);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_app_catalog").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });
});
