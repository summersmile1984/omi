import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

const MAX_UID_LENGTH = 256;
const MAX_RUN_ID_LENGTH = 256;
const MAX_LIFECYCLE_LIMIT = 1_000;
const DEFAULT_LIFECYCLE_LIMIT = 500;
const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const SHORT_TERM_TTL_SECONDS = 48 * 60 * 60;
const MAX_EVIDENCE_BYTES = 65_536;
const TRANSITION_BATCH_SIZE = 100;
const RUN_ID = /^[^/\u0000]{1,256}$/;
const CONTROL_SOURCE = "cloudflare_short_term_lifecycle_projection";
const LIFECYCLE_PROJECTION_REVISION = "memory-lifecycle-projection-v2";

type JobsContext = Context<{ Bindings: JobsEnv }>;

type LifecycleControl = {
  uid: string;
  schema_version: number;
  source: string;
  enabled: number;
  executor_state: string;
  account_generation: number;
};

type LifecycleRun = {
  uid: string;
  run_id: string;
  request_fingerprint: string;
  evaluated_at: number;
  requested_limit: number;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  lease_until: number | null;
  result_json: string | null;
  account_generation: number;
};

type LifecycleMemory = {
  uid: string;
  id: string;
  content: string;
  status: "active" | "superseded" | "hidden" | "tombstoned";
  processing_state: "pending" | "processed" | "blocked";
  source_state: "active" | "tombstoned" | "purged";
  captured_at: number;
  expires_at: number | null;
  conversation_id: string | null;
  evidence_json: string;
  sensitivity_labels_json: string;
  account_generation: number;
};

type LifecycleTransition = {
  transitionId: string;
  memoryId: string;
  outcome: "remain_short_term" | "source_tombstoned";
  reason: string;
  evaluatedAt: string;
  auditMetadataJson: string;
  idempotencyKey: string;
  fingerprint: string;
};

type LifecycleResult = {
  uid: string;
  run_id: string;
  evaluated_at: string;
  evaluated_count: number;
  created_count: number;
  existing_count: number;
  skipped_count: number;
  skipped_memory_ids: string[];
  default_access_allowed: false;
  archive_default_visible: false;
};

class LifecycleTerminalError extends Error {}

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let difference = leftBytes.length ^ rightBytes.length;
  const maximum = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < maximum; index += 1) {
    difference |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return difference === 0;
}

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  return Boolean(expected && provided && constantTimeEqual(provided, expected));
}

function validUid(uid: string): boolean {
  return uid.length > 0 && uid.length <= MAX_UID_LENGTH && !uid.includes("/") && !uid.includes("\u0000");
}

function invalidRequest(c: JobsContext, detail: string, status = 400): Response {
  return c.json({ error: "invalid_request", detail }, status as 400);
}

function lifecycleUnavailable(c: JobsContext, reason: string, status = 503): Response {
  return c.json({ error: "short_term_lifecycle_unavailable", reason }, status as 503);
}

function strictInteger(value: unknown, minimum: number, maximum: number): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : null;
}

function parseEvaluationTime(value: string | undefined): { epoch: number; iso: string } | null {
  if (!value) {
    const now = Math.floor(Date.now() / 1_000);
    return { epoch: now, iso: new Date(now * 1_000).toISOString() };
  }
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return null;
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return null;
  const epoch = Math.floor(milliseconds / 1_000);
  return { epoch, iso: new Date(epoch * 1_000).toISOString() };
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
}

function isoTimestamp(epoch: number): string {
  return new Date(epoch * 1_000).toISOString().replace(".000Z", "+00:00");
}

function effectiveExpiry(memory: LifecycleMemory): number {
  const policyExpiry = memory.captured_at + SHORT_TERM_TTL_SECONDS;
  return memory.expires_at === null ? policyExpiry : Math.min(memory.expires_at, policyExpiry);
}

