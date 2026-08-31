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
  run(): Promise<{ meta: { changes: number } }>;
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
      run: async () => ({
        meta: {
          changes: Number(this.database.prepare(sql).run(...args.map(sqliteValue)).changes),
        },
      }),
    });
    return build();
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

describe("Cloudflare short-term lifecycle shadow boundary", () => {
  it("fails closed without a D1 control projection and does not create a run", async () => {
    const { database, env, sent } = environment();
    try {
      seedAuthority(database);
      const response = await jobs.fetch(
        adminRequest("lifecycle-user", "run_id=run-1"),
        env,
      );

      expect(response.status).toBe(503);
      expect(await response.json()).toMatchObject({
        error: "short_term_lifecycle_unavailable",
        reason: "missing_ready_lifecycle_authority",
      });
      expect(sent).toHaveLength(0);
      expect(
        database.database
          .prepare("SELECT COUNT(*) AS count FROM cf_memory_short_term_lifecycle_runs")
          .get(),
      ).toMatchObject({ count: 0 });
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

      expect(first.status).toBe(202);
      expect(await first.json()).toMatchObject({
        status: "queued",
        run_id: "run-1",
        account_generation: generation,
      });
      expect(duplicate.status).toBe(200);
      expect(await duplicate.json()).toEqual({ status: "already_queued", run_id: "run-1" });
      expect(conflict.status).toBe(409);
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({
        uid,
        kind: "memory_short_term_lifecycle",
        payload: { runId: "run-1", accountGeneration: generation },
      });
    } finally {
      database.close();
    }
  });

  it("rechecks the deletion fence before leasing and never writes a transition", async () => {
    const { database, env, sent } = environment();
    try {
      const { uid, generation } = seedAuthority(database);
      readyControl(database, uid, generation);
      const queued = await jobs.fetch(adminRequest(uid, "run_id=run-2"), env);
      expect(queued.status).toBe(202);

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
          body: sent[0],
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

  it("fails a ready-but-unimplemented executor explicitly instead of reporting parity", async () => {
    const { database, env, sent } = environment();
    try {
      const { uid, generation } = seedAuthority(database);
      readyControl(database, uid, generation);
      const queued = await jobs.fetch(adminRequest(uid, "run_id=run-3"), env);
      expect(queued.status).toBe(202);
      const acknowledgements = { ack: vi.fn(), retry: vi.fn() };
      await processMemoryShortTermLifecycleMessage(
        { body: sent[0], attempts: 1, ack: acknowledgements.ack, retry: acknowledgements.retry } as never,
        env,
      );
      expect(
        database.database
          .prepare("SELECT status, last_error FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?")
          .get(uid, "run-3"),
      ).toMatchObject({ status: "failed", last_error: "lifecycle_executor_unavailable" });
      expect(acknowledgements.ack).toHaveBeenCalledOnce();
      expect(acknowledgements.retry).not.toHaveBeenCalled();
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
