import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import { createSignedAuthContext } from "../shared/auth-context";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";

/**
 * Dormant Cloudflare seam for POST /v1/apps/migrate-owner.
 *
 * The legacy route accepts a Firebase anonymous credential and performs
 * Firestore ownership plus memory re-encryption side effects.  This module
 * accepts only a hash-only proof that a trusted import workflow has already
 * verified and projected.  It intentionally does not verify Firebase tokens
 * or mutate the app catalog: the future API Core executor owns that contract.
 *
 * There is no Edge route for this path yet.  Registering the namespaced Jobs
 * route keeps the lifecycle testable while APP_OWNER_MIGRATION_STAGING_ENABLED
 * and APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED remain off by default.
 */

const ROUTE_PATH = "/v2/cf/apps/migrate-owner";
const PROCESSOR_PATH = "/internal/apps/migrate-owner";
const MAX_BODY_BYTES = 16_000;
const MAX_UID_LENGTH = 256;
const MAX_JOB_ID_LENGTH = 128;
const MAX_REVISION_LENGTH = 256;
const MAX_IDEMPOTENCY_KEY_LENGTH = 128;
const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const MAX_ATTEMPTS = 3;
const RECONCILE_BATCH_SIZE = 50;

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;

export type AppOwnerMigrationJobRow = {
  job_id: string;
  source_uid: string;
  target_uid: string;
  source_proof_hash: string;
  source_projection_revision: string;
  target_account_generation: number;
  idempotency_key: string;
  request_fingerprint: string;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  lease_token: string | null;
  lease_until: number | null;
  next_attempt_at: number;
  last_error: string | null;
  result_json: string | null;
};

type AppOwnerMigrationSource = {
  source_uid: string;
  source_provider: "firebase-anonymous";
  source_proof_hash: string;
  source_projection_revision: string;
  projection_status: "imported" | "revoked" | "conflict";
  app_projection_count: number;
  memory_projection_count: number;
};

type MigrationRequest = {
  sourceUid: string;
  sourceProofHash: string;
  idempotencyKey: string;
};

function validText(value: unknown, maximum: number): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= maximum &&
    !/[\\/\0]/.test(value)
  );
}

function validHash(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function integer(value: unknown, minimum = 0): number | null {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(result) && result >= minimum ? result : null;
}

function parseJob(value: unknown): AppOwnerMigrationJobRow | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Partial<AppOwnerMigrationJobRow>;
  const generation = integer(row.target_account_generation);
  const attempts = integer(row.attempts);
  const nextAttemptAt = integer(row.next_attempt_at);
  const leaseUntil =
    row.lease_until === null || row.lease_until === undefined
      ? null
      : integer(row.lease_until);
  if (
    !validText(row.job_id, MAX_JOB_ID_LENGTH) ||
    !validText(row.source_uid, MAX_UID_LENGTH) ||
    !validText(row.target_uid, MAX_UID_LENGTH) ||
    row.source_uid === row.target_uid ||
    !validHash(row.source_proof_hash) ||
    !validText(row.source_projection_revision, MAX_REVISION_LENGTH) ||
    generation === null ||
    !validText(row.idempotency_key, MAX_IDEMPOTENCY_KEY_LENGTH) ||
    !validHash(row.request_fingerprint) ||
    (row.status !== "queued" &&
      row.status !== "running" &&
      row.status !== "completed" &&
      row.status !== "failed") ||
    attempts === null ||
    nextAttemptAt === null
  ) {
    return null;
  }
  if (
    row.lease_token !== null &&
    row.lease_token !== undefined &&
    !validText(row.lease_token, MAX_JOB_ID_LENGTH)
  ) {
    return null;
  }
  if (
    row.last_error !== null &&
    row.last_error !== undefined &&
    typeof row.last_error !== "string"
  ) {
    return null;
  }
  if (
    row.result_json !== null &&
    row.result_json !== undefined &&
    typeof row.result_json !== "string"
  ) {
    return null;
  }
  return {
    job_id: row.job_id,
    source_uid: row.source_uid,
    target_uid: row.target_uid,
    source_proof_hash: row.source_proof_hash,
    source_projection_revision: row.source_projection_revision,
    target_account_generation: generation,
    idempotency_key: row.idempotency_key,
    request_fingerprint: row.request_fingerprint,
    status: row.status,
    attempts,
    lease_token: row.lease_token ?? null,
    lease_until: leaseUntil,
    next_attempt_at: nextAttemptAt,
    last_error: row.last_error ?? null,
    result_json: row.result_json ?? null,
  };
}

