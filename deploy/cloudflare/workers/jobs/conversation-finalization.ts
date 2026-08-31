import type { Message } from "@cloudflare/workers-types";
import { createSignedAuthContext } from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import type { JobMessage, JobsEnv } from "./env";

const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const RECONCILE_BATCH_SIZE = 50;
const MAX_ATTEMPTS = 3;
const PROCESSOR_PATH = "/internal/conversations/finalize";

type FinalizationJobRow = {
  uid: string;
  conversation_id: string;
  job_id: string;
  finalization_revision: number;
  operation: "finalize" | "reprocess";
  language_code: string | null;
  app_id: string | null;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  lease_until: number | null;
  next_attempt_at: number;
};

function validString(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength;
}

function parsePayload(value: unknown): {
  conversationId: string;
  revision: number;
  languageCode: string | null;
  appId: string | null;
} | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const payload = value as Record<string, unknown>;
  const conversationId = payload.conversationId;
  const revision = payload.revision;
  if (!validString(conversationId, 256)) return null;
  const parsedRevision = typeof revision === "number" ? revision : Number(revision);
  if (!Number.isSafeInteger(parsedRevision) || parsedRevision < 0) return null;
  const languageCode = payload.languageCode;
  const appId = payload.appId;
  if (languageCode !== undefined && (typeof languageCode !== "string" || languageCode.length > 32)) return null;
  if (appId !== undefined && (typeof appId !== "string" || appId.length > 256)) return null;
  return {
    conversationId,
    revision: parsedRevision,
    languageCode: typeof languageCode === "string" ? languageCode : null,
    appId: typeof appId === "string" ? appId : null,
  };
}

function parseJobRow(value: unknown): FinalizationJobRow | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Partial<FinalizationJobRow>;
  if (
    !validString(row.uid, 256) ||
    !validString(row.conversation_id, 256) ||
    !validString(row.job_id, 128) ||
    (row.status !== "queued" &&
      row.status !== "running" &&
      row.status !== "completed" &&
      row.status !== "failed")
  ) {
    return null;
  }
  const revision = Number(row.finalization_revision);
  const attempts = Number(row.attempts);
  const nextAttemptAt = Number(row.next_attempt_at);
  if (
    !Number.isSafeInteger(revision) ||
    revision < 0 ||
    !Number.isSafeInteger(attempts) ||
    attempts < 0 ||
    !Number.isSafeInteger(nextAttemptAt) ||
    nextAttemptAt < 0
  ) {
    return null;
  }
  const leaseUntil = row.lease_until === null || row.lease_until === undefined
    ? null
    : Number(row.lease_until);
  if (leaseUntil !== null && (!Number.isSafeInteger(leaseUntil) || leaseUntil < 0)) {
    return null;
  }
  return {
    uid: row.uid,
    conversation_id: row.conversation_id,
    job_id: row.job_id,
    finalization_revision: revision,
    operation: row.operation === "reprocess" ? "reprocess" : "finalize",
    language_code: typeof row.language_code === "string" ? row.language_code : null,
    app_id: typeof row.app_id === "string" ? row.app_id : null,
    status: row.status,
    attempts,
    lease_until: leaseUntil,
    next_attempt_at: nextAttemptAt,
  };
}

function jobMessage(job: FinalizationJobRow): JobMessage {
  return {
    jobId: job.job_id,
    uid: job.uid,
    kind: job.operation === "reprocess" ? "conversation_reprocess" : "conversation_finalize",
    payload: {
      conversationId: job.conversation_id,
      revision: job.finalization_revision,
      ...(job.language_code ? { languageCode: job.language_code } : {}),
      ...(job.app_id ? { appId: job.app_id } : {}),
    },
  };
}

function retryableProcessorStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

async function markTerminalFailure(
  env: JobsEnv,
  job: FinalizationJobRow,
  reason: string,
  now: number,
): Promise<void> {
  await env.APP_DB.batch([
    env.APP_DB.prepare(
      "UPDATE cf_conversation_finalization_jobs SET status = 'failed', lease_until = NULL, " +
        "last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND status <> 'completed'",
    ).bind(reason, now, job.uid, job.job_id),
    env.APP_DB.prepare(
      "UPDATE cf_conversations SET status = 'failed', finalization_status = 'failed' " +
        "WHERE uid = ? AND id = ? AND finalization_job_id = ? AND status = 'processing'",
    ).bind(job.uid, job.conversation_id, job.job_id),
  ]);
}

async function markRetry(
  env: JobsEnv,
  job: FinalizationJobRow,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_conversation_finalization_jobs SET status = 'queued', lease_until = NULL, " +
      "next_attempt_at = ?, last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ? " +
      "AND status = 'running'",
  )
    .bind(now + RETRY_DELAY_SECONDS, "conversation finalization processor unavailable", now, job.uid, job.job_id)
    .run();
}

