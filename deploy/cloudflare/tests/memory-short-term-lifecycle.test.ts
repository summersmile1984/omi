import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import jobs from "../workers/jobs/index";
import { processMemoryShortTermLifecycleMessage } from "../workers/jobs/memory-short-term-lifecycle";

type D1Statement = {
  bind(...values: unknown[]): D1Statement;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<T>;
  run(): Promise<{ meta: { changes: number } }>;
};

function sqliteValue(value: unknown) {
  return value as never;
}

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");
  private failMemoryRead = false;

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter(
        (value) =>
          value.endsWith(".sql") &&
          value !== "0102_audio_merge_jobs.sql" &&
          value !== "0102_hume_webhook_events.sql",
      )
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string): D1Statement {
    const build = (args: unknown[] = []): D1Statement => ({
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as T | undefined) ?? null,
      all: async <T>() => ({
        ...(this.failMemoryRead && sql.includes("FROM cf_memories WHERE")
          ? (() => {
              this.failMemoryRead = false;
              throw new Error("simulated lifecycle memory read failure");
            })()
          : {}),
        results: this.database.prepare(sql).all(...args.map(sqliteValue)),
      }) as T,
      run: async () => ({
        meta: {
          changes: Number(this.database.prepare(sql).run(...args.map(sqliteValue)).changes),
        },
      }),
    });
    return build();
  }

  failNextLifecycleMemoryRead() {
    this.failMemoryRead = true;
  }

  async batch(statements: D1Statement[]) {
    const results = [];
    for (const statement of statements) results.push(await statement.run());
    return results;
  }

  close() {
    this.database.close();
  }
}

function environment() {
  const database = new SqliteD1();
  const sent: JobMessage[] = [];
  const env = {
    APP_DB: database,
    ADMIN_KEY: "lifecycle-admin-secret",
    JOBS: { send: vi.fn(async (message: JobMessage) => sent.push(message)) },
  } as unknown as JobsEnv;
  return { database, env, sent };
}

function seedAuthority(database: SqliteD1, uid = "lifecycle-user", generation = 7) {
  database.database
    .prepare(
      `INSERT INTO cf_account_cutover
         (uid, state, account_generation, ui_generation, api_generation,
          checkpoint_phase, manifest_id, destination_backend_bound, updated_at)
       VALUES (?, 'new', ?, ?, ?, 'completed', 'lifecycle-test-v1', 1, ?)`,
    )
    .run(uid, generation, generation, generation, 1);
  return { uid, generation };
}

function readyControl(database: SqliteD1, uid: string, generation: number) {
  database.database
    .prepare(
      `INSERT INTO cf_memory_short_term_lifecycle_control
         (uid, schema_version, source, enabled, executor_state, account_generation,
          source_revision, updated_at)
       VALUES (?, 1, 'cloudflare_short_term_lifecycle_projection', 1, 'ready', ?, 'projection-v1', ?)`,
    )
    .run(uid, generation, 1);
}

function adminRequest(uid: string, query: string, key = "lifecycle-admin-secret") {
  return new Request(
    `https://jobs.test/memory/admin/users/${uid}/short-term-lifecycle/run?${query}`,
    { method: "POST", headers: { "secret-key": key } },
  );
}

afterEach(() => vi.restoreAllMocks());

