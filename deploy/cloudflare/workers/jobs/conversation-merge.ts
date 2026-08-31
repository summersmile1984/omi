import type { Message } from "@cloudflare/workers-types";
import { createSignedAuthContext } from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import type { JobMessage, JobsEnv } from "./env";

const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const RECONCILE_BATCH_SIZE = 50;
const MAX_ATTEMPTS = 3;
const MAX_SOURCE_CONVERSATIONS = 20;
const PROCESSOR_PATH = "/internal/conversations/merge";

type ConversationMergeJobRow = {
  uid: string;
  job_id: string;
  source_conversation_ids_json: string;
  result_conversation_id: string;
  merge_revision: number;
  reprocess: number;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  lease_until: number | null;
  next_attempt_at: number;
};

function validString(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength;
}

function parseIds(value: unknown): string[] | null {
  if (typeof value !== "string") return null;
  let decoded: unknown;
  try {
    decoded = JSON.parse(value);
  } catch {
    return null;
  }
  if (!Array.isArray(decoded) || decoded.length < 2 || decoded.length > MAX_SOURCE_CONVERSATIONS) return null;
  const ids = decoded.filter((id): id is string => validString(id, 256) && !id.includes("/"));
  return ids.length === decoded.length && new Set(ids).size === ids.length ? ids : null;
}

function parsePayload(value: unknown): { conversationIds: string[]; revision: number; reprocess: boolean } | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const payload = value as Record<string, unknown>;
  const conversationIds = Array.isArray(payload.conversationIds)
    ? parseIds(JSON.stringify(payload.conversationIds))
    : null;
  const revision = Number(payload.revision);
  if (!conversationIds || !Number.isSafeInteger(revision) || revision < 0 || typeof payload.reprocess !== "boolean") {
    return null;
  }
  return { conversationIds, revision, reprocess: payload.reprocess };
}

function parseJobRow(value: unknown): ConversationMergeJobRow | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Partial<ConversationMergeJobRow>;
  if (
    !validString(row.uid, 256) ||
    !validString(row.job_id, 128) ||
    !validString(row.source_conversation_ids_json, 16_000) ||
    !validString(row.result_conversation_id, 256) ||
    (row.status !== "queued" && row.status !== "running" && row.status !== "completed" && row.status !== "failed")
  ) {
    return null;
  }
  const revision = Number(row.merge_revision);
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
  const leaseUntil = row.lease_until === null || row.lease_until === undefined ? null : Number(row.lease_until);
  if (leaseUntil !== null && (!Number.isSafeInteger(leaseUntil) || leaseUntil < 0)) return null;
  return {
    uid: row.uid,
    job_id: row.job_id,
    source_conversation_ids_json: row.source_conversation_ids_json,
    result_conversation_id: row.result_conversation_id,
    merge_revision: revision,
    reprocess: Number(row.reprocess) ? 1 : 0,
    status: row.status,
    attempts,
    lease_until: leaseUntil,
    next_attempt_at: nextAttemptAt,
  };
}

function jobMessage(job: ConversationMergeJobRow): JobMessage {
  const ids = parseIds(job.source_conversation_ids_json) || [];
  return {
    jobId: job.job_id,
    uid: job.uid,
    kind: "conversation_merge",
    payload: { conversationIds: ids, revision: job.merge_revision, reprocess: Boolean(job.reprocess) },
  };
}

function retryableProcessorStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

async function markTerminalFailure(env: JobsEnv, job: ConversationMergeJobRow, reason: string, now: number) {
  const ids = parseIds(job.source_conversation_ids_json) || [];
  const placeholders = ids.map(() => "?").join(",");
  await env.APP_DB.batch([
    env.APP_DB.prepare(
      "UPDATE cf_conversation_merge_jobs SET status = 'failed', lease_until = NULL, last_error = ?, updated_at = ? " +
        "WHERE uid = ? AND job_id = ? AND status <> 'completed'",
    ).bind(reason, now, job.uid, job.job_id),
    env.APP_DB.prepare(
      "UPDATE cf_conversations SET status = 'completed', merge_job_id = NULL, merge_revision = NULL " +
        `WHERE uid = ? AND id IN (${placeholders}) AND merge_job_id = ? AND status = 'merging'`,
    ).bind(job.uid, ...ids, job.job_id),
  ]);
}