function parseSource(value: unknown): AppOwnerMigrationSource | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Partial<AppOwnerMigrationSource>;
  const appCount = integer(row.app_projection_count);
  const memoryCount = integer(row.memory_projection_count);
  if (
    !validText(row.source_uid, MAX_UID_LENGTH) ||
    row.source_provider !== "firebase-anonymous" ||
    !validHash(row.source_proof_hash) ||
    !validText(row.source_projection_revision, MAX_REVISION_LENGTH) ||
    (row.projection_status !== "imported" &&
      row.projection_status !== "revoked" &&
      row.projection_status !== "conflict") ||
    appCount === null ||
    memoryCount === null
  ) {
    return null;
  }
  return {
    source_uid: row.source_uid,
    source_provider: row.source_provider,
    source_proof_hash: row.source_proof_hash,
    source_projection_revision: row.source_projection_revision,
    projection_status: row.projection_status,
    app_projection_count: appCount,
    memory_projection_count: memoryCount,
  };
}

function noStoreHeaders(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function responseForJob(job: AppOwnerMigrationJobRow): Record<string, unknown> {
  return {
    job_id: job.job_id,
    status: job.status,
    attempts: job.attempts,
    next_attempt_at: job.next_attempt_at,
    ...(job.last_error ? { error: job.last_error } : {}),
    ...(job.result_json ? { result: JSON.parse(job.result_json) } : {}),
  };
}

function queueMessage(job: AppOwnerMigrationJobRow): JobMessage {
  return {
    jobId: job.job_id,
    uid: job.target_uid,
    kind: "app_owner_migration",
    payload: {
      sourceUid: job.source_uid,
      targetAccountGeneration: job.target_account_generation,
      sourceProjectionRevision: job.source_projection_revision,
    },
  };
}

function retryDelay(attempts: number): number {
  return RETRY_DELAY_SECONDS * 2 ** Math.min(Math.max(0, attempts - 1), 5);
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export async function appOwnerMigrationFingerprint(
  sourceUid: string,
  sourceProofHash: string,
  targetUid: string,
  targetGeneration: number,
  sourceProjectionRevision: string,
): Promise<string> {
  return sha256Hex(
    `app-owner-migration\0${sourceUid}\0${sourceProofHash}\0${targetUid}\0${targetGeneration}\0${sourceProjectionRevision}`,
  );
}

async function readBoundedJson(request: Request): Promise<unknown> {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && (declared < 0 || declared > MAX_BODY_BYTES)) {
    throw new Error("request body is too large");
  }
  if (!request.body) return {};
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) break;
      size += item.value.byteLength;
      if (size > MAX_BODY_BYTES) throw new Error("request body is too large");
      chunks.push(item.value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

function parseRequest(value: unknown, idempotencyKey: string | null): MigrationRequest | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const body = value as Record<string, unknown>;
  if (
    "source_token" in body ||
    "firebase_id_token" in body ||
    "target_uid" in body
  ) {
    return null;
  }
  if (
    !validText(body.source_uid, MAX_UID_LENGTH) ||
    !validHash(body.source_proof_hash) ||
    !validText(idempotencyKey, MAX_IDEMPOTENCY_KEY_LENGTH)
  ) {
    return null;
  }
  return {
    sourceUid: body.source_uid,
    sourceProofHash: body.source_proof_hash,
    idempotencyKey,
  };
}

async function deletionFence(env: JobsEnv, uid: string): Promise<boolean> {
  const now = Math.floor(Date.now() / 1_000);
  const row = await env.APP_DB.prepare(
    "SELECT lifecycle FROM (" +
      "SELECT 'deleting' AS lifecycle, 0 AS priority FROM cf_account_deletion_intents WHERE uid = ? " +
      "UNION ALL SELECT 'deleted' AS lifecycle, 1 AS priority FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?" +
      ") ORDER BY priority LIMIT 1",
  )
    .bind(uid, uid, now)
    .first<{ lifecycle?: unknown }>();
  return row?.lifecycle === "deleting" || row?.lifecycle === "deleted";
}

async function targetGeneration(env: JobsEnv, uid: string): Promise<number | null> {
  const row = await env.APP_DB.prepare(
    "SELECT uid, state, checkpoint_phase, destination_backend_bound, account_generation " +
      "FROM cf_account_cutover WHERE uid = ? LIMIT 1",
  )
    .bind(uid)
    .first<{
      uid?: unknown;
      state?: unknown;
      checkpoint_phase?: unknown;
      destination_backend_bound?: unknown;
      account_generation?: unknown;
    }>();
  if (
    !row ||
    row.uid !== uid ||
    row.state !== "new" ||
    row.checkpoint_phase !== "completed" ||
    Number(row.destination_backend_bound) !== 1
  ) {
    return null;
  }
  return integer(row.account_generation);
}

async function sourceProjection(
  env: JobsEnv,
  sourceUid: string,
): Promise<AppOwnerMigrationSource | null> {
  const row = await env.APP_DB.prepare(
    "SELECT source_uid, source_provider, source_proof_hash, source_projection_revision, " +
      "projection_status, app_projection_count, memory_projection_count " +
      "FROM cf_app_owner_migration_sources WHERE source_uid = ? LIMIT 1",
  )
    .bind(sourceUid)
    .first();
  return parseSource(row);
}

async function jobById(
  env: JobsEnv,
  targetUid: string,
  jobId: string,
): Promise<AppOwnerMigrationJobRow | null> {
  const row = await env.APP_DB.prepare(
    "SELECT job_id, source_uid, target_uid, source_proof_hash, source_projection_revision, " +
      "target_account_generation, idempotency_key, request_fingerprint, status, attempts, " +
      "lease_token, lease_until, next_attempt_at, last_error, result_json " +
      "FROM cf_app_owner_migration_jobs WHERE target_uid = ? AND job_id = ? LIMIT 1",
  )
    .bind(targetUid, jobId)
    .first();
  return parseJob(row);
}

async function existingJob(
  env: JobsEnv,
  targetUid: string,
  sourceUid: string,
  idempotencyKey: string,
): Promise<AppOwnerMigrationJobRow | null> {
  const bySource = await env.APP_DB.prepare(
    "SELECT job_id, source_uid, target_uid, source_proof_hash, source_projection_revision, " +
      "target_account_generation, idempotency_key, request_fingerprint, status, attempts, " +
      "lease_token, lease_until, next_attempt_at, last_error, result_json " +
      "FROM cf_app_owner_migration_jobs WHERE target_uid = ? AND source_uid = ? LIMIT 1",
  )
    .bind(targetUid, sourceUid)
    .first();
  const sourceJob = parseJob(bySource);
  const byKey = await env.APP_DB.prepare(
    "SELECT job_id, source_uid, target_uid, source_proof_hash, source_projection_revision, " +
      "target_account_generation, idempotency_key, request_fingerprint, status, attempts, " +
      "lease_token, lease_until, next_attempt_at, last_error, result_json " +
      "FROM cf_app_owner_migration_jobs WHERE target_uid = ? AND idempotency_key = ? LIMIT 1",
  )
    .bind(targetUid, idempotencyKey)
    .first();
  const keyJob = parseJob(byKey);
  if (sourceJob && keyJob && sourceJob.job_id !== keyJob.job_id) {
    throw new Error("migration identity conflict");
  }
  return sourceJob || keyJob;
}

async function jobBySource(
  env: JobsEnv,
  sourceUid: string,
): Promise<AppOwnerMigrationJobRow | null> {
  const row = await env.APP_DB.prepare(
    "SELECT job_id, source_uid, target_uid, source_proof_hash, source_projection_revision, " +
      "target_account_generation, idempotency_key, request_fingerprint, status, attempts, " +
      "lease_token, lease_until, next_attempt_at, last_error, result_json " +
      "FROM cf_app_owner_migration_jobs WHERE source_uid = ? LIMIT 1",
  )
    .bind(sourceUid)
    .first();
  return parseJob(row);
}

async function publish(
  env: JobsEnv,
  job: AppOwnerMigrationJobRow,
): Promise<boolean> {
  try {
    await env.JOBS.send(queueMessage(job));
    return true;
  } catch {
    return false;
  }
}

async function markQueueUnavailable(
  env: JobsEnv,
  job: AppOwnerMigrationJobRow,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_app_owner_migration_jobs SET status = 'failed', last_error = ?, " +
      "lease_token = NULL, lease_until = NULL, updated_at = ? " +
      "WHERE target_uid = ? AND job_id = ? AND status = 'queued'",
  )
    .bind("queue unavailable", now, job.target_uid, job.job_id)
    .run();
}