function parseLifecycleMemory(row: unknown): LifecycleMemory | null {
  if (!row || typeof row !== "object") return null;
  const value = row as Partial<LifecycleMemory>;
  const capturedAt = strictInteger(value.captured_at, 0, Number.MAX_SAFE_INTEGER);
  const expiresAt = value.expires_at === null || value.expires_at === undefined
    ? null
    : strictInteger(value.expires_at, 0, Number.MAX_SAFE_INTEGER);
  const generation = strictInteger(value.account_generation, 0, Number.MAX_SAFE_INTEGER);
  const statuses = new Set(["active", "superseded", "hidden", "tombstoned"]);
  const processingStates = new Set(["pending", "processed", "blocked"]);
  const sourceStates = new Set(["active", "tombstoned", "purged"]);
  if (
    typeof value.uid !== "string" || !validUid(value.uid) ||
    typeof value.id !== "string" || !RUN_ID.test(value.id) ||
    typeof value.content !== "string" || value.content.length === 0 || value.content.length > 50_000 ||
    typeof value.status !== "string" || !statuses.has(value.status) ||
    typeof value.processing_state !== "string" || !processingStates.has(value.processing_state) ||
    typeof value.source_state !== "string" || !sourceStates.has(value.source_state) ||
    capturedAt === null || expiresAt === undefined || generation === null ||
    typeof value.evidence_json !== "string" || value.evidence_json.length > MAX_EVIDENCE_BYTES ||
    typeof value.sensitivity_labels_json !== "string" || value.sensitivity_labels_json.length > 4_096 ||
    (value.conversation_id !== null && value.conversation_id !== undefined && typeof value.conversation_id !== "string")
  ) return null;
  return {
    uid: value.uid,
    id: value.id,
    content: value.content,
    status: value.status as LifecycleMemory["status"],
    processing_state: value.processing_state as LifecycleMemory["processing_state"],
    source_state: value.source_state as LifecycleMemory["source_state"],
    captured_at: capturedAt,
    expires_at: expiresAt,
    conversation_id: value.conversation_id === undefined ? null : value.conversation_id,
    evidence_json: value.evidence_json,
    sensitivity_labels_json: value.sensitivity_labels_json,
    account_generation: generation,
  };
}

function sourceRefs(memory: LifecycleMemory): Array<Record<string, unknown>> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(memory.evidence_json);
  } catch {
    throw new Error("malformed lifecycle evidence projection");
  }
  if (!Array.isArray(parsed)) throw new Error("malformed lifecycle evidence projection");
  const refs: Array<Record<string, unknown>> = [];
  for (const evidence of parsed) {
    if (!evidence || typeof evidence !== "object" || Array.isArray(evidence)) {
      throw new Error("malformed lifecycle evidence projection");
    }
    const value = evidence as Record<string, unknown>;
    refs.push({
      evidence_id: typeof value.evidence_id === "string" ? value.evidence_id : null,
      source_id: typeof value.source_id === "string" ? value.source_id : null,
      source_type: typeof value.source_type === "string" ? value.source_type : null,
      source_version: typeof value.source_version === "string" ? value.source_version : null,
      source_state: typeof value.source_state === "string" ? value.source_state : memory.source_state,
    });
  }
  if (refs.length === 0 && memory.conversation_id) {
    refs.push({
      evidence_id: null,
      source_id: memory.conversation_id,
      source_type: "conversation",
      source_version: null,
      source_state: memory.source_state,
    });
  }
  return refs;
}

