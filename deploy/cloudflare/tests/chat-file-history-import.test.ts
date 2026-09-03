import { DatabaseSync } from "node:sqlite";
import { createHash, createHmac } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import {
  chatFileHistoryImportConstants,
  registerChatFileHistoryImportRoutes,
} from "../workers/jobs/chat-file-history-import";

const PLAN_SECRET = "chat-file-plan-secret-that-is-long-enough";
const PROVIDER_SECRET = "chat-file-provider-secret-that-is-long-enough";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    const directory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../migrations/app");
    for (const filename of readdirSync(directory).filter((value) => value.endsWith(".sql")).sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const make = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => make(values),
      first: async <T>() => (this.database.prepare(sql).get(...args as never[]) as T | undefined) ?? null,
      all: async <T>() => ({ results: this.database.prepare(sql).all(...args as never[]) as T[] }),
      run: async () => ({ meta: { changes: Number(this.database.prepare(sql).run(...args as never[]).changes) } }),
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

function signed(secret: string, value: string): string {
  return createHmac("sha256", secret).update(value).digest("base64url");
}

function fixture() {
  const uid = "history-file-user";
  const sourceFileId = "legacy-file-1";
  const sourceObjectUri = "gs://omi-legacy/files/legacy-file-1";
  const checksum = "a".repeat(64);
  const providerFileId = "file-history-1";
  const name = "notes.txt";
  const mimeType = "text/plain";
  const size = 12;
  const storageKey = `${uid}/${sourceFileId}`;
  const requestFingerprint = sha256(`${uid}\0${name}\0${mimeType}\0${checksum}`);
  const importId = sha256(`${uid}\0${sourceFileId}\0${checksum}`);
  const planHash = sha256(JSON.stringify({
    uid,
    sourceFileId,
    sourceObjectUri,
    checksum,
    providerFileId,
    name,
    mimeType,
    size,
    storageKey,
    action: "stage",
    errors: [],
  }));
  const entry = {
    uid,
    importId,
    sourceFileId,
    sourceObjectUri,
    sourceGeneration: "gcs-generation-1",
    checksum,
    providerFileId,
    name,
    mimeType,
    size,
    storageKey,
    requestFingerprint,
    createdAt: 1700000000,
    updatedAt: 1700000000,
    action: "stage",
    status: "planned",
    lastError: null,
    planHash,
    accountGeneration: 4,
  };
  const manifest = sha256(stableJson({
    schema_version: 1,
    entries: [{ uid, import_id: importId, plan_hash: planHash, account_generation: 4 }],
  }));
  const plan = { manifest_sha256: manifest, entries: [entry] };
  const reviewPayload = chatFileHistoryImportConstants.reviewSignaturePayload({
    manifest_sha256: manifest,
    entries: [{
      uid,
      import_id: importId,
      file_id: sourceFileId,
      source_file_id: sourceFileId,
      source_object_uri: sourceObjectUri,
      source_generation: "gcs-generation-1",
      checksum_sha256: checksum,
      provider_file_id: providerFileId,
      name,
      mime_type: mimeType,
      size,
      storage_key: storageKey,
      request_fingerprint: requestFingerprint,
      plan_hash: planHash,
      account_generation: 4,
      created_at: 1700000000,
      updated_at: 1700000000,
    }],
  });
  return { plan, entry, reviewSignature: signed(PLAN_SECRET, reviewPayload) };
}

function environment() {
  const database = new SqliteD1();
  database.database.exec(
    "INSERT INTO cf_account_cutover (uid, state, checkpoint_phase, destination_backend_bound, account_generation, updated_at) VALUES ('history-file-user', 'new', 'completed', 1, 4, 1700000000);",
  );
  const env = {
    APP_DB: database,
    ADMIN_KEY: "history-admin",
    CHAT_FILE_HISTORY_IMPORT_STAGING_ENABLED: "true",
    CHAT_FILE_HISTORY_IMPORT_SIGNING_SECRET: PLAN_SECRET,
    CHAT_FILE_HISTORY_PROVIDER_ATTESTATION_SECRET: PROVIDER_SECRET,
    CHAT_FILES: {
      head: async (key: string) => key === "history-file-user/legacy-file-1"
        ? { size: 12, customMetadata: { checksum: "a".repeat(64) } }
        : null,
    },
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerChatFileHistoryImportRoutes(app);
  return { database, env, app };
}

async function reviewPlan(app: Hono<{ Bindings: JobsEnv }>, env: JobsEnv, plan: ReturnType<typeof fixture>["plan"], signature: string) {
  return app.request("/internal/chat-file-history/reviews", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "secret-key": "history-admin",
      "x-chat-file-plan-signature": signature,
    },
    body: JSON.stringify(plan),
  }, env);
}