describe("Cloudflare short-term lifecycle D1 authority", () => {
  it("bootstraps the lifecycle projection from a completed cutover", async () => {
    const { database, env, sent } = environment();
    try {
      seedAuthority(database);
      const response = await jobs.fetch(
        adminRequest("lifecycle-user", "run_id=run-1"),
        env,
      );

      expect(response.status).toBe(200);
      expect(await response.json()).toMatchObject({
        uid: "lifecycle-user",
        run_id: "run-1",
        evaluated_count: 0,
        created_count: 0,
      });
      expect(sent).toHaveLength(0);
      expect(
        database.database
          .prepare("SELECT COUNT(*) AS count FROM cf_memory_short_term_lifecycle_control")
          .get(),
      ).toMatchObject({ count: 1 });
    } finally {
      database.close();
    }
  });

  it("admits only a generation-bound ready projection and deduplicates run input", async () => {
    const { database, env, sent } = environment();
    try {
      const { uid, generation } = seedAuthority(database);
      readyControl(database, uid, generation);

      const first = await jobs.fetch(
        adminRequest(uid, "run_id=run-1&limit=10&evaluated_at=2026-08-31T00:00:00Z"),
        env,
      );
      const duplicate = await jobs.fetch(
        adminRequest(uid, "run_id=run-1&limit=10&evaluated_at=2026-08-31T00:00:00Z"),
        env,
      );
      const conflict = await jobs.fetch(
        adminRequest(uid, "run_id=run-1&limit=11&evaluated_at=2026-08-31T00:00:00Z"),
        env,
      );

      expect(first.status).toBe(200);
      expect(await first.json()).toMatchObject({
        uid,
        run_id: "run-1",
      });
      expect(duplicate.status).toBe(200);
      expect(await duplicate.json()).toMatchObject({
        uid,
        run_id: "run-1",
        evaluated_count: 0,
      });
      expect(conflict.status).toBe(409);
      expect(sent).toHaveLength(0);
    } finally {
      database.close();
    }
  });

  it("rechecks the deletion fence before leasing and never writes a transition", async () => {
    const { database, env, sent } = environment();
    try {
      const { uid, generation } = seedAuthority(database);
      readyControl(database, uid, generation);
      database.database
        .prepare(
          `INSERT INTO cf_memory_short_term_lifecycle_runs
             (uid, run_id, request_fingerprint, evaluated_at, requested_limit,
              status, attempts, next_attempt_at, account_generation, created_at, updated_at)
           VALUES (?, ?, ?, 100, 10, 'queued', 0, 100, ?, 100, 100)`,
        )
        .run(uid, "run-2", "a".repeat(64), generation);
      database.database
        .prepare(
          `INSERT INTO cf_account_deletion_intents
             (uid, job_id, status, phase, next_attempt_at, created_at, updated_at)
           VALUES (?, 'delete-lifecycle-user', 'pending', 'quiescing', 1, 1, 1)`,
        )
        .run(uid);
      const acknowledgements = { ack: vi.fn(), retry: vi.fn() };
      await processMemoryShortTermLifecycleMessage(
        {
          body: {
            jobId: "memory-stl-fenced",
            uid,
            kind: "memory_short_term_lifecycle",
            payload: {
              runId: "run-2",
              requestFingerprint: "a".repeat(64),
              accountGeneration: generation,
            },
          },
          attempts: 1,
          ack: acknowledgements.ack,
          retry: acknowledgements.retry,
        } as never,
        env,
      );

      const run = database.database
        .prepare(
          "SELECT status, last_error, lease_until FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?",
        )
        .get(uid, "run-2") as { status: string; last_error: string; lease_until: number | null };
      // The mutation fence rejects the lease update itself.  The consumer
      // acknowledges the message so account deletion can purge the queued row.
      expect(run).toMatchObject({
        status: "queued",
        lease_until: null,
      });
      expect(acknowledgements.ack).toHaveBeenCalledOnce();
      expect(acknowledgements.retry).not.toHaveBeenCalled();
      expect(
        database.database
          .prepare("SELECT COUNT(*) AS count FROM cf_memory_short_term_lifecycle_transitions")
          .get(),
      ).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });

  it("completes a ready D1 lifecycle run without reporting a false executor failure", async () => {
    const { database, env, sent } = environment();
    try {
      const { uid, generation } = seedAuthority(database);
      readyControl(database, uid, generation);
      const completed = await jobs.fetch(adminRequest(uid, "run_id=run-3"), env);
      expect(completed.status).toBe(200);
      expect(
        database.database
          .prepare("SELECT status, last_error, result_json FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?")
          .get(uid, "run-3"),
      ).toMatchObject({ status: "completed", last_error: null });
      expect(JSON.parse((database.database
        .prepare("SELECT result_json FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?")
        .get(uid, "run-3") as { result_json: string }).result_json)).toMatchObject({
        uid,
        run_id: "run-3",
        evaluated_count: 0,
        created_count: 0,
        existing_count: 0,
        skipped_count: 0,
      });
    } finally {
      database.close();
    }
  });

  it("applies the legacy expiry policy and persists source-tombstoned audits idempotently", async () => {
    const { database, env, sent } = environment();
    try {
      const { uid, generation } = seedAuthority(database);
      readyControl(database, uid, generation);
      database.database
        .prepare(
          `INSERT INTO cf_memories
             (uid, id, content, memory_tier, valid_at, created_at, updated_at,
              status, processing_state, source_state, captured_at, expires_at,
              account_generation, evidence_json, conversation_id)
           VALUES (?, 'expired-active', 'expired', 'short_term', 100, 100, 100,
                   'active', 'processed', 'active', 100, 999999999, ?,
                   '[]', 'conversation-1'),
                  (?, 'expired-tombstoned', 'gone', 'short_term', 100, 100, 100,
                   'active', 'processed', 'tombstoned', 100, 999999999, ?,
                   '[]', 'conversation-2')`,
        )
        .run(uid, generation, uid, generation);

      const completed = await jobs.fetch(
        adminRequest(uid, "run_id=run-policy&limit=10&evaluated_at=2026-08-31T00:00:00Z"),
        env,
      );
      expect(completed.status).toBe(200);

      expect(database.database
        .prepare("SELECT status, last_error, result_json FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?")
        .get(uid, "run-policy")).toMatchObject({ status: "completed", last_error: null });
      expect(database.database
        .prepare("SELECT memory_id, outcome, reason FROM cf_memory_short_term_lifecycle_transitions WHERE uid = ? ORDER BY memory_id")
        .all(uid)).toEqual([
        { memory_id: "expired-active", outcome: "remain_short_term", reason: "short_term_expired_requires_lifecycle_decision" },
        { memory_id: "expired-tombstoned", outcome: "source_tombstoned", reason: "source_tombstoned" },
      ]);
      const result = JSON.parse((database.database
        .prepare("SELECT result_json FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?")
        .get(uid, "run-policy") as { result_json: string }).result_json);
      expect(result).toMatchObject({ evaluated_count: 2, created_count: 2, existing_count: 0 });
      const duplicate = await jobs.fetch(
        adminRequest(uid, "run_id=run-policy&limit=10&evaluated_at=2026-08-31T00:00:00Z"),
        env,
      );
      expect(duplicate.status).toBe(200);
      expect(await duplicate.json()).toMatchObject({ evaluated_count: 2, created_count: 2 });
      expect(database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_memory_short_term_lifecycle_transitions")
        .get()).toMatchObject({ count: 2 });
    } finally {
      database.close();
    }
  });

  it("releases a lease for transient D1 failure and lets the next delivery complete", async () => {
    const { database, env } = environment();
    try {
      const { uid, generation } = seedAuthority(database);
      readyControl(database, uid, generation);
      database.database
        .prepare(
          `INSERT INTO cf_memory_short_term_lifecycle_runs
             (uid, run_id, request_fingerprint, evaluated_at, requested_limit,
              status, attempts, next_attempt_at, account_generation, created_at, updated_at)
           VALUES (?, ?, ?, 100, 10, 'queued', 0, 100, ?, 100, 100)`,
        )
        .run(uid, "run-retry", "b".repeat(64), generation);
      database.failNextLifecycleMemoryRead();
      const first = { ack: vi.fn(), retry: vi.fn() };
      const message = {
        body: {
          jobId: "memory-stl-retry",
          uid,
          kind: "memory_short_term_lifecycle",
          payload: {
            runId: "run-retry",
            requestFingerprint: "b".repeat(64),
            accountGeneration: generation,
          },
        },
        attempts: 1,
        ack: first.ack,
        retry: first.retry,
      };
      const firstMessage = message as never;
      await processMemoryShortTermLifecycleMessage(firstMessage, env);
      expect(first.retry).toHaveBeenCalledOnce();
      expect(first.ack).not.toHaveBeenCalled();
      expect(database.database
        .prepare("SELECT status, last_error, lease_token FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?")
        .get(uid, "run-retry")).toMatchObject({ status: "queued", lease_token: null });

      const second = { ack: vi.fn(), retry: vi.fn() };
      await processMemoryShortTermLifecycleMessage({ ...message, attempts: 2, ack: second.ack, retry: second.retry } as never, env);
      expect(second.ack).toHaveBeenCalledOnce();
      expect(second.retry).not.toHaveBeenCalled();
      expect(database.database
        .prepare("SELECT status, last_error FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?")
        .get(uid, "run-retry")).toMatchObject({ status: "completed", last_error: null });
    } finally {
      database.close();
    }
  });

  it("rejects new admission when the account deletion fence is already present", async () => {
    const { database, env, sent } = environment();
    try {
      const { uid } = seedAuthority(database);
      database.database
        .prepare(
          `INSERT INTO cf_account_deletion_intents
             (uid, job_id, status, phase, next_attempt_at, created_at, updated_at)
           VALUES (?, 'delete-lifecycle-user', 'pending', 'quiescing', 1, 1, 1)`,
        )
        .run(uid);
      const response = await jobs.fetch(adminRequest(uid, "run_id=run-4"), env);
      expect(response.status).toBe(409);
      expect(await response.json()).toMatchObject({
        error: "short_term_lifecycle_unavailable",
        reason: "account_deletion_in_progress",
      });
      expect(sent).toHaveLength(0);
    } finally {
      database.close();
    }
  });

  it("rejects the admin boundary before touching D1 for a bad key", async () => {
    const { database, env, sent } = environment();
    try {
      const response = await jobs.fetch(
        adminRequest("lifecycle-user", "run_id=run-1", "wrong"),
        env,
      );
      expect(response.status).toBe(403);
      expect(sent).toHaveLength(0);
    } finally {
      database.close();
    }
  });
});