async function markRetry(env: JobsEnv, job: ConversationMergeJobRow, now: number) {
  await env.APP_DB.prepare(
    "UPDATE cf_conversation_merge_jobs SET status = 'queued', lease_until = NULL, next_attempt_at = ?, " +
      "last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND status = 'running'",
  )
    .bind(now + RETRY_DELAY_SECONDS, "conversation merge processor unavailable", now, job.uid, job.job_id)
    .run();
}

async function callProcessor(env: JobsEnv, job: ConversationMergeJobRow): Promise<Response> {
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
        conversation_ids: parseIds(job.source_conversation_ids_json),
        revision: job.merge_revision,
        reprocess: Boolean(job.reprocess),
      }),
    }),
  );
}

export async function processConversationMergeMessage(message: Message<JobMessage>, env: JobsEnv): Promise<void> {
  if (message.body.kind !== "conversation_merge" || !validString(message.body.uid, 256) || !validString(message.body.jobId, 128)) {
    message.ack();
    return;
  }
  const payload = parsePayload(message.body.payload);
  if (!payload) {
    message.ack();
    return;
  }
  const raw = await env.APP_DB.prepare(
    "SELECT uid, job_id, source_conversation_ids_json, result_conversation_id, merge_revision, reprocess, status, attempts, lease_until, next_attempt_at " +
      "FROM cf_conversation_merge_jobs WHERE uid = ? AND job_id = ?",
  )
    .bind(message.body.uid, message.body.jobId)
    .first();
  const existing = parseJobRow(raw);
  const storedIds = existing && parseIds(existing.source_conversation_ids_json);
  if (
    !existing ||
    !storedIds ||
    storedIds.join("\0") !== payload.conversationIds.join("\0") ||
    existing.merge_revision !== payload.revision ||
    Boolean(existing.reprocess) !== payload.reprocess
  ) {
    message.ack();
    return;
  }
  if (existing.status === "completed" || existing.status === "failed") {
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1_000);
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_conversation_merge_jobs SET status = 'running', attempts = attempts + 1, lease_until = ?, updated_at = ? " +
      "WHERE uid = ? AND job_id = ? AND (status = 'queued' AND next_attempt_at <= ? OR status = 'running' AND lease_until <= ?)",
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
      await markTerminalFailure(env, job, "conversation merge rejected", now);
      message.ack();
      return;
    }
    await markRetry(env, job, now);
    recordFallback({ component: "other", from: "d1", to: "none", reason: "dependency_unavailable", outcome: "degraded" });
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
  } catch {
    if (job.attempts >= MAX_ATTEMPTS) {
      await markTerminalFailure(env, job, "conversation merge unavailable", now);
      message.ack();
      return;
    }
    await markRetry(env, job, now);
    recordFallback({ component: "other", from: "d1", to: "none", reason: "dependency_unavailable", outcome: "degraded" });
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
  }
}

export async function reconcileConversationMerges(env: JobsEnv, now: number): Promise<void> {
  const result = await env.APP_DB.prepare(
    "SELECT uid, job_id, source_conversation_ids_json, result_conversation_id, merge_revision, reprocess, status, attempts, lease_until, next_attempt_at " +
      "FROM cf_conversation_merge_jobs WHERE (status = 'queued' AND next_attempt_at <= ?) OR " +
      "(status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?) ORDER BY next_attempt_at, updated_at LIMIT ?",
  )
    .bind(now, now, RECONCILE_BATCH_SIZE)
    .all();
  for (const raw of result.results || []) {
    const job = parseJobRow(raw);
    if (!job || job.status === "completed" || job.status === "failed") continue;
    const repaired = await env.APP_DB.prepare(
      "UPDATE cf_conversation_merge_jobs SET status = 'queued', lease_until = NULL, next_attempt_at = ?, updated_at = ? " +
        "WHERE uid = ? AND job_id = ? AND (status = 'queued' AND next_attempt_at <= ? OR status = 'running' AND lease_until <= ?)",
    )
      .bind(now, now, job.uid, job.job_id, now, now)
      .run();
    if (Number(repaired.meta?.changes ?? 0) !== 1) continue;
    try {
      await env.JOBS.send(jobMessage(job));
    } catch {
      await env.APP_DB.prepare(
        "UPDATE cf_conversation_merge_jobs SET next_attempt_at = ?, last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND status = 'queued'",
      )
        .bind(now + RETRY_DELAY_SECONDS, "conversation processor queue unavailable", now, job.uid, job.job_id)
        .run();
      recordFallback({ component: "other", from: "d1", to: "none", reason: "dependency_unavailable", outcome: "degraded" });
    }
  }
}

export const CONVERSATION_MERGE_MAX_ATTEMPTS = MAX_ATTEMPTS;
