import type { D1PreparedStatement, Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../shared/auth-context";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";

/**
 * Dormant Cloudflare seam for POST /v1/apps/migrate-owner.
 *
 * The legacy route accepts a Firebase anonymous credential and performs
 * Firestore ownership plus memory re-encryption side effects.  This module
 * accepts only a hash-only proof that a trusted import workflow has already
 * verified and projected.  It intentionally does not verify Firebase tokens
 * The staging executor below owns only the already-projected D1 app catalog
 * rows.  It does not claim to replay Firestore apps/personas or re-encrypt
 * legacy memories; those residuals remain an explicit production gate.
 *
 * The exact legacy route remains protected by the Persona/apps boundary.  The
 * namespaced Jobs route is independently feature-gated while
 * APP_OWNER_MIGRATION_STAGING_ENABLED and
 * APP_OWNER_MIGRATION_EXECUTOR_STAGING_ENABLED remain off by default.
 */

const ROUTE_PATH = "/v2/cf/apps/migrate-owner";
const IDENTITY_PROJECTION_PATH = `${ROUTE_PATH}/identity-projection`;
const AUTH_IDENTITY_PROJECTION_PATH =
  "/internal/firebase/anonymous-identity";
// Retained as a stable constant for callers that record the retired API Core
// processor boundary; the staging executor now performs the D1 projection in
// the Jobs worker itself.
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
const MAX_APP_PROJECTION_ROWS = 500;

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
  source_uid_hash: string;
  source_provider: "firebase-anonymous";
  source_proof_hash: string;
  source_projection_revision: string;
  projection_status: "imported" | "revoked" | "conflict";
  app_projection_count: number;
  memory_projection_count: number;
  target_uid: string;
  target_account_generation: number;
  source_credential_generation: number;
  attestation_expires_at: number;
};

type MigrationRequest = {
  sourceUid: string;
  sourceProofHash: string;
  idempotencyKey: string;
};

type IdentityProjectionRequest = {
  sourceUid: string;
  sourceToken: string;
};

type AnonymousIdentityAttestation = {
  target_uid: string;
  source_ref: string;
  source_uid_hash: string;
  source_proof_hash: string;
  source_credential_generation: number;
  source_projection_revision: string;
  attested_at: number;
  expires_at: number;
};

type AppCatalogOwnerRow = {
  id: string;
  owner_uid: string | null;
  owner_account_generation: number | null;
  owner_migration_job_id: string | null;
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
  const targetGeneration = integer(row.target_account_generation);
  const sourceGeneration = integer(row.source_credential_generation);
  const expiresAt = integer(row.attestation_expires_at);
  if (
    !validText(row.source_uid, MAX_UID_LENGTH) ||
    !row.source_uid.startsWith("fb-anon-") ||
    !validHash(row.source_uid_hash) ||
    row.source_uid !== `fb-anon-${row.source_uid_hash}` ||
    row.source_provider !== "firebase-anonymous" ||
    !validHash(row.source_proof_hash) ||
    !validText(row.source_projection_revision, MAX_REVISION_LENGTH) ||
    (row.projection_status !== "imported" &&
      row.projection_status !== "revoked" &&
      row.projection_status !== "conflict") ||
    appCount === null ||
    memoryCount === null ||
    !validText(row.target_uid, MAX_UID_LENGTH) ||
    targetGeneration === null ||
    sourceGeneration === null ||
    expiresAt === null
  ) {
    return null;
  }
  return {
    source_uid: row.source_uid,
    source_uid_hash: row.source_uid_hash,
    source_provider: row.source_provider,
    source_proof_hash: row.source_proof_hash,
    source_projection_revision: row.source_projection_revision,
    projection_status: row.projection_status,
    app_projection_count: appCount,
    memory_projection_count: memoryCount,
    target_uid: row.target_uid,
    target_account_generation: targetGeneration,
    source_credential_generation: sourceGeneration,
    attestation_expires_at: expiresAt,
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

class AppOwnerMigrationAuthorityError extends Error {}

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

function parseIdentityProjectionRequest(
  value: unknown,
): IdentityProjectionRequest | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const body = value as Record<string, unknown>;
  if (
    !validText(body.source_uid, MAX_UID_LENGTH) ||
    typeof body.source_token !== "string" ||
    body.source_token.length < 1 ||
    new TextEncoder().encode(body.source_token).byteLength > 8_192 ||
    /[\u0000-\u001f\u007f]/.test(body.source_token) ||
    "source_proof_hash" in body ||
    "source_uid_hash" in body ||
    "target_uid" in body
  ) {
    return null;
  }
  return { sourceUid: body.source_uid, sourceToken: body.source_token };
}

function parseAttestation(value: unknown): AnonymousIdentityAttestation | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Partial<AnonymousIdentityAttestation>;
  const sourceGeneration = integer(row.source_credential_generation);
  const attestedAt = integer(row.attested_at);
  const expiresAt = integer(row.expires_at);
  if (
    !validText(row.target_uid, MAX_UID_LENGTH) ||
    !validText(row.source_ref, MAX_UID_LENGTH) ||
    !row.source_ref.startsWith("fb-anon-") ||
    !validHash(row.source_uid_hash) ||
    row.source_ref !== `fb-anon-${row.source_uid_hash}` ||
    !validHash(row.source_proof_hash) ||
    !validHash(row.source_projection_revision) ||
    sourceGeneration === null ||
    attestedAt === null ||
    expiresAt === null ||
    expiresAt <= attestedAt
  ) {
    return null;
  }
  return {
    target_uid: row.target_uid,
    source_ref: row.source_ref,
    source_uid_hash: row.source_uid_hash,
    source_proof_hash: row.source_proof_hash,
    source_credential_generation: sourceGeneration,
    source_projection_revision: row.source_projection_revision,
    attested_at: attestedAt,
    expires_at: expiresAt,
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
    "SELECT source_uid, source_uid_hash, source_provider, source_proof_hash, source_projection_revision, " +
      "projection_status, app_projection_count, memory_projection_count, target_uid, " +
      "target_account_generation, source_credential_generation, attestation_expires_at " +
      "FROM cf_app_owner_migration_sources WHERE source_uid = ? LIMIT 1",
  )
    .bind(sourceUid)
    .first();
  return parseSource(row);
}