async function buildLifecycleTransition(
  memory: LifecycleMemory,
  evaluatedAt: number,
  runId: string,
): Promise<LifecycleTransition> {
  const expiryAt = effectiveExpiry(memory);
  const evaluatedIso = isoTimestamp(evaluatedAt);
  const expiryIso = isoTimestamp(expiryAt);
  const sourceState = memory.source_state;
  const tombstoned = sourceState === "tombstoned" || sourceState === "purged";
  const outcome: LifecycleTransition["outcome"] = tombstoned ? "source_tombstoned" : "remain_short_term";
  const reason = tombstoned
    ? "source_tombstoned"
    : "short_term_expired_requires_lifecycle_decision";
  const auditMetadata: Record<string, unknown> = {
    policy_version: "short_term_lifecycle.v1",
    memory_id: memory.id,
    uid: memory.uid,
    tier: "short_term",
    status: memory.status,
    processing_state: memory.processing_state,
    source_state: sourceState,
    captured_at: isoTimestamp(memory.captured_at),
    expires_at: expiryIso,
    evaluated_at: evaluatedIso,
    disposition: null,
    decision_reason: reason,
    outcome,
    requires_lifecycle_decision: !tombstoned,
    default_access_allowed: false,
    source_refs: sourceRefs(memory),
  };
  const idempotencyPayload = {
    policy_version: "short_term_lifecycle.v1",
    uid: memory.uid,
    memory_item_id: memory.id,
    outcome,
    reason,
    evaluated_at: evaluatedIso,
    source_refs: auditMetadata.source_refs,
  };
  const idempotencyDigest = await sha256Hex(canonicalJson(idempotencyPayload));
  const idempotencyKey = `short-term-lifecycle:${memory.uid}:${memory.id}:${outcome}:${idempotencyDigest}`;
  const transitionId = `stl_${(await sha256Hex(`${memory.uid}:${idempotencyKey}`)).slice(0, 32)}`;
  const fingerprint = await sha256Hex(canonicalJson({
    uid: memory.uid,
    memory_item_id: memory.id,
    outcome,
    reason,
    run_id: runId,
    audit_metadata: auditMetadata,
  }));
  return {
    transitionId,
    memoryId: memory.id,
    outcome,
    reason,
    evaluatedAt: evaluatedIso,
    auditMetadataJson: canonicalJson(auditMetadata),
    idempotencyKey,
    fingerprint,
  };
}

async function accountFence(c: JobsContext, uid: string, now: number): Promise<boolean> {
  const row = await c.env.APP_DB.prepare(
    "SELECT lifecycle FROM (" +
      "SELECT 'deleting' AS lifecycle, 0 AS priority FROM cf_account_deletion_intents WHERE uid = ? " +
      "UNION ALL SELECT 'deleted' AS lifecycle, 1 AS priority FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?" +
      ") ORDER BY priority LIMIT 1",
  )
    .bind(uid, uid, now)
    .first<{ lifecycle?: unknown }>();
  if (row === null) return false;
  if (row && (row.lifecycle === "deleting" || row.lifecycle === "deleted")) return true;
  throw new Error("malformed account deletion fence");
}

async function currentAuthority(c: JobsContext, uid: string, now: number): Promise<number | Response> {
  if (await accountFence(c, uid, now)) return lifecycleUnavailable(c, "account_deletion_in_progress", 409);

  const cutover = await c.env.APP_DB.prepare(
    "SELECT uid, schema_version, state, account_generation, checkpoint_phase, destination_backend_bound " +
      "FROM cf_account_cutover WHERE uid = ?",
  )
    .bind(uid)
    .first<Record<string, unknown>>();
  const generation = strictInteger(cutover?.account_generation, 0, Number.MAX_SAFE_INTEGER);
  if (
    !cutover ||
    cutover.uid !== uid ||
    cutover.schema_version !== 1 ||
    cutover.state !== "new" ||
    cutover.checkpoint_phase !== "completed" ||
    cutover.destination_backend_bound !== 1 ||
    generation === null
  ) {
    return lifecycleUnavailable(c, "missing_completed_cutover");
  }

  // A completed, destination-bound cutover is the only capability writer for
  // this staging authority.  Materialize the lifecycle control row once from
  // that signed control plane; an existing operator-disabled row is never
  // silently upgraded by a request.
  try {
    await c.env.APP_DB.prepare(
      "SELECT captured_at, expires_at, status, processing_state, source_state, evidence_json, account_generation " +
        "FROM cf_memories LIMIT 0",
    ).all();
    await c.env.APP_DB.prepare(
      "INSERT INTO cf_memory_short_term_lifecycle_control " +
        "(uid, schema_version, source, enabled, executor_state, account_generation, source_revision, updated_at) " +
        "VALUES (?, 1, ?, 1, 'ready', ?, ?, ?) ON CONFLICT(uid) DO NOTHING",
    )
      .bind(uid, CONTROL_SOURCE, generation, LIFECYCLE_PROJECTION_REVISION, now)
      .run();
  } catch (error) {
    if (error instanceof Error && error.message.includes("account deletion fence")) {
      return lifecycleUnavailable(c, "account_deletion_in_progress", 409);
    }
    return lifecycleUnavailable(c, "lifecycle_schema_unavailable");
  }

  const control = await c.env.APP_DB.prepare(
    "SELECT uid, schema_version, source, enabled, executor_state, account_generation " +
      "FROM cf_memory_short_term_lifecycle_control WHERE uid = ?",
  )
    .bind(uid)
    .first<LifecycleControl>();
  if (
    !control ||
    control.uid !== uid ||
    control.schema_version !== 1 ||
    control.source !== CONTROL_SOURCE ||
    control.enabled !== 1 ||
    control.executor_state !== "ready" ||
    control.account_generation !== generation
  ) {
    return lifecycleUnavailable(c, "missing_ready_lifecycle_authority");
  }
  return generation;
}