describe("Cloudflare reviewed historical chat-file promotion", () => {
  it("fails closed before checking the plan or provider", async () => {
    const { app, env, database } = environment();
    try {
      env.CHAT_FILE_HISTORY_IMPORT_STAGING_ENABLED = "false";
      expect((await app.request("/internal/chat-file-history/reviews", { method: "POST" }, env)).status).toBe(503);
      env.CHAT_FILE_HISTORY_IMPORT_STAGING_ENABLED = "true";
      expect((await app.request("/internal/chat-file-history/reviews", { method: "POST" }, env)).status).toBe(403);
    } finally {
      database.close();
    }
  });

  it("requires R2 and provider attestations, then applies idempotently", async () => {
    const { app, env, database } = environment();
    try {
      const fixtureData = fixture();
      const reviewed = await reviewPlan(app, env, fixtureData.plan, fixtureData.reviewSignature);
      expect(reviewed.status).toBe(201);
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      const invalidApply = { review_id: reviewId, manifest_sha256: fixtureData.plan.manifest_sha256, attestations: [{ import_id: fixtureData.entry.importId, signature: signed(PROVIDER_SECRET, "wrong") }] };
      expect((await app.request(`/internal/chat-file-history/reviews/${reviewId}/apply`, {
        method: "POST",
        headers: { "content-type": "application/json", "secret-key": "history-admin", "x-chat-file-plan-signature": signed(PLAN_SECRET, stableJson(invalidApply)) },
        body: JSON.stringify(invalidApply),
      }, env)).status).toBe(409);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_chat_files").get()).toMatchObject({ count: 0 });

      const item = {
        uid: fixtureData.entry.uid,
        file_id: fixtureData.entry.sourceFileId,
        storage_key: fixtureData.entry.storageKey,
        checksum_sha256: fixtureData.entry.checksum,
        size: fixtureData.entry.size,
        provider_file_id: fixtureData.entry.providerFileId,
        account_generation: fixtureData.entry.accountGeneration,
        plan_hash: fixtureData.entry.planHash,
      };
      const attestation = signed(PROVIDER_SECRET, chatFileHistoryImportConstants.providerAttestationPayload(item));
      const applyBody = { review_id: reviewId, manifest_sha256: fixtureData.plan.manifest_sha256, attestations: [{ import_id: fixtureData.entry.importId, signature: attestation }] };
      const headers = { "content-type": "application/json", "secret-key": "history-admin", "x-chat-file-plan-signature": signed(PLAN_SECRET, stableJson(applyBody)) };
      const applied = await app.request(`/internal/chat-file-history/reviews/${reviewId}/apply`, { method: "POST", headers, body: JSON.stringify(applyBody) }, env);
      expect(applied.status).toBe(200);
      expect(await applied.json()).toMatchObject({ applied_count: 1, already_applied_count: 0 });
      expect(database.database.prepare("SELECT status, provider_file_id FROM cf_chat_files").get()).toMatchObject({ status: "ready", provider_file_id: "file-history-1" });
      const repeated = await app.request(`/internal/chat-file-history/reviews/${reviewId}/apply`, { method: "POST", headers, body: JSON.stringify(applyBody) }, env);
      expect(repeated.status).toBe(200);
      expect(await repeated.json()).toMatchObject({ applied_count: 0, already_applied_count: 1 });
    } finally {
      database.close();
    }
  });

  it("does not promote when the deletion fence appears after review", async () => {
    const { app, env, database } = environment();
    try {
      const fixtureData = fixture();
      const reviewed = await reviewPlan(app, env, fixtureData.plan, fixtureData.reviewSignature);
      const reviewId = ((await reviewed.json()) as { review_id: string }).review_id;
      database.database.exec("INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES ('history-file-user', 'history-file-delete', 'pending', 'quiescing', 1700000000, 1700000000, 1700000000);");
      const applyBody = { review_id: reviewId, manifest_sha256: fixtureData.plan.manifest_sha256, attestations: [{ import_id: fixtureData.entry.importId, signature: signed(PROVIDER_SECRET, "wrong") }] };
      const response = await app.request(`/internal/chat-file-history/reviews/${reviewId}/apply`, { method: "POST", headers: { "secret-key": "history-admin", "x-chat-file-plan-signature": signed(PLAN_SECRET, stableJson(applyBody)) }, body: JSON.stringify(applyBody) }, env);
      expect(response.status).toBe(409);
      expect(await response.json()).toMatchObject({ error: "chat_file_history_authority_changed" });
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_chat_files").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });
});