async function appCatalogRows(
  env: JobsEnv,
  source: AppOwnerMigrationSource,
  job: AppOwnerMigrationJobRow,
): Promise<{ sourceRows: AppCatalogOwnerRow[]; migratedRows: AppCatalogOwnerRow[] }> {
  if (source.app_projection_count > MAX_APP_PROJECTION_ROWS) {
    throw new AppOwnerMigrationAuthorityError("app projection exceeds executor limit");
  }
  const sourceResult = await env.APP_DB.prepare(
    "SELECT id, owner_uid, owner_account_generation, owner_migration_job_id " +
      "FROM cf_app_catalog WHERE owner_uid = ? ORDER BY id LIMIT ?",
  )
    .bind(source.source_uid, MAX_APP_PROJECTION_ROWS + 1)
    .all<AppCatalogOwnerRow>();
  const sourceRows = (sourceResult.results || []).map((row) => ({
    id: row.id,
    owner_uid: row.owner_uid ?? null,
    owner_account_generation:
      row.owner_account_generation === null || row.owner_account_generation === undefined
        ? null
        : integer(row.owner_account_generation),
    owner_migration_job_id: row.owner_migration_job_id ?? null,
  }));
  const migratedResult = await env.APP_DB.prepare(
    "SELECT id, owner_uid, owner_account_generation, owner_migration_job_id " +
      "FROM cf_app_catalog WHERE owner_migration_job_id = ? ORDER BY id LIMIT ?",
  )
    .bind(job.job_id, MAX_APP_PROJECTION_ROWS + 1)
    .all<AppCatalogOwnerRow>();
  const migratedRows = (migratedResult.results || []).map((row) => ({
    id: row.id,
    owner_uid: row.owner_uid ?? null,
    owner_account_generation:
      row.owner_account_generation === null || row.owner_account_generation === undefined
        ? null
        : integer(row.owner_account_generation),
    owner_migration_job_id: row.owner_migration_job_id ?? null,
  }));
  if (
    sourceRows.length > MAX_APP_PROJECTION_ROWS ||
    migratedRows.length > MAX_APP_PROJECTION_ROWS ||
    sourceRows.some(
      (row) =>
        row.owner_migration_job_id !== null ||
        row.owner_account_generation !== null ||
        row.owner_uid !== source.source_uid,
    ) ||
    migratedRows.some(
      (row) =>
        row.owner_uid !== job.target_uid ||
        row.owner_account_generation !== job.target_account_generation ||
        row.owner_migration_job_id !== job.job_id,
    )
  ) {
    throw new AppOwnerMigrationAuthorityError("app catalog authority changed");
  }
  const sourceIds = new Set(sourceRows.map((row) => row.id));
  const migratedIds = new Set(migratedRows.map((row) => row.id));
  if ([...sourceIds].some((id) => migratedIds.has(id))) {
    throw new AppOwnerMigrationAuthorityError("app catalog migration overlap");
  }
  if (sourceRows.length + migratedRows.length !== source.app_projection_count) {
    throw new AppOwnerMigrationAuthorityError("app catalog projection incomplete");
  }
  return { sourceRows, migratedRows };
}

