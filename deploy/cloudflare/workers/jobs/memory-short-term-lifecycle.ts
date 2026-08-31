import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

const MAX_UID_LENGTH = 256;
const MAX_RUN_ID_LENGTH = 256;
const MAX_LIFECYCLE_LIMIT = 1_000;
const DEFAULT_LIFECYCLE_LIMIT = 500;
const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const RUN_ID = /^[^/\u0000]{1,256}$/;
const CONTROL_SOURCE = "cloudflare_short_term_lifecycle_projection";

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
  account_generation: number;
};

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
    (value.lease_until !== null && value.lease_until !== undefined && leaseUntil === null)
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
          "SELECT uid, run_id, request_fingerprint, evaluated_at, requested_limit, status, attempts, lease_until, account_generation " +
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
      if (run.status === "completed") return c.json({ status: "acked", run_id: run.run_id }, 200);
      if (run.status === "failed") return lifecycleUnavailable(c, "run_failed");
      return c.json({ status: "already_queued", run_id: run.run_id }, 200);
    }

    try {
      await c.env.JOBS.send(lifecycleMessage(run));
    } catch {
      await failRun(c.env, run, "queue_unavailable", now);
      return lifecycleUnavailable(c, "queue_unavailable");
    }
    return c.json({
      status: "queued",
      run_id: run.run_id,
      evaluated_at: evaluated.iso,
      job_id: jobId,
      account_generation: accountGeneration,
    }, 202);
  });
}

/** Process a lifecycle message with a lease, fence, and explicit no-parity failure. */
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
    if (await accountFence({ env } as JobsContext, row.uid, now)) {
      await failRun(env, row, "account_deletion_in_progress", now);
      message.ack();
      return;
    }
    const control = await env.APP_DB.prepare(
      "SELECT uid, schema_version, source, enabled, executor_state, account_generation " +
        "FROM cf_memory_short_term_lifecycle_control WHERE uid = ?",
    )
      .bind(row.uid)
      .first<LifecycleControl>();
    if (
      !control || control.uid !== row.uid || control.schema_version !== 1 || control.source !== CONTROL_SOURCE ||
      control.enabled !== 1 || control.executor_state !== "ready" || control.account_generation !== row.account_generation
    ) {
      await failRun(env, row, "lifecycle_authority_unavailable", now);
      message.ack();
      return;
    }
    // The policy-equivalent D1 memory reader and transition writer are not
    // implemented yet.  Never mark the run completed while that authority is
    // absent; this is the safety boundary that keeps legacy as owner.
    await failRun(env, row, "lifecycle_executor_unavailable", now);
    message.ack();
  } catch {
    await failRun(env, row, "lifecycle_authority_unavailable", now);
    message.ack();
  }
}
