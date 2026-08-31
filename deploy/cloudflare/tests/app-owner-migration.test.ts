import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Hono } from "hono";
import type { Message } from "@cloudflare/workers-types";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import {
  appOwnerMigrationConstants,
  processAppOwnerMigrationMessage,
  registerAppOwnerMigrationRoutes,
} from "../workers/jobs/app-owner-migration";

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
        (this.database.prepare(sql).get(...(args as never[])) as T | undefined) ??
        null,
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

const databases: SqliteD1[] = [];
const SECRET = "app-owner-migration-secret";
const PROOF_HASH = "a".repeat(64);

function environment(
  processor: (request: Request) => Promise<Response> = async () =>
    new Response(null, { status: 503 }),
) {
  const database = new SqliteD1();
  databases.push(database);
  const sent: JobMessage[] = [];
  const env = {
    APP_DB: database,
    JOBS: {
      send: vi.fn(async (message: JobMessage) => {
        sent.push(message);
      }),
    },
    API_CORE: { fetch: vi.fn(processor) },
    INTERNAL_ASSERTION_SECRET: SECRET,
    APP_OWNER_MIGRATION_STAGING_ENABLED: "true",
    APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED: "true",
  } as unknown as JobsEnv;
  database.database
    .prepare(
      "INSERT INTO cf_account_cutover (uid, state, account_generation, checkpoint_phase, destination_backend_bound, updated_at) VALUES (?, 'new', ?, 'completed', 1, ?)",
    )
    .run("target-user", 7, 1000);
  database.database
    .prepare(
      "INSERT INTO cf_app_owner_migration_sources (source_uid, source_provider, source_proof_hash, source_projection_revision, projection_status, app_projection_count, memory_projection_count, imported_at, updated_at) VALUES (?, 'firebase-anonymous', ?, 'source-rev-1', 'imported', 2, 3, 1000, 1000)",
    )
    .run("anonymous-source", PROOF_HASH);
  return { database, env, sent };
}

function appFor(uid = "target-user") {
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerAppOwnerMigrationRoutes(app, async () =>
    ({
      uid,
      authority: "better-auth",
      requestId: "migration-test",
      version: 1,
      audience: "jobs",
      assertionId: "assertion",
      issuedAt: 1,
      expiresAt: 2,
      method: "POST",
      path: appOwnerMigrationConstants.routePath,
    }) as never,
  );
  return app;
}

function migrationRequest(idempotencyKey = "migration-1", proof = PROOF_HASH) {
  return new Request(
    `https://jobs.test${appOwnerMigrationConstants.routePath}`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "idempotency-key": idempotencyKey,
      },
      body: JSON.stringify({
        source_uid: "anonymous-source",
        source_proof_hash: proof,
      }),
    },
  );
}

function queuedMessage(jobId: string, uid = "target-user"): Message<JobMessage> {
  return {
    body: {
      jobId,
      uid,
      kind: "app_owner_migration",
      payload: {
        sourceUid: "anonymous-source",
        targetAccountGeneration: 7,
        sourceProjectionRevision: "source-rev-1",
      },
    },
    attempts: 1,
    ack: vi.fn(),
    retry: vi.fn(),
  } as unknown as Message<JobMessage>;
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("dormant app owner migration seam", () => {
  it("fails closed without the feature gate or imported anonymous proof", async () => {
    const { env } = environment();
    env.APP_OWNER_MIGRATION_STAGING_ENABLED = "false";
    const response = await appFor().fetch(migrationRequest(), env);
    expect(response.status).toBe(503);

    env.APP_OWNER_MIGRATION_STAGING_ENABLED = "true";
    env.APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED = "true";
    const missingProof = await appFor().fetch(
      migrationRequest("migration-2", "b".repeat(64)),
      env,
    );
    expect(missingProof.status).toBe(503);
    expect((await missingProof.json()) as { reason?: string }).toMatchObject({
      reason: "source_proof_not_admitted",
    });
  });

  it("admits one D1 job and returns the same row on replay", async () => {
    const { env, sent, database } = environment();
    const app = appFor();
    const first = await app.fetch(migrationRequest(), env);
    expect(first.status).toBe(202);
    const firstBody = await first.json();
    const replay = await app.fetch(migrationRequest(), env);
    expect(replay.status).toBe(200);
    expect(await replay.json()).toEqual(firstBody);
    expect(sent).toHaveLength(1);
    expect(
      database.database
        .prepare(
          "SELECT status, source_uid, target_uid, attempts FROM cf_app_owner_migration_jobs",
        )
        .get(),
    ).toMatchObject({
      status: "queued",
      source_uid: "anonymous-source",
      target_uid: "target-user",
      attempts: 0,
    });
  });

  it("rejects cross-owner replay of the same source proof", async () => {
    const { env, database } = environment();
    database.database
      .prepare(
        "INSERT INTO cf_account_cutover (uid, state, account_generation, checkpoint_phase, destination_backend_bound, updated_at) VALUES (?, 'new', 8, 'completed', 1, 1000)",
      )
      .run("other-target");
    const first = await appFor().fetch(migrationRequest(), env);
    expect(first.status).toBe(202);
    const second = await appFor("other-target").fetch(
      migrationRequest("other-key"),
      env,
    );
    expect(second.status).toBe(409);
    expect(await second.json()).toMatchObject({
      error: "migration_request_conflict",
    });
  });

  it("honors a deletion fence before admitting a job", async () => {
    const { env, database, sent } = environment();
    database.database
      .prepare(
        "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES (?, ?, 'pending', 'quiescing', 1000, 1000, 1000)",
      )
      .run("target-user", "delete-target");
    const response = await appFor().fetch(migrationRequest(), env);
    expect(response.status).toBe(409);
    expect(sent).toHaveLength(0);
    expect(
      database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_app_owner_migration_jobs")
        .get(),
    ).toMatchObject({ count: 0 });
  });

  it("claims a lease and retries a transient executor failure", async () => {
    const { env, sent, database } = environment();
    const app = appFor();
    const response = await app.fetch(migrationRequest(), env);
    const body = (await response.json()) as { job_id: string };
    const message = queuedMessage(body.job_id);
    await processAppOwnerMigrationMessage(message, env);
    expect(message.retry).toHaveBeenCalledOnce();
    expect(message.ack).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect(
      database.database
        .prepare(
          "SELECT status, attempts, lease_token, next_attempt_at, last_error FROM cf_app_owner_migration_jobs",
        )
        .get(),
    ).toMatchObject({
      status: "queued",
      attempts: 1,
      lease_token: null,
      last_error: "app owner migration executor unavailable",
    });
  });
});