function appIdList(ids: string[]): string {
  if (ids.length === 0 || ids.length > MAX_APP_PROJECTION_ROWS) {
    throw new AppOwnerMigrationAuthorityError("app catalog projection is invalid");
  }
  return ids.map(() => "?").join(", ");
}

async function executeAppOwnerD1Projection(
  env: JobsEnv,
  job: AppOwnerMigrationJobRow,
  source: AppOwnerMigrationSource,
  now: number,
): Promise<Record<string, unknown>> {
  if (await deletionFence(env, source.source_uid) || await deletionFence(env, job.target_uid)) {
    throw new AppOwnerMigrationAuthorityError("account deletion fence");
  }
  const generation = await targetGeneration(env, job.target_uid);
  if (generation !== job.target_account_generation) {
    throw new AppOwnerMigrationAuthorityError("target generation changed");
  }
  const { sourceRows, migratedRows } = await appCatalogRows(env, source, job);
  const appIds = [...new Set([...sourceRows, ...migratedRows].map((row) => row.id))];
  if (appIds.length === 0) {
    return {
      status: "completed",
      app_count: 0,
      memory_count: source.memory_projection_count,
      memory_reencryption: "not_migrated",
      account_generation: job.target_account_generation,
    };
  }
  const placeholders = appIdList(appIds);
  const statements: D1PreparedStatement[] = sourceRows.map((row) =>
    env.APP_DB.prepare(
      "UPDATE cf_app_catalog SET owner_uid = ?, owner_account_generation = ?, " +
        "owner_migration_job_id = ?, updated_at = ? WHERE id = ? AND owner_uid = ? " +
        "AND owner_account_generation IS NULL AND owner_migration_job_id IS NULL",
    ).bind(
      job.target_uid,
      job.target_account_generation,
      job.job_id,
      now,
      row.id,
      source.source_uid,
    ),
  );
  statements.push(
    env.APP_DB.prepare(
      `UPDATE cf_mcp_app_connections SET owner_uid = ?, revision = revision + 1, updated_at = ? WHERE owner_uid = ? AND app_id IN (${placeholders})`,
    ).bind(job.target_uid, now, source.source_uid, ...appIds),
    env.APP_DB.prepare(
      `UPDATE cf_mcp_app_discoveries SET owner_uid = ?, revision = revision + 1, updated_at = ? WHERE owner_uid = ? AND app_id IN (${placeholders})`,
    ).bind(job.target_uid, now, source.source_uid, ...appIds),
    env.APP_DB.prepare(
      `UPDATE cf_mcp_app_oauth_transactions SET owner_uid = ?, status = CASE WHEN status = 'pending' THEN 'failed' ELSE status END, last_error = CASE WHEN status = 'pending' THEN 'owner migrated' ELSE last_error END, updated_at = ? WHERE owner_uid = ? AND app_id IN (${placeholders})`,
    ).bind(job.target_uid, now, source.source_uid, ...appIds),
    env.APP_DB.prepare(
      `UPDATE cf_app_payment_links SET owner_uid = ?, updated_at = ? WHERE owner_uid = ? AND app_id IN (${placeholders})`,
    ).bind(job.target_uid, now, source.source_uid, ...appIds),
  );
  const results = await env.APP_DB.batch(statements);
  const catalogResults = results.slice(0, sourceRows.length);
  if (
    catalogResults.some((result) => Number(result.meta?.changes || 0) !== 1)
  ) {
    throw new AppOwnerMigrationAuthorityError("app catalog CAS lost");
  }
  return {
    status: "completed",
    app_count: appIds.length,
    app_ids: appIds,
    memory_count: source.memory_projection_count,
    memory_reencryption: "not_migrated",
    account_generation: job.target_account_generation,
  };
}