function parseRun(row: unknown): LifecycleRun | null {
  if (!row || typeof row !== "object") return null;
  const value = row as Partial<LifecycleRun>;
  const evaluatedAt = strictInteger(value.evaluated_at, 0, Number.MAX_SAFE_INTEGER);
  const requestedLimit = strictInteger(value.requested_limit, 1, MAX_LIFECYCLE_LIMIT);
  const attempts = strictInteger(value.attempts, 0, Number.MAX_SAFE_INTEGER);
  const generation = strictInteger(value.account_generation, 0, Number.MAX_SAFE_INTEGER);
  const leaseUntil = value.lease_until === null || value.lease_until === undefined
    ? null
    : strictInteger(value.lease_until, 0, Number.MAX_SAFE_INTEGER);
  if (
    typeof value.uid !== "string" || !validUid(value.uid) ||
    typeof value.run_id !== "string" || !RUN_ID.test(value.run_id) ||
    typeof value.request_fingerprint !== "string" || !/^[a-f0-9]{64}$/.test(value.request_fingerprint) ||
    evaluatedAt === null || requestedLimit === null || attempts === null || generation === null ||
    (value.status !== "queued" && value.status !== "running" && value.status !== "completed" && value.status !== "failed") ||
    (value.lease_until !== null && value.lease_until !== undefined && leaseUntil === null) ||
    (value.result_json !== null && value.result_json !== undefined && typeof value.result_json !== "string")
  ) return null;
  return {
    uid: value.uid,
    run_id: value.run_id,
    request_fingerprint: value.request_fingerprint,
    evaluated_at: evaluatedAt,
    requested_limit: requestedLimit,
    status: value.status,
    attempts,
    lease_until: leaseUntil,
    result_json: value.result_json === undefined ? null : value.result_json,
    account_generation: generation,
  };
}

function lifecycleMessage(run: LifecycleRun): JobMessage {
  return {
    jobId: `memory-stl-${run.request_fingerprint.slice(0, 48)}`,
    uid: run.uid,
    kind: "memory_short_term_lifecycle",
    payload: {
      runId: run.run_id,
      requestFingerprint: run.request_fingerprint,
      accountGeneration: run.account_generation,
    },
  };
}

function parseMessagePayload(payload: Record<string, unknown>): {
  runId: string;
  requestFingerprint: string;
  accountGeneration: number;
} | null {
  const runId = typeof payload.runId === "string" ? payload.runId : "";
  const requestFingerprint = typeof payload.requestFingerprint === "string" ? payload.requestFingerprint : "";
  const accountGeneration = strictInteger(payload.accountGeneration, 0, Number.MAX_SAFE_INTEGER);
  if (!RUN_ID.test(runId) || !/^[a-f0-9]{64}$/.test(requestFingerprint) || accountGeneration === null) return null;
  return { runId, requestFingerprint, accountGeneration };
}

async function failRun(env: JobsEnv, run: LifecycleRun, reason: string, now: number): Promise<void> {
  try {
    await env.APP_DB.prepare(
      "UPDATE cf_memory_short_term_lifecycle_runs SET status = 'failed', lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ? " +
        "WHERE uid = ? AND run_id = ? AND status <> 'completed'",
    )
      .bind(reason, now, run.uid, run.run_id)
      .run();
  } catch {
    // A concurrent deletion fence is allowed to remove the row; the message
    // must not retry into an account that is being purged.
  }
}