async function admit(c: JobsContext, context: SignedAuthContext): Promise<Response> {
  if (
    c.env.APP_OWNER_MIGRATION_STAGING_ENABLED !== "true" ||
    c.env.APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED !== "true"
  ) {
    return c.json({ error: "app_owner_migration_unavailable" }, 503, noStoreHeaders());
  }
  if (context.authority !== "better-auth") {
    return c.json({ error: "better_auth_required" }, 403, noStoreHeaders());
  }
  let parsed: unknown;
  try {
    parsed = await readBoundedJson(c.req.raw);
  } catch {
    return c.json({ error: "invalid_request" }, 400, noStoreHeaders());
  }
  const idempotencyKey = c.req.header("idempotency-key") || null;
  const request = parseRequest(parsed, idempotencyKey);
  if (!request || request.sourceUid === context.uid) {
    return c.json({ error: "invalid_migration_request" }, 422, noStoreHeaders());
  }
  try {
    if (
      (await deletionFence(c.env, context.uid)) ||
      (await deletionFence(c.env, request.sourceUid))
    ) {
      return c.json({ error: "account_deletion_in_progress" }, 409, noStoreHeaders());
    }
    const generation = await targetGeneration(c.env, context.uid);
    if (generation === null) {
      return c.json(
        { error: "app_owner_migration_unavailable", reason: "target_not_admitted" },
        503,
        noStoreHeaders(),
      );
    }
    const source = await sourceProjection(c.env, request.sourceUid);
    if (
      !source ||
      source.projection_status !== "imported" ||
      source.source_proof_hash !== request.sourceProofHash
    ) {
      return c.json(
        { error: "app_owner_migration_unavailable", reason: "source_proof_not_admitted" },
        503,
        noStoreHeaders(),
      );
    }
    const fingerprint = await appOwnerMigrationFingerprint(
      request.sourceUid,
      request.sourceProofHash,
      context.uid,
      generation,
      source.source_projection_revision,
    );
    const existing = await existingJob(
      c.env,
      context.uid,
      request.sourceUid,
      request.idempotencyKey,
    );
    if (existing) {
      if (
        existing.source_proof_hash !== request.sourceProofHash ||
        existing.request_fingerprint !== fingerprint
      ) {
        return c.json({ error: "migration_request_conflict" }, 409, noStoreHeaders());
      }
      return c.json(responseForJob(existing), 200, noStoreHeaders());
    }
    const now = Math.floor(Date.now() / 1_000);
    const jobId = `app-owner-migration-${fingerprint.slice(0, 48)}`;
    const inserted = await c.env.APP_DB.prepare(
      "INSERT INTO cf_app_owner_migration_jobs " +
        "(job_id, source_uid, target_uid, source_proof_hash, source_projection_revision, " +
        "target_account_generation, idempotency_key, request_fingerprint, status, attempts, " +
        "next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?) ON CONFLICT DO NOTHING",
    )
      .bind(
        jobId,
        request.sourceUid,
        context.uid,
        request.sourceProofHash,
        source.source_projection_revision,
        generation,
        request.idempotencyKey,
        fingerprint,
        now,
        now,
        now,
      )
      .run();
    if (Number(inserted.meta?.changes || 0) !== 1) {
      const raced = await jobBySource(c.env, request.sourceUid);
      if (!raced) {
        return c.json(
          { error: "app_owner_migration_unavailable" },
          503,
          noStoreHeaders(),
        );
      }
      if (raced.target_uid !== context.uid) {
        return c.json(
          { error: "migration_request_conflict" },
          409,
          noStoreHeaders(),
        );
      }
      return c.json(responseForJob(raced), 200, noStoreHeaders());
    }
    const row = await jobById(c.env, context.uid, jobId);
    if (!row) return c.json({ error: "app_owner_migration_unavailable" }, 503, noStoreHeaders());
    if (!(await publish(c.env, row))) {
      await markQueueUnavailable(c.env, row, now);
      return c.json({ error: "app_owner_migration_unavailable" }, 503, noStoreHeaders());
    }
    return c.json(responseForJob(row), 202, noStoreHeaders());
  } catch (error) {
    if (error instanceof Error && error.message === "migration identity conflict") {
      return c.json({ error: "migration_request_conflict" }, 409, noStoreHeaders());
    }
    return c.json({ error: "app_owner_migration_unavailable" }, 503, noStoreHeaders());
  }
}

