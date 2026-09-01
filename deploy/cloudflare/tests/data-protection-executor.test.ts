import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import type { Message } from "@cloudflare/workers-types";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import {
  dataProtectionExecutorConstants,
  processDataProtectionMigrationMessage,
  registerDataProtectionMigrationRoutes,
} from "../workers/jobs/data-protection-executor";

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

  close() {
    this.database.close();
  }
}

function environment(enabled = true, secret = "data-protection-secret-012345678901234567890123") {
  const database = new SqliteD1();
  database.database.exec(
    "INSERT INTO cf_account_cutover (uid, state, checkpoint_phase, destination_backend_bound, account_generation, updated_at) " +
      "VALUES ('protection-user', 'new', 'completed', 1, 7, 1700000000)",
  );
  const sent: JobMessage[] = [];
  const env = {
    APP_DB: database,
    ADMIN_KEY: "protection-admin",
    DATA_PROTECTION_EXECUTOR_STAGING_ENABLED: enabled ? "true" : "false",
    DATA_PROTECTION_ENCRYPTION_SECRET: secret,
    JOBS: { send: async (message: JobMessage) => sent.push(message) },
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerDataProtectionMigrationRoutes(app);
  return { app, database, env, sent };
}

function headers() {
  return {
    "content-type": "application/json",
    "secret-key": "protection-admin",
  };
}

function requestBody(items: Array<{ type: string; id: string }>) {
  return {
    uid: "protection-user",
    account_generation: 7,
    operation: items.length === 1 ? "single" : "batch",
    target_level: "enhanced",
    items,
  };
}

function addSources(database: SqliteD1) {
  database.database.exec(
    "INSERT INTO cf_memories (uid, id, content, evidence_json, memory_tier, valid_at, created_at, updated_at) " +
      "VALUES ('protection-user', 'memory-1', 'memory plaintext', '[{\"source_id\":\"conversation-1\"}]', 'long_term', 1, 1, 1);" +
      "INSERT INTO cf_conversations (uid, id, created_at, structured_json, transcript_segments_json, photos_json) " +
      "VALUES ('protection-user', 'conversation-1', 1, '{\"title\":\"plain\"}', '[{\"text\":\"transcript plaintext\"}]', '[{\"base64\":\"photo plaintext\"}]');" +
      "INSERT INTO cf_chat_messages (uid, id, created_at, message_json) " +
      "VALUES ('protection-user', 'chat-1', 1, '{\"id\":\"chat-1\",\"text\":\"chat plaintext\",\"sender\":\"human\"}')",
  );
}

function queueMessage(jobId: string): Message<JobMessage> {
  let acknowledged = false;
  let retried = false;
  return {
    body: {
      jobId,
      uid: "protection-user",
      kind: "data_protection_migration",
      payload: { runId: jobId },
    },
    attempts: 1,
    ack() {
      acknowledged = true;
    },
    retry() {
      retried = true;
    },
    get acknowledged() {
      return acknowledged;
    },
    get retried() {
      return retried;
    },
  } as unknown as Message<JobMessage>;
}

async function decrypt(secret: string, uid: string, encoded: string): Promise<string> {
  const bytes = Uint8Array.from(atob(encoded), (value) => value.charCodeAt(0));
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: new TextEncoder().encode(uid),
      info: new TextEncoder().encode("user-data-encryption"),
    },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"],
  );
  const clear = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: bytes.slice(0, 12) },
    key,
    bytes.slice(12),
  );
  return new TextDecoder().decode(clear);
}