async function retryRun(
  env: JobsEnv,
  run: LifecycleRun,
  leaseToken: string,
  reason: string,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_memory_short_term_lifecycle_runs SET status = 'queued', lease_token = NULL, lease_until = NULL, " +
      "next_attempt_at = ?, last_error = ?, updated_at = ? " +
      "WHERE uid = ? AND run_id = ? AND status = 'running' AND lease_token = ?",
  )
    .bind(now + RETRY_DELAY_SECONDS, reason, now, run.uid, run.run_id, leaseToken)
    .run();
}

async function readReadyControl(
  env: JobsEnv,
  uid: string,
  generation: number,
): Promise<boolean> {
  const control = await env.APP_DB.prepare(
    "SELECT uid, schema_version, source, enabled, executor_state, account_generation " +
      "FROM cf_memory_short_term_lifecycle_control WHERE uid = ?",
  )
    .bind(uid)
    .first<LifecycleControl>();
  return Boolean(
    control && control.uid === uid && control.schema_version === 1 && control.source === CONTROL_SOURCE &&
      control.enabled === 1 && control.executor_state === "ready" && control.account_generation === generation,
  );
}

async function readLifecycleMemories(
  env: JobsEnv,
  run: LifecycleRun,
): Promise<LifecycleMemory[]> {
  const result = await env.APP_DB.prepare(
    "SELECT uid, id, content, status, processing_state, source_state, captured_at, expires_at, conversation_id, " +
      "evidence_json, sensitivity_labels_json, account_generation " +
      "FROM cf_memories WHERE uid = ? AND account_generation = ? AND memory_tier = 'short_term' " +
      "AND status = 'active' AND processing_state = 'processed' AND deleted_at IS NULL AND invalid_at IS NULL " +
      "AND CASE WHEN expires_at IS NULL OR expires_at > captured_at + ? " +
      "THEN captured_at + ? ELSE expires_at END <= ? " +
      "ORDER BY CASE WHEN expires_at IS NULL OR expires_at > captured_at + ? " +
      "THEN captured_at + ? ELSE expires_at END ASC, id ASC LIMIT ?",
  )
    .bind(
      run.uid,
      run.account_generation,
      SHORT_TERM_TTL_SECONDS,
      SHORT_TERM_TTL_SECONDS,
      run.evaluated_at,
      SHORT_TERM_TTL_SECONDS,
      SHORT_TERM_TTL_SECONDS,
      run.requested_limit,
    )
    .all<{ results?: unknown[] }>();
  const rows = Array.isArray(result?.results) ? result.results : [];
  const memories: LifecycleMemory[] = [];
  for (const row of rows) {
    const memory = parseLifecycleMemory(row);
    if (!memory || memory.uid !== run.uid || memory.account_generation !== run.account_generation) {
      throw new LifecycleTerminalError("lifecycle_memory_projection_invalid");
    }
    if (effectiveExpiry(memory) > run.evaluated_at) {
      throw new LifecycleTerminalError("lifecycle_memory_expiry_projection_invalid");
    }
    memories.push(memory);
  }
  return memories;
}