async function callProcessor(env: JobsEnv, job: AppOwnerMigrationJobRow): Promise<Response> {
  if (!env.API_CORE) throw new Error("api core service unavailable");
  const signed = await createSignedAuthContext(
    { uid: job.target_uid, authority: "internal", requestId: `app-owner-migration:${job.job_id}` },
    "api-core",
    "POST",
    PROCESSOR_PATH,
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!signed) throw new Error("internal assertion unavailable");
  return env.API_CORE.fetch(
    new Request(`https://api-core.internal${PROCESSOR_PATH}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-omi-auth-context": signed.encoded,
        "x-omi-internal-signature": signed.signature,
      },
      body: JSON.stringify({
        job_id: job.job_id,
        source_uid: job.source_uid,
        target_uid: job.target_uid,
        source_projection_revision: job.source_projection_revision,
        target_account_generation: job.target_account_generation,
      }),
    }),
  );
}

async function updateFailed(
  env: JobsEnv,
  job: AppOwnerMigrationJobRow,
  reason: string,
  now: number,
): Promise<void> {
  try {
    await env.APP_DB.prepare(
      "UPDATE cf_app_owner_migration_jobs SET status = 'failed', lease_token = NULL, " +
        "lease_until = NULL, last_error = ?, updated_at = ? " +
        "WHERE target_uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
    )
      .bind(reason.slice(0, 2_048), now, job.target_uid, job.job_id, job.lease_token)
      .run();
  } catch {
    // A deletion trigger may intentionally reject this write.  The deletion
    // owner will delete the row; acknowledging avoids a late resurrection.
  }
}

async function updateRetry(
  env: JobsEnv,
  job: AppOwnerMigrationJobRow,
  now: number,
): Promise<boolean> {
  try {
    const result = await env.APP_DB.prepare(
      "UPDATE cf_app_owner_migration_jobs SET status = 'queued', lease_token = NULL, " +
        "lease_until = NULL, next_attempt_at = ?, last_error = ?, updated_at = ? " +
        "WHERE target_uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
    )
      .bind(
        now + retryDelay(job.attempts),
        "app owner migration executor unavailable",
        now,
        job.target_uid,
        job.job_id,
        job.lease_token,
      )
      .run();
    return Number(result.meta?.changes || 0) === 1;
  } catch {
    return false;
  }
}

async function updateCompleted(
  env: JobsEnv,
  job: AppOwnerMigrationJobRow,
  result: unknown,
  now: number,
): Promise<void> {
  const resultJson = JSON.stringify(result);
  await env.APP_DB.prepare(
    "UPDATE cf_app_owner_migration_jobs SET status = 'completed', lease_token = NULL, " +
      "lease_until = NULL, result_json = ?, last_error = NULL, updated_at = ? " +
      "WHERE target_uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
  )
    .bind(resultJson.slice(0, 65_536), now, job.target_uid, job.job_id, job.lease_token)
    .run();
}

export async function processAppOwnerMigrationMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  if (
    message.body.kind !== "app_owner_migration" ||
    !validText(message.body.uid, MAX_UID_LENGTH) ||
    !validText(message.body.jobId, MAX_JOB_ID_LENGTH)
  ) {
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1_000);
  const job = await jobById(env, message.body.uid, message.body.jobId);
  if (!job || job.status === "completed" || job.status === "failed") {
    message.ack();
    return;
  }
  if (job.next_attempt_at > now) {
    message.retry({ delaySeconds: Math.max(1, job.next_attempt_at - now) });
    return;
  }
  if (job.status === "running" && job.lease_until !== null && job.lease_until > now) {
    message.retry({ delaySeconds: Math.max(1, job.lease_until - now) });
    return;
  }
  if (
    (await deletionFence(env, job.source_uid)) ||
    (await deletionFence(env, job.target_uid))
  ) {
    message.ack();
    return;
  }
  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_app_owner_migration_jobs SET status = 'running', attempts = attempts + 1, " +
      "lease_token = ?, lease_until = ?, updated_at = ? WHERE target_uid = ? AND job_id = ? AND " +
      "((status = 'queued' AND next_attempt_at <= ?) OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))",
  )
    .bind(leaseToken, now + LEASE_SECONDS, now, job.target_uid, job.job_id, now, now)
    .run();
  if (Number(claimed.meta?.changes || 0) !== 1) {
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }
  const leased = await jobById(env, job.target_uid, job.job_id);
  if (!leased || leased.status !== "running" || leased.lease_token !== leaseToken) {
    message.ack();
    return;
  }
  const source = await sourceProjection(env, leased.source_uid);
  const generation = await targetGeneration(env, leased.target_uid);
  if (
    !source ||
    source.projection_status !== "imported" ||
    source.source_proof_hash !== leased.source_proof_hash ||
    source.source_projection_revision !== leased.source_projection_revision ||
    generation !== leased.target_account_generation
  ) {
    await updateFailed(env, leased, "migration authority changed", now);
    message.ack();
    return;
  }
  if (env.APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED !== "true") {
    await updateFailed(env, leased, "migration executor unavailable", now);
    message.ack();
    return;
  }
  let response: Response;
  try {
    response = await callProcessor(env, leased);
  } catch {
    if (leased.attempts >= MAX_ATTEMPTS || !(await updateRetry(env, leased, now))) {
      await updateFailed(env, leased, "migration executor unavailable", now);
      message.ack();
      return;
    }
    message.retry({ delaySeconds: retryDelay(leased.attempts) });
    return;
  }
  if (response.ok) {
    let result: unknown = { status: "completed" };
    try {
      const raw = await response.text();
      if (raw) result = JSON.parse(raw);
    } catch {
      await updateFailed(env, leased, "migration executor returned invalid result", now);
      message.ack();
      return;
    }
    await updateCompleted(env, leased, result, now);
    message.ack();
    return;
  }
  if (response.status === 400 || response.status === 404 || response.status === 409) {
    await updateFailed(env, leased, `migration executor rejected (${response.status})`, now);
    message.ack();
    return;
  }
  if (leased.attempts >= MAX_ATTEMPTS || !(await updateRetry(env, leased, now))) {
    await updateFailed(env, leased, "migration executor unavailable", now);
    message.ack();
    return;
  }
  message.retry({ delaySeconds: retryDelay(leased.attempts) });
}

export async function reconcileAppOwnerMigrationJobs(
  env: JobsEnv,
  now: number,
): Promise<void> {
  const result = await env.APP_DB.prepare(
    "SELECT job_id, source_uid, target_uid, source_proof_hash, source_projection_revision, " +
      "target_account_generation, idempotency_key, request_fingerprint, status, attempts, " +
      "lease_token, lease_until, next_attempt_at, last_error, result_json " +
      "FROM cf_app_owner_migration_jobs WHERE " +
      "(status = 'queued' AND next_attempt_at <= ?) OR " +
      "(status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?) " +
      "ORDER BY next_attempt_at, updated_at LIMIT ?",
  )
    .bind(now, now, RECONCILE_BATCH_SIZE)
    .all();
  for (const raw of result.results || []) {
    const job = parseJob(raw);
    if (!job || (await deletionFence(env, job.source_uid)) || (await deletionFence(env, job.target_uid))) {
      continue;
    }
    const repaired = await env.APP_DB.prepare(
      "UPDATE cf_app_owner_migration_jobs SET status = 'queued', lease_token = NULL, " +
        "lease_until = NULL, next_attempt_at = ?, updated_at = ? WHERE target_uid = ? AND job_id = ? AND " +
        "((status = 'queued' AND next_attempt_at <= ?) OR (status = 'running' AND lease_until <= ?))",
    )
      .bind(now, now, job.target_uid, job.job_id, now, now)
      .run();
    if (Number(repaired.meta?.changes || 0) !== 1) continue;
    if (
      !(await publish(env, {
        ...job,
        status: "queued",
        next_attempt_at: now,
        lease_token: null,
        lease_until: null,
      }))
    ) {
      try {
        await env.APP_DB.prepare(
          "UPDATE cf_app_owner_migration_jobs SET next_attempt_at = ?, last_error = ?, updated_at = ? " +
            "WHERE target_uid = ? AND job_id = ? AND status = 'queued'",
        )
          .bind(now + RETRY_DELAY_SECONDS, "queue unavailable", now, job.target_uid, job.job_id)
          .run();
      } catch {
        // The next scheduled reconciliation remains the recovery path.
      }
    }
  }
}

export function registerAppOwnerMigrationRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
): void {
  app.post(ROUTE_PATH, async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401, noStoreHeaders());
    return admit(c, context);
  });

  app.get(`${ROUTE_PATH}/:jobId`, async (c) => {
    if (
      c.env.APP_OWNER_MIGRATION_STAGING_ENABLED !== "true" ||
      c.env.APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED !== "true"
    ) {
      return c.json({ error: "app_owner_migration_unavailable" }, 503, noStoreHeaders());
    }
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401, noStoreHeaders());
    const jobId = c.req.param("jobId");
    if (!validText(jobId, MAX_JOB_ID_LENGTH)) {
      return c.json({ error: "invalid_request" }, 400, noStoreHeaders());
    }
    try {
      const job = await jobById(c.env, context.uid, jobId);
      if (!job) return c.json({ error: "not_found" }, 404, noStoreHeaders());
      return c.json(responseForJob(job), 200, noStoreHeaders());
    } catch {
      return c.json({ error: "app_owner_migration_unavailable" }, 503, noStoreHeaders());
    }
  });
}

export const appOwnerMigrationConstants = Object.freeze({
  routePath: ROUTE_PATH,
  processorPath: PROCESSOR_PATH,
  leaseSeconds: LEASE_SECONDS,
  maxAttempts: MAX_ATTEMPTS,
});