describe("Cloudflare data-protection encrypted preparation executor", () => {
  it("is gated, requires the encryption key, and queues an idempotent plan", async () => {
    const disabled = environment(false);
    try {
      const response = await disabled.app.request(
        dataProtectionExecutorConstants.routePath,
        { method: "POST", headers: headers(), body: JSON.stringify(requestBody([{ type: "memory", id: "memory-1" }])) },
        disabled.env,
      );
      expect(response.status).toBe(503);
    } finally {
      disabled.database.close();
    }

    const { app, database, env, sent } = environment(true, "too-short");
    try {
      const response = await app.request(
        dataProtectionExecutorConstants.routePath,
        { method: "POST", headers: headers(), body: JSON.stringify(requestBody([{ type: "memory", id: "memory-1" }])) },
        env,
      );
      expect(response.status).toBe(503);
      expect(sent).toHaveLength(0);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_data_protection_migration_runs").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });

  it("prepares all three legacy field shapes with Python-compatible HKDF/AES-GCM and preserves source rows", async () => {
    const { app, database, env, sent } = environment();
    addSources(database);
    try {
      const body = requestBody([
        { type: "memory", id: "memory-1" },
        { type: "conversation", id: "conversation-1" },
        { type: "chat", id: "chat-1" },
      ]);
      const first = await app.request(
        dataProtectionExecutorConstants.routePath,
        { method: "POST", headers: headers(), body: JSON.stringify(body) },
        env,
      );
      expect(first.status).toBe(202);
      const firstBody = (await first.json()) as { run_id: string; status: string };
      expect(firstBody.status).toBe("queued");
      const duplicate = await app.request(
        dataProtectionExecutorConstants.routePath,
        { method: "POST", headers: headers(), body: JSON.stringify(body) },
        env,
      );
      expect(duplicate.status).toBe(200);
      expect(((await duplicate.json()) as { run_id: string }).run_id).toBe(firstBody.run_id);
      expect(sent).toHaveLength(1);

      const message = queueMessage(firstBody.run_id);
      await processDataProtectionMigrationMessage(message, env);
      expect((message as unknown as { acknowledged: boolean }).acknowledged).toBe(true);

      const status = await app.request(
        `${dataProtectionExecutorConstants.routePath}/${firstBody.run_id}?uid=protection-user`,
        { method: "GET", headers: headers() },
        env,
      );
      expect(status.status).toBe(200);
      expect(await status.json()).toMatchObject({ status: "completed", phase: "prepared", prepared_count: 3 });
      const resultRow = database.database.prepare("SELECT result_json FROM cf_data_protection_migration_runs WHERE run_id = ?").get(firstBody.run_id) as { result_json: string };
      const result = JSON.parse(String(resultRow.result_json)) as { envelope_scheme: string; items: Array<{ type: string; fields: Record<string, string> }> };
      expect(result.envelope_scheme).toBe(dataProtectionExecutorConstants.envelopeScheme);
      const byType = new Map(result.items.map((item) => [item.type, item.fields]));
      expect(await decrypt(env.DATA_PROTECTION_ENCRYPTION_SECRET!, "protection-user", byType.get("memory")!.content)).toBe("memory plaintext");
      expect(await decrypt(env.DATA_PROTECTION_ENCRYPTION_SECRET!, "protection-user", byType.get("memory")!.evidence_json)).toBe('[{"source_id":"conversation-1"}]');
      expect(await decrypt(env.DATA_PROTECTION_ENCRYPTION_SECRET!, "protection-user", byType.get("conversation")!.transcript_segments_json)).toBe('[{"text":"transcript plaintext"}]');
      const photos = JSON.parse(byType.get("conversation")!.photos_json) as Array<{ base64: string }>;
      expect(await decrypt(env.DATA_PROTECTION_ENCRYPTION_SECRET!, "protection-user", photos[0].base64)).toBe("photo plaintext");
      const chat = JSON.parse(byType.get("chat")!.message_json) as { text: string };
      expect(await decrypt(env.DATA_PROTECTION_ENCRYPTION_SECRET!, "protection-user", chat.text)).toBe("chat plaintext");
      expect((database.database.prepare("SELECT content FROM cf_memories WHERE uid = 'protection-user' AND id = 'memory-1'").get() as { content: string }).content).toBe("memory plaintext");
      expect((database.database.prepare("SELECT data_protection_level FROM cf_conversations WHERE uid = 'protection-user' AND id = 'conversation-1'").get() as { data_protection_level: string }).data_protection_level).toBe("standard");
      expect((database.database.prepare("SELECT message_json FROM cf_chat_messages WHERE uid = 'protection-user' AND id = 'chat-1'").get() as { message_json: string }).message_json).toContain("chat plaintext");
    } finally {
      database.close();
    }
  });

  it("fails closed on source revision drift and deletion fences", async () => {
    const { app, database, env, sent } = environment();
    addSources(database);
    try {
      const response = await app.request(
        dataProtectionExecutorConstants.routePath,
        { method: "POST", headers: headers(), body: JSON.stringify(requestBody([{ type: "memory", id: "memory-1" }])) },
        env,
      );
      const runId = (await response.json() as { run_id: string }).run_id;
      database.database.prepare("UPDATE cf_memories SET content = 'changed' WHERE uid = 'protection-user' AND id = 'memory-1'").run();
      const message = queueMessage(runId);
      await processDataProtectionMigrationMessage(message, env);
      expect(database.database.prepare("SELECT status, last_error FROM cf_data_protection_migration_runs WHERE run_id = ?").get(runId)).toMatchObject({ status: "failed", last_error: "source changed" });
      expect(sent).toHaveLength(1);

      database.database.prepare("INSERT INTO cf_account_cutover (uid, state, checkpoint_phase, destination_backend_bound, account_generation, updated_at) VALUES ('fenced-user', 'new', 'completed', 1, 0, 1)").run();
      database.database.prepare("INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES ('fenced-user', 'delete-1', 'pending', 'quiescing', 1, 1, 1)").run();
      const fencedBody = { ...requestBody([{ type: "memory", id: "memory-1" }]), uid: "fenced-user", account_generation: 0 };
      const fenced = await app.request(
        dataProtectionExecutorConstants.routePath,
        { method: "POST", headers: headers(), body: JSON.stringify(fencedBody) },
        env,
      );
      expect(fenced.status).toBe(409);
      expect(database.database.prepare("SELECT COUNT(*) AS count FROM cf_data_protection_migration_runs WHERE uid = 'fenced-user'").get()).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });
});