async function executeLifecycleRun(
  env: JobsEnv,
  run: LifecycleRun,
  leaseToken: string,
): Promise<LifecycleResult> {
  const now = Math.floor(Date.now() / 1_000);
  if (await accountFence({ env } as JobsContext, run.uid, now)) {
    throw new LifecycleTerminalError("account_deletion_in_progress");
  }
  const cutover = await env.APP_DB.prepare(
    "SELECT uid, schema_version, state, account_generation, checkpoint_phase, destination_backend_bound " +
      "FROM cf_account_cutover WHERE uid = ?",
  )
    .bind(run.uid)
    .first<Record<string, unknown>>();
  if (
    !cutover || cutover.uid !== run.uid || cutover.schema_version !== 1 || cutover.state !== "new" ||
    cutover.checkpoint_phase !== "completed" || cutover.destination_backend_bound !== 1 ||
    strictInteger(cutover.account_generation, 0, Number.MAX_SAFE_INTEGER) !== run.account_generation
  ) {
    throw new LifecycleTerminalError("lifecycle_cutover_changed");
  }
  if (!(await readReadyControl(env, run.uid, run.account_generation))) {
    throw new LifecycleTerminalError("lifecycle_authority_unavailable");
  }
  const memories = await readLifecycleMemories(env, run);
  const transitions = await Promise.all(
    memories.map((memory) => buildLifecycleTransition(memory, run.evaluated_at, run.run_id)),
  );
  const statements: Array<ReturnType<JobsEnv["APP_DB"]["prepare"]>> = [];
  for (const transition of transitions) {
    if (new TextEncoder().encode(transition.auditMetadataJson).byteLength > MAX_EVIDENCE_BYTES) {
      throw new LifecycleTerminalError("lifecycle_transition_metadata_too_large");
    }
    statements.push(
      env.APP_DB.prepare(
        "INSERT INTO cf_memory_short_term_lifecycle_transitions " +
          "(uid, transition_id, memory_id, run_id, outcome, reason, evaluated_at, audit_metadata_json, " +
          "idempotency_key, fingerprint, account_generation, created_at) " +
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) " +
          "ON CONFLICT(uid, idempotency_key) DO NOTHING",
      ).bind(
        run.uid,
        transition.transitionId,
        transition.memoryId,
        run.run_id,
        transition.outcome,
        transition.reason,
        transition.evaluatedAt,
        transition.auditMetadataJson,
        transition.idempotencyKey,
        transition.fingerprint,
        run.account_generation,
        now,
      ) as ReturnType<JobsEnv["APP_DB"]["prepare"]>,
    );
  }
  let createdCount = 0;
  for (let offset = 0; offset < statements.length; offset += TRANSITION_BATCH_SIZE) {
    const batchResult = await env.APP_DB.batch(statements.slice(offset, offset + TRANSITION_BATCH_SIZE));
    for (const result of batchResult) {
      const changes = Number((result as { meta?: { changes?: unknown } })?.meta?.changes ?? 0);
      if (changes === 1) createdCount += 1;
      else if (changes !== 0) throw new Error("invalid lifecycle transition result");
    }
  }
  const result: LifecycleResult = {
    uid: run.uid,
    run_id: run.run_id,
    evaluated_at: isoTimestamp(run.evaluated_at),
    evaluated_count: memories.length,
    created_count: createdCount,
    existing_count: memories.length - createdCount,
    skipped_count: 0,
    skipped_memory_ids: [],
    default_access_allowed: false,
    archive_default_visible: false,
  };
  const completed = await env.APP_DB.prepare(
    "UPDATE cf_memory_short_term_lifecycle_runs SET status = 'completed', lease_token = NULL, lease_until = NULL, " +
      "next_attempt_at = ?, result_json = ?, last_error = NULL, updated_at = ? " +
      "WHERE uid = ? AND run_id = ? AND status = 'running' AND lease_token = ?",
  )
    .bind(now, JSON.stringify(result), now, run.uid, run.run_id, leaseToken)
    .run();
  if (completed.meta?.changes !== 1) throw new Error("lifecycle completion lease lost");
  return result;
}

function storedLifecycleResult(run: LifecycleRun): LifecycleResult | null {
  if (!run.result_json) return null;
  try {
    const value = JSON.parse(run.result_json) as Partial<LifecycleResult>;
    if (
      value.uid !== run.uid || value.run_id !== run.run_id || typeof value.evaluated_at !== "string" ||
      !Number.isSafeInteger(value.evaluated_count) || !Number.isSafeInteger(value.created_count) ||
      !Number.isSafeInteger(value.existing_count) || !Number.isSafeInteger(value.skipped_count) ||
      !Array.isArray(value.skipped_memory_ids) || value.default_access_allowed !== false ||
      value.archive_default_visible !== false
    ) return null;
    return value as LifecycleResult;
  } catch {
    return null;
  }
}