async function sourceProjectionByHash(
  env: JobsEnv,
  sourceUidHash: string,
): Promise<AppOwnerMigrationSource | null> {
  const row = await env.APP_DB.prepare(
    "SELECT source_uid, source_uid_hash, source_provider, source_proof_hash, source_projection_revision, " +
      "projection_status, app_projection_count, memory_projection_count, target_uid, " +
      "target_account_generation, source_credential_generation, attestation_expires_at " +
      "FROM cf_app_owner_migration_sources WHERE source_uid_hash = ? LIMIT 1",
  )
    .bind(sourceUidHash)
    .first();
  return parseSource(row);
}

async function verifyAnonymousSource(
  env: JobsEnv,
  context: SignedAuthContext,
  request: IdentityProjectionRequest,
): Promise<AnonymousIdentityAttestation> {
  const signed = await createSignedAuthContext(
    {
      uid: context.uid,
      authority: "internal",
      requestId: context.requestId,
    },
    "auth",
    "POST",
    AUTH_IDENTITY_PROJECTION_PATH,
    env.INTERNAL_ASSERTION_SECRET,
  );
  if (!signed) throw new Error("identity bridge unavailable");
  const response = await env.AUTH.fetch(
    new Request(`https://auth.internal${AUTH_IDENTITY_PROJECTION_PATH}`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${request.sourceToken}`,
        "content-type": "application/json",
        [AUTH_CONTEXT_HEADER]: signed.encoded,
        [AUTH_SIGNATURE_HEADER]: signed.signature,
        "x-request-id": context.requestId,
      },
      body: JSON.stringify({ expected_source_uid: request.sourceUid }),
    }),
  );
  let body: unknown;
  try {
    const raw = await response.text();
    if (new TextEncoder().encode(raw).byteLength > 16_384) {
      throw new Error("identity response too large");
    }
    body = JSON.parse(raw);
  } catch {
    throw new Error("identity bridge unavailable");
  }
  if (!response.ok) {
    const error =
      body && typeof body === "object" && !Array.isArray(body)
        ? (body as { error?: unknown }).error
        : null;
    if (
      response.status === 403 &&
      (error === "source_identity_rejected" ||
        error === "source_identity_mismatch" ||
        error === "source_identity_revoked")
    ) {
      throw new Error(String(error));
    }
    throw new Error("identity bridge unavailable");
  }
  const attestation = parseAttestation(body);
  if (!attestation || attestation.target_uid !== context.uid) {
    throw new Error("identity bridge unavailable");
  }
  return attestation;
}

async function projectAnonymousIdentity(
  c: JobsContext,
  context: SignedAuthContext,
): Promise<Response> {
  if (c.env.FIREBASE_IDENTITY_PROJECTION_STAGING_ENABLED !== "true") {
    return c.json(
      { error: "firebase_identity_projection_unavailable" },
      503,
      noStoreHeaders(),
    );
  }
  if (context.authority !== "better-auth") {
    return c.json({ error: "better_auth_required" }, 403, noStoreHeaders());
  }
  let body: unknown;
  try {
    body = await readBoundedJson(c.req.raw);
  } catch {
    return c.json({ error: "invalid_request" }, 400, noStoreHeaders());
  }
  const request = parseIdentityProjectionRequest(body);
  if (!request || request.sourceUid === context.uid) {
    return c.json({ error: "invalid_request" }, 422, noStoreHeaders());
  }
  try {
    if (await deletionFence(c.env, context.uid)) {
      return c.json(
        { error: "account_deletion_in_progress" },
        409,
        noStoreHeaders(),
      );
    }
    const generation = await targetGeneration(c.env, context.uid);
    if (generation === null) {
      return c.json(
        {
          error: "firebase_identity_projection_unavailable",
          reason: "target_not_admitted",
        },
        503,
        noStoreHeaders(),
      );
    }
    const attestation = await verifyAnonymousSource(c.env, context, request);
    const now = Math.floor(Date.now() / 1_000);
    if (
      attestation.attested_at < now - 120 ||
      attestation.attested_at > now + 5 ||
      attestation.expires_at <= now ||
      (await deletionFence(c.env, context.uid)) ||
      (await targetGeneration(c.env, context.uid)) !== generation
    ) {
      return c.json(
        { error: "firebase_identity_projection_unavailable" },
        503,
        noStoreHeaders(),
      );
    }
    const existing = await sourceProjectionByHash(
      c.env,
      attestation.source_uid_hash,
    );
    if (existing) {
      if (
        existing.target_uid !== context.uid ||
        existing.target_account_generation !== generation
      ) {
        return c.json(
          { error: "firebase_identity_projection_conflict" },
          409,
          noStoreHeaders(),
        );
      }
      if (
        existing.projection_status === "imported" &&
        existing.source_proof_hash === attestation.source_proof_hash &&
        existing.source_projection_revision ===
          attestation.source_projection_revision &&
        existing.source_credential_generation ===
          attestation.source_credential_generation
      ) {
        return c.json(
          {
            source_ref: existing.source_uid,
            source_proof_hash: existing.source_proof_hash,
            source_projection_revision:
              existing.source_projection_revision,
            target_account_generation: generation,
            status: "imported",
          },
          200,
          noStoreHeaders(),
        );
      }
      await c.env.APP_DB.prepare(
        "UPDATE cf_app_owner_migration_sources SET projection_status = 'conflict', updated_at = ? " +
          "WHERE source_uid_hash = ? AND target_uid = ? AND target_account_generation = ? " +
          "AND projection_status = 'imported'",
      )
        .bind(now, attestation.source_uid_hash, context.uid, generation)
        .run();
      return c.json(
        { error: "firebase_identity_projection_conflict" },
        409,
        noStoreHeaders(),
      );
    }
    const inserted = await c.env.APP_DB.prepare(
      "INSERT INTO cf_app_owner_migration_sources " +
        "(source_uid, source_uid_hash, source_provider, source_proof_hash, source_projection_revision, " +
        "projection_status, app_projection_count, memory_projection_count, target_uid, " +
        "target_account_generation, source_credential_generation, attestation_expires_at, " +
        "imported_at, updated_at) VALUES (?, ?, 'firebase-anonymous', ?, ?, 'imported', 0, 0, ?, ?, ?, ?, ?, ?)",
    )
      .bind(
        attestation.source_ref,
        attestation.source_uid_hash,
        attestation.source_proof_hash,
        attestation.source_projection_revision,
        context.uid,
        generation,
        attestation.source_credential_generation,
        attestation.expires_at,
        now,
        now,
      )
      .run();
    if (Number(inserted.meta?.changes || 0) !== 1) {
      return c.json(
        { error: "firebase_identity_projection_conflict" },
        409,
        noStoreHeaders(),
      );
    }
    return c.json(
      {
        source_ref: attestation.source_ref,
        source_proof_hash: attestation.source_proof_hash,
        source_projection_revision: attestation.source_projection_revision,
        target_account_generation: generation,
        status: "imported",
      },
      201,
      noStoreHeaders(),
    );
  } catch (error) {
    if (
      error instanceof Error &&
      (error.message === "source_identity_rejected" ||
        error.message === "source_identity_mismatch" ||
        error.message === "source_identity_revoked")
    ) {
      return c.json(
        { error: error.message },
        403,
        noStoreHeaders(),
      );
    }
    return c.json(
      { error: "firebase_identity_projection_unavailable" },
      503,
      noStoreHeaders(),
    );
  }
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
    if (await deletionFence(c.env, context.uid)) {
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
    const now = Math.floor(Date.now() / 1_000);
    if (
      source &&
      (source.target_uid !== context.uid ||
        source.target_account_generation !== generation)
    ) {
      return c.json(
        { error: "migration_request_conflict" },
        409,
        noStoreHeaders(),
      );
    }
    if (
      !source ||
      source.projection_status !== "imported" ||
      source.source_proof_hash !== request.sourceProofHash ||
      source.attestation_expires_at <= now
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
    source.target_uid !== leased.target_uid ||
    source.target_account_generation !== leased.target_account_generation ||
    source.attestation_expires_at <= now ||
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
  let result: Record<string, unknown>;
  try {
    result = await executeAppOwnerD1Projection(env, leased, source, now);
  } catch (error) {
    if (error instanceof AppOwnerMigrationAuthorityError) {
      await updateFailed(env, leased, error.message, now);
      message.ack();
      return;
    }
    if (leased.attempts >= MAX_ATTEMPTS || !(await updateRetry(env, leased, now))) {
      await updateFailed(env, leased, "migration executor unavailable", now);
      message.ack();
      return;
    }
    message.retry({ delaySeconds: retryDelay(leased.attempts) });
    return;
  }
  await updateCompleted(env, leased, result, now);
  message.ack();
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
  app.post(IDENTITY_PROJECTION_PATH, async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401, noStoreHeaders());
    return projectAnonymousIdentity(c, context);
  });

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
  identityProjectionPath: IDENTITY_PROJECTION_PATH,
  processorPath: PROCESSOR_PATH,
  leaseSeconds: LEASE_SECONDS,
  maxAttempts: MAX_ATTEMPTS,
});