async function callProcessor(
  env: JobsEnv,
  job: FinalizationJobRow,
): Promise<Response> {
  if (!env.API_CORE) throw new Error("api core service unavailable");
  const signed = await createSignedAuthContext(
    { uid: job.uid, authority: "internal", requestId: `job:${job.job_id}` },
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
        conversation_id: job.conversation_id,
        revision: job.finalization_revision,
        operation: job.operation,
        ...(job.language_code ? { language_code: job.language_code } : {}),
        ...(job.app_id ? { app_id: job.app_id } : {}),
      }),
    }),
  );
}

export async function processConversationFinalizationMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const payload = parsePayload(message.body.payload);
  if (
    (message.body.kind !== "conversation_finalize" && message.body.kind !== "conversation_reprocess") ||
    !validString(message.body.uid, 256) ||
    !validString(message.body.jobId, 128) ||
    !payload
  ) {
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1_000);
  const raw = await env.APP_DB.prepare(
    "SELECT uid, conversation_id, job_id, finalization_revision, operation, language_code, app_id, status, attempts, lease_until, next_attempt_at " +
      "FROM cf_conversation_finalization_jobs WHERE uid = ? AND job_id = ?",
  )
    .bind(message.body.uid, message.body.jobId)
    .first();
  const existing = parseJobRow(raw);
  if (
    !existing ||
    existing.conversation_id !== payload.conversationId ||
    existing.finalization_revision !== payload.revision ||
    (existing.operation === "reprocess") !== (message.body.kind === "conversation_reprocess") ||
    (existing.operation === "reprocess" &&
      (existing.language_code !== payload.languageCode || existing.app_id !== payload.appId))
  ) {
    message.ack();
    return;
  }
  if (existing.status === "completed" || existing.status === "failed") {
    message.ack();
    return;
  }

  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_conversation_finalization_jobs SET status = 'running', attempts = attempts + 1, " +
      "lease_until = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND " +
      "(status = 'queued' AND next_attempt_at <= ? OR status = 'running' AND lease_until <= ?) ",
  )
    .bind(now + LEASE_SECONDS, now, existing.uid, existing.job_id, now, now)
    .run();
  if (Number(claimed.meta?.changes ?? 0) !== 1) {
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }
  const job = { ...existing, status: "running" as const, attempts: existing.attempts + 1 };

  try {
    const response = await callProcessor(env, job);
    if (response.ok) {
      message.ack();
      return;
    }
    if (!retryableProcessorStatus(response.status) || job.attempts >= MAX_ATTEMPTS) {
      await markTerminalFailure(env, job, "conversation finalization rejected", now);
      message.ack();
      return;
    }
    await markRetry(env, job, now);
    recordFallback({
      component: "other",
      from: "d1",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "degraded",
    });
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
  } catch {
    if (job.attempts >= MAX_ATTEMPTS) {
      await markTerminalFailure(env, job, "conversation finalization unavailable", now);
      message.ack();
      return;
    }
    await markRetry(env, job, now);
    recordFallback({
      component: "other",
      from: "d1",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "degraded",
    });
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
  }
}

export async function reconcileConversationFinalizations(
  env: JobsEnv,
  now: number,
): Promise<void> {
  const result = await env.APP_DB.prepare(
    "SELECT uid, conversation_id, job_id, finalization_revision, operation, language_code, app_id, status, attempts, lease_until, next_attempt_at " +
      "FROM cf_conversation_finalization_jobs WHERE (status = 'queued' AND next_attempt_at <= ?) " +
      "OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?) " +
      "ORDER BY next_attempt_at, updated_at LIMIT ?",
  )
    .bind(now, now, RECONCILE_BATCH_SIZE)
    .all();
  for (const raw of result.results || []) {
    const job = parseJobRow(raw);
    if (!job || job.status === "completed" || job.status === "failed") continue;
    const repaired = await env.APP_DB.prepare(
      "UPDATE cf_conversation_finalization_jobs SET status = 'queued', lease_until = NULL, " +
        "next_attempt_at = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND " +
        "(status = 'queued' AND next_attempt_at <= ? OR status = 'running' AND lease_until <= ?)",
    )
      .bind(now, now, job.uid, job.job_id, now, now)
      .run();
    if (Number(repaired.meta?.changes ?? 0) !== 1) continue;
    try {
      await env.JOBS.send(jobMessage(job));
    } catch {
      await env.APP_DB.prepare(
        "UPDATE cf_conversation_finalization_jobs SET next_attempt_at = ?, last_error = ?, updated_at = ? " +
          "WHERE uid = ? AND job_id = ? AND status = 'queued'",
      )
        .bind(now + RETRY_DELAY_SECONDS, "conversation processor queue unavailable", now, job.uid, job.job_id)
        .run();
      recordFallback({
        component: "other",
        from: "d1",
        to: "none",
        reason: "dependency_unavailable",
        outcome: "degraded",
      });
    }
  }
}

export const CONVERSATION_FINALIZATION_MAX_ATTEMPTS = MAX_ATTEMPTS;