async function claimLifecycleRun(
  env: JobsEnv,
  run: LifecycleRun,
  now: number,
): Promise<string | null> {
  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_memory_short_term_lifecycle_runs SET status = 'running', attempts = attempts + 1, " +
      "lease_token = ?, lease_until = ?, updated_at = ? " +
      "WHERE uid = ? AND run_id = ? AND account_generation = ? AND " +
      "(status = 'queued' OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))",
  )
    .bind(leaseToken, now + LEASE_SECONDS, now, run.uid, run.run_id, run.account_generation, now)
    .run();
  return claimed.meta?.changes === 1 ? leaseToken : null;
}

/**
 * Shadow boundary for the legacy admin lifecycle entrypoint.
 *
 * The endpoint is intentionally not in the Edge/route manifest yet.  It only
 * admits a run when a separately populated D1 control row proves that the
 * lifecycle projection and executor are ready.  Until the policy-equivalent
 * executor exists, the Queue consumer records a terminal failure rather than
 * claiming a successful Firestore-compatible transition run.
 */
export function registerMemoryShortTermLifecycleRoutes(app: Hono<{ Bindings: JobsEnv }>): void {
  app.post("/memory/admin/users/:uid/short-term-lifecycle/run", async (c) => {
    if (!adminAuthorized(c)) {
      return c.json({ detail: "You are not authorized to perform this action" }, 403);
    }
    const uid = c.req.param("uid");
    if (!validUid(uid)) return invalidRequest(c, "invalid uid");

    const runId = (c.req.query("run_id") || "").trim();
    if (!runId || runId.length > MAX_RUN_ID_LENGTH || !RUN_ID.test(runId)) {
      return invalidRequest(c, "run_id must be non-empty and must not contain slash");
    }
    const limitValue = c.req.query("limit");
    const limit = limitValue === undefined
      ? DEFAULT_LIFECYCLE_LIMIT
      : strictInteger(limitValue, 1, MAX_LIFECYCLE_LIMIT);
    if (limit === null) return invalidRequest(c, "limit must be between 1 and 1000");
    const evaluated = parseEvaluationTime(c.req.query("evaluated_at"));
    if (!evaluated) return invalidRequest(c, "evaluated_at must include a timezone");

    const now = Math.floor(Date.now() / 1_000);
    let generationOrResponse: number | Response;
    try {
      generationOrResponse = await currentAuthority(c, uid, now);
    } catch {
      return lifecycleUnavailable(c, "authority_unavailable");
    }
    if (generationOrResponse instanceof Response) return generationOrResponse;
    const accountGeneration = generationOrResponse;
    const requestFingerprint = await sha256Hex(
      `memory_short_term_lifecycle\0${uid}\0${runId}\0${evaluated.epoch}\0${limit}\0${accountGeneration}`,
    );
    const jobId = `memory-stl-${requestFingerprint.slice(0, 48)}`;

    let inserted = false;
    try {
      const result = await c.env.APP_DB.prepare(
        "INSERT INTO cf_memory_short_term_lifecycle_runs " +
          "(uid, run_id, request_fingerprint, evaluated_at, requested_limit, status, attempts, next_attempt_at, account_generation, created_at, updated_at) " +
          "VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
      )
        .bind(uid, runId, requestFingerprint, evaluated.epoch, limit, now, accountGeneration, now, now)
        .run();
      inserted = result.meta?.changes === 1;
    } catch (error) {
      if (error instanceof Error && error.message.includes("account deletion fence")) {
        return lifecycleUnavailable(c, "account_deletion_in_progress", 409);
      }
      return lifecycleUnavailable(c, "run_authority_unavailable");
    }

    let run: LifecycleRun | null;
    try {
      run = parseRun(
        await c.env.APP_DB.prepare(
          "SELECT uid, run_id, request_fingerprint, evaluated_at, requested_limit, status, attempts, lease_until, result_json, account_generation " +
            "FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?",
        )
          .bind(uid, runId)
          .first(),
      );
    } catch {
      return lifecycleUnavailable(c, "run_authority_unavailable");
    }
    if (!run) return lifecycleUnavailable(c, inserted ? "invalid_run_projection" : "run_id_conflict", inserted ? 503 : 409);
    if (run.request_fingerprint !== requestFingerprint || run.account_generation !== accountGeneration) {
      return c.json({ error: "run_id reused with different payload" }, 409);
    }
    if (!inserted) {
      if (run.status === "completed") {
        const result = storedLifecycleResult(run);
        return result ? c.json(result, 200) : lifecycleUnavailable(c, "invalid_run_result");
      }
      if (run.status === "failed") return lifecycleUnavailable(c, "run_failed");
      return c.json({ status: "already_queued", run_id: run.run_id }, 200);
    }

    const leaseToken = await claimLifecycleRun(c.env, run, now);
    if (!leaseToken) return c.json({ status: "already_queued", run_id: run.run_id }, 200);
    try {
      const result = await executeLifecycleRun(c.env, run, leaseToken);
      return c.json(result, 200);
    } catch (error) {
      const reason = error instanceof Error ? error.message : "lifecycle_execution_unavailable";
      if (error instanceof LifecycleTerminalError || reason.includes("account deletion fence")) {
        await failRun(c.env, run, reason, now);
        return lifecycleUnavailable(c, reason.includes("account deletion fence") ? "account_deletion_in_progress" : reason, 409);
      }
      try {
        await retryRun(c.env, run, leaseToken, reason, now);
        await c.env.JOBS.send(lifecycleMessage(run));
        return c.json({
          status: "queued",
          run_id: run.run_id,
          evaluated_at: evaluated.iso,
          job_id: jobId,
          account_generation: accountGeneration,
        }, 202);
      } catch {
        await failRun(c.env, run, "queue_unavailable", now);
        return lifecycleUnavailable(c, "queue_unavailable");
      }
    }
  });
}

