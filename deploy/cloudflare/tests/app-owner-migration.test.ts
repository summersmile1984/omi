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
      __sql: sql,
      __args: args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as
          T | undefined) ?? null,
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

  async batch(statements: Array<{ __sql?: string; __args?: unknown[] }>) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((statement) => {
        const result = this.database
          .prepare(statement.__sql || "")
          .run(...((statement.__args || []) as never[]));
        return { meta: { changes: Number(result.changes) } };
      });
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
const SECRET = "app-owner-migration-secret";
const ADMIN_KEY = "app-owner-migration-admin";
const PROOF_HASH = "a".repeat(64);
const SOURCE_UID_HASH = "b".repeat(64);
const SOURCE_REF = `fb-anon-${SOURCE_UID_HASH}`;
const SOURCE_REVISION = "c".repeat(64);
const DATA_PROJECTION_REVISION = "d".repeat(64);

function environment(
  processor: (request: Request) => Promise<Response> = async () =>
    new Response(null, { status: 503 }),
  options: {
    seedSource?: boolean;
    auth?: (request: Request) => Promise<Response>;
  } = {},
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
    AUTH: {
      fetch: vi.fn(
        options.auth ||
          (async () =>
            Response.json({ error: "not_configured" }, { status: 503 })),
      ),
    },
    INTERNAL_ASSERTION_SECRET: SECRET,
    APPS_ADMIN_KEY: ADMIN_KEY,
    APP_OWNER_MIGRATION_STAGING_ENABLED: "true",
    APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED: "true",
    APP_OWNER_MIGRATION_DATA_ATTESTATION_STAGING_ENABLED: "true",
    FIREBASE_IDENTITY_PROJECTION_STAGING_ENABLED: "true",
  } as unknown as JobsEnv;
  database.database
    .prepare(
      "INSERT INTO cf_account_cutover (uid, state, account_generation, checkpoint_phase, destination_backend_bound, updated_at) VALUES (?, 'new', ?, 'completed', 1, ?)",
    )
    .run("target-user", 7, 1000);
  if (options.seedSource !== false) {
    database.database
      .prepare(
        "INSERT INTO cf_app_owner_migration_sources " +
          "(source_uid, source_uid_hash, source_provider, source_proof_hash, source_projection_revision, " +
          "projection_status, app_projection_count, memory_projection_count, " +
          "data_projection_status, data_projection_revision, memory_reencryption_status, " +
          "memory_reencryption_revision, target_uid, target_account_generation, " +
          "source_credential_generation, attestation_expires_at, imported_at, updated_at) " +
          "VALUES (?, ?, 'firebase-anonymous', ?, ?, 'imported', 2, 0, 'verified', ?, " +
          "'not_required', NULL, 'target-user', 7, 100, 4000000000, 1000, 1000)",
      )
      .run(
        SOURCE_REF,
        SOURCE_UID_HASH,
        PROOF_HASH,
        SOURCE_REVISION,
        DATA_PROJECTION_REVISION,
      );
  }
  return { database, env, sent };
}

function appFor(uid = "target-user") {
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerAppOwnerMigrationRoutes(
    app,
    async () =>
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
        source_uid: SOURCE_REF,
        source_proof_hash: proof,
      }),
    },
  );
}

function dataProjectionAttestationRequest(
  overrides: Partial<Record<string, unknown>> = {},
  secret = ADMIN_KEY,
) {
  return new Request(
    `https://jobs.test${appOwnerMigrationConstants.dataProjectionAttestationPath}`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "secret-key": secret,
      },
      body: JSON.stringify({
        source_uid: SOURCE_REF,
        source_uid_hash: SOURCE_UID_HASH,
        source_proof_hash: PROOF_HASH,
        source_projection_revision: SOURCE_REVISION,
        target_uid: "target-user",
        target_account_generation: 7,
        data_projection_revision: DATA_PROJECTION_REVISION,
        app_projection_count: 0,
        memory_projection_count: 0,
        memory_reencryption_status: "not_required",
        memory_reencryption_revision: null,
        ...overrides,
      }),
    },
  );
}