/** Process a lifecycle message with a lease, D1 policy reader, and idempotent transition writer. */
export async function processMemoryShortTermLifecycleMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const parsed = parseMessagePayload(message.body.payload);
  if (!parsed) {
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1_000);
  const row = parseRun(
    await env.APP_DB.prepare(
      "SELECT uid, run_id, request_fingerprint, evaluated_at, requested_limit, status, attempts, lease_until, account_generation " +
        "FROM cf_memory_short_term_lifecycle_runs WHERE uid = ? AND run_id = ?",
    )
      .bind(message.body.uid, parsed.runId)
      .first(),
  );
  if (!row || row.request_fingerprint !== parsed.requestFingerprint || row.account_generation !== parsed.accountGeneration) {
    message.ack();
    return;
  }
  if (row.status === "completed" || row.status === "failed") {
    message.ack();
    return;
  }
  if (row.status === "running" && (row.lease_until === null || row.lease_until > now)) {
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }

  const leaseToken = crypto.randomUUID();
  let claimed: { meta?: { changes?: number } };
  try {
    claimed = await env.APP_DB.prepare(
      "UPDATE cf_memory_short_term_lifecycle_runs SET status = 'running', attempts = attempts + 1, lease_token = ?, lease_until = ?, updated_at = ? " +
        "WHERE uid = ? AND run_id = ? AND account_generation = ? AND " +
        "(status = 'queued' OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))",
    )
      .bind(leaseToken, now + LEASE_SECONDS, now, row.uid, row.run_id, row.account_generation, now)
      .run();
  } catch {
    // The mutation fence may be raised between the read above and the lease
    // claim.  A fenced account is terminal for this message, not a retryable
    // provider failure; otherwise a purge could be kept alive by Queue retry.
    if (await accountFence({ env } as JobsContext, row.uid, now)) {
      message.ack();
      return;
    }
    throw new Error("lifecycle lease unavailable");
  }
  if (claimed.meta?.changes !== 1) {
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }

  try {
    await executeLifecycleRun(env, row, leaseToken);
    message.ack();
  } catch (error) {
    const reason = error instanceof Error ? error.message : "lifecycle_execution_unavailable";
    if (error instanceof LifecycleTerminalError || reason.includes("account deletion fence") || message.attempts >= 3) {
      await failRun(env, row, reason.includes("account deletion fence") ? "account_deletion_in_progress" : reason, now);
      message.ack();
      return;
    }
    try {
      await retryRun(env, row, leaseToken, reason, now);
      message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    } catch {
      // Preserve the queue retry when D1 cannot even release the lease.  A
      // later delivery can reclaim the expired lease and retry the run.
      throw error;
    }
  }
}