function projectionRequest(
  sourceUid = "firebase-anonymous-source",
  sourceToken = "firebase-anonymous-id-token",
) {
  return new Request(
    `https://jobs.test${appOwnerMigrationConstants.identityProjectionPath}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        source_uid: sourceUid,
        source_token: sourceToken,
      }),
    },
  );
}

function attestation(
  overrides: Partial<Record<string, unknown>> = {},
): Record<string, unknown> {
  const now = Math.floor(Date.now() / 1_000);
  return {
    target_uid: "target-user",
    source_ref: SOURCE_REF,
    source_uid_hash: SOURCE_UID_HASH,
    source_proof_hash: PROOF_HASH,
    source_credential_generation: 100,
    source_projection_revision: SOURCE_REVISION,
    attested_at: now,
    expires_at: now + 3_000,
    ...overrides,
  };
}

function queuedMessage(
  jobId: string,
  uid = "target-user",
): Message<JobMessage> {
  return {
    body: {
      jobId,
      uid,
      kind: "app_owner_migration",
      payload: {
        sourceUid: SOURCE_REF,
        targetAccountGeneration: 7,
        sourceProjectionRevision: SOURCE_REVISION,
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
  it("projects verified anonymous evidence without persisting the Firebase uid or token", async () => {
    const auth = vi.fn(async (request: Request) => {
      expect(request.headers.get("authorization")).toBe(
        "Bearer firebase-anonymous-id-token",
      );
      expect(await request.json()).toEqual({
        expected_source_uid: "firebase-anonymous-source",
      });
      return Response.json(attestation());
    });
    const { env, database } = environment(undefined, {
      seedSource: false,
      auth,
    });
    const app = appFor();
    const first = await app.fetch(projectionRequest(), env);
    expect(first.status).toBe(201);
    expect(await first.json()).toMatchObject({
      source_ref: SOURCE_REF,
      source_proof_hash: PROOF_HASH,
      target_account_generation: 7,
      status: "imported",
    });
    const replay = await app.fetch(projectionRequest(), env);
    expect(replay.status).toBe(200);
    expect(auth).toHaveBeenCalledTimes(2);
    const row = database.database
      .prepare("SELECT * FROM cf_app_owner_migration_sources")
      .get() as Record<string, unknown>;
    expect(row).toMatchObject({
      source_uid: SOURCE_REF,
      source_uid_hash: SOURCE_UID_HASH,
      target_uid: "target-user",
      target_account_generation: 7,
      projection_status: "imported",
    });
    expect(JSON.stringify(row)).not.toContain("firebase-anonymous-source");
    expect(JSON.stringify(row)).not.toContain("firebase-anonymous-id-token");
  });

  it("marks changed proof evidence as conflict and rejects revoked credentials", async () => {
    let changed = false;
    const auth = vi.fn(async () =>
      changed
        ? Response.json(
            attestation({
              source_proof_hash: "d".repeat(64),
              source_projection_revision: "e".repeat(64),
              source_credential_generation: 101,
            }),
          )
        : Response.json(attestation()),
    );
    const { env, database } = environment(undefined, {
      seedSource: false,
      auth,
    });
    const app = appFor();
    expect((await app.fetch(projectionRequest(), env)).status).toBe(201);
    changed = true;
    const conflict = await app.fetch(projectionRequest(), env);
    expect(conflict.status).toBe(409);
    expect(await conflict.json()).toEqual({
      error: "firebase_identity_projection_conflict",
    });
    expect(
      database.database
        .prepare("SELECT projection_status FROM cf_app_owner_migration_sources")
        .get(),
    ).toEqual({ projection_status: "conflict" });

    const revoked = environment(undefined, {
      seedSource: false,
      auth: async () =>
        Response.json({ error: "source_identity_revoked" }, { status: 403 }),
    });
    const rejected = await app.fetch(projectionRequest(), revoked.env);
    expect(rejected.status).toBe(403);
    expect(await rejected.json()).toEqual({
      error: "source_identity_revoked",
    });
    expect(
      revoked.database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_app_owner_migration_sources")
        .get(),
    ).toEqual({ count: 0 });
  });

  it("checks the target deletion fence before sending the Firebase credential", async () => {
    const auth = vi.fn(async () => Response.json(attestation()));
    const { env, database } = environment(undefined, {
      seedSource: false,
      auth,
    });
    database.database
      .prepare(
        "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES (?, ?, 'pending', 'quiescing', 1000, 1000, 1000)",
      )
      .run("target-user", "delete-projection-target");
    const response = await appFor().fetch(projectionRequest(), env);
    expect(response.status).toBe(409);
    expect(auth).not.toHaveBeenCalled();
  });

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

  it("does not admit identity-only evidence as a data migration", async () => {
    const auth = vi.fn(async () => Response.json(attestation()));
    const { env, sent } = environment(undefined, {
      seedSource: false,
      auth,
    });
    const app = appFor();
    expect((await app.fetch(projectionRequest(), env)).status).toBe(201);

    const response = await app.fetch(
      migrationRequest("identity-only-migration"),
      env,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: "app_owner_migration_unavailable",
      reason: "source_data_projection_not_admitted",
    });
    expect(sent).toHaveLength(0);
  });

  it("writes a reviewed data attestation once and rejects conflicting replay", async () => {
    const auth = vi.fn(async () => Response.json(attestation()));
    const { env, database } = environment(undefined, {
      seedSource: false,
      auth,
    });
    const app = appFor();
    expect((await app.fetch(projectionRequest(), env)).status).toBe(201);

    const first = await app.fetch(
      dataProjectionAttestationRequest({ app_projection_count: 2 }),
      env,
    );
    expect(first.status).toBe(201);
    expect(await first.json()).toMatchObject({
      source_uid: SOURCE_REF,
      status: "attested",
      memory_reencryption_status: "not_required",
    });
    expect(
      database.database
        .prepare(
          "SELECT app_projection_count, data_projection_status, data_projection_revision, " +
            "memory_reencryption_status FROM cf_app_owner_migration_sources",
        )
        .get(),
    ).toEqual({
      app_projection_count: 2,
      data_projection_status: "verified",
      data_projection_revision: DATA_PROJECTION_REVISION,
      memory_reencryption_status: "not_required",
    });

    const replay = await app.fetch(
      dataProjectionAttestationRequest({ app_projection_count: 2 }),
      env,
    );
    expect(replay.status).toBe(200);
    expect(await replay.json()).toMatchObject({ status: "already_attested" });

    const conflict = await app.fetch(
      dataProjectionAttestationRequest({
        app_projection_count: 1,
        data_projection_revision: "e".repeat(64),
      }),
      env,
    );
    expect(conflict.status).toBe(409);
    expect(await conflict.json()).toEqual({ error: "source_projection_conflict" });
  });

  it("keeps the attestation writer admin-only, gated, and memory-safe", async () => {
    const { env } = environment();
    const app = appFor();
    const forbidden = await app.fetch(
      dataProjectionAttestationRequest({}, "wrong-admin-key"),
      env,
    );
    expect(forbidden.status).toBe(403);

    const invalidMemory = await app.fetch(
      dataProjectionAttestationRequest({
        memory_projection_count: 1,
      }),
      env,
    );
    expect(invalidMemory.status).toBe(422);

    env.APP_OWNER_MIGRATION_DATA_ATTESTATION_STAGING_ENABLED = "false";
    const unavailable = await app.fetch(
      dataProjectionAttestationRequest(),
      env,
    );
    expect(unavailable.status).toBe(503);
  });

  it("blocks a source with memories until re-encryption evidence is attested", async () => {
    const { env, database, sent } = environment(undefined, {
      seedSource: false,
    });
    database.database
      .prepare(
        "INSERT INTO cf_app_owner_migration_sources " +
          "(source_uid, source_uid_hash, source_provider, source_proof_hash, source_projection_revision, " +
          "projection_status, app_projection_count, memory_projection_count, data_projection_status, " +
          "data_projection_revision, memory_reencryption_status, memory_reencryption_revision, target_uid, " +
          "target_account_generation, source_credential_generation, attestation_expires_at, imported_at, updated_at) " +
          "VALUES (?, ?, 'firebase-anonymous', ?, ?, 'imported', 0, 1, 'verified', ?, " +
          "'unverified', NULL, 'target-user', 7, 100, 4000000000, 1000, 1000)",
      )
      .run(
        SOURCE_REF,
        SOURCE_UID_HASH,
        PROOF_HASH,
        SOURCE_REVISION,
        DATA_PROJECTION_REVISION,
      );
    const response = await appFor().fetch(
      migrationRequest("memory-pending"),
      env,
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: "app_owner_migration_unavailable",
      reason: "source_data_projection_not_admitted",
    });
    expect(sent).toHaveLength(0);
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
      source_uid: SOURCE_REF,
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

  it("claims a lease and atomically transfers projected D1 app ownership", async () => {
    const { env, sent, database } = environment();
    for (const appId of ["projected-app-1", "projected-app-2"]) {
      database.database
        .prepare(
          "INSERT INTO cf_app_catalog (id, approved, status, disabled, data_json, updated_at, owner_uid) VALUES (?, 1, 'approved', 0, ?, 1000, ?)",
        )
        .run(appId, JSON.stringify({ id: appId, name: appId }), SOURCE_REF);
    }
    const app = appFor();
    const response = await app.fetch(migrationRequest(), env);
    const body = (await response.json()) as { job_id: string };
    const message = queuedMessage(body.job_id);
    await processAppOwnerMigrationMessage(message, env);
    expect(message.ack).toHaveBeenCalledOnce();
    expect(message.retry).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect(
      database.database
        .prepare(
          "SELECT status, attempts, lease_token, next_attempt_at, last_error, result_json FROM cf_app_owner_migration_jobs",
        )
        .get(),
    ).toMatchObject({
      status: "completed",
      attempts: 1,
      lease_token: null,
      last_error: null,
    });
    expect(
      database.database
        .prepare(
          "SELECT id, owner_uid, owner_account_generation, owner_migration_job_id FROM cf_app_catalog ORDER BY id",
        )
        .all(),
    ).toEqual([
      {
        id: "projected-app-1",
        owner_uid: "target-user",
        owner_account_generation: 7,
        owner_migration_job_id: body.job_id,
      },
      {
        id: "projected-app-2",
        owner_uid: "target-user",
        owner_account_generation: 7,
        owner_migration_job_id: body.job_id,
      },
    ]);
    expect(env.API_CORE?.fetch).not.toHaveBeenCalled();
  });
});
