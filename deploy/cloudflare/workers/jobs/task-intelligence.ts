import type { Message } from "@cloudflare/workers-types";
import { createSignedAuthContext } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";

const PROCESSOR_PATH = "/internal/task-intelligence/evaluate";
const RETRY_DELAY_SECONDS = 10;
const RECONCILE_BATCH_SIZE = 50;

type TaskIntelligenceJobRow = {
  uid: string;
  job_id: string;
  account_generation: number;
  device_id: string;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  lease_until: number | null;
  next_attempt_at: number;
};

function validString(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maximum;
}

function parseJob(value: unknown): TaskIntelligenceJobRow | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Partial<TaskIntelligenceJobRow>;
  if (
    !validString(row.uid, 256) ||
    !validString(row.job_id, 128) ||
    !validString(row.device_id, 128) ||
    (row.status !== "queued" &&
      row.status !== "running" &&
      row.status !== "completed" &&
      row.status !== "failed")
  ) {
    return null;
  }
  const generation = Number(row.account_generation);
  const attempts = Number(row.attempts);
  const nextAttemptAt = Number(row.next_attempt_at);
  if (
    !Number.isSafeInteger(generation) ||
    generation < 0 ||
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
    job_id: row.job_id,
    account_generation: generation,
    device_id: row.device_id,
    status: row.status,
    attempts,
    lease_until: leaseUntil,
    next_attempt_at: nextAttemptAt,
  };
}

function taskMessage(job: TaskIntelligenceJobRow): JobMessage {
  return {
    jobId: job.job_id,
    uid: job.uid,
    kind: "task_intelligence_evaluate",
    payload: {},
  };
}

async function callProcessor(env: JobsEnv, job: TaskIntelligenceJobRow): Promise<Response> {
  if (!env.API_CORE) throw new Error("api core service unavailable");
  const signed = await createSignedAuthContext(
    { uid: job.uid, authority: "internal", requestId: `task-intelligence:${job.job_id}` },
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
        account_generation: job.account_generation,
        device_id: job.device_id,
      }),
    }),
  );
}

async function currentJob(env: JobsEnv, uid: string, jobId: string): Promise<TaskIntelligenceJobRow | null> {
  const row = await env.APP_DB.prepare(
    "SELECT uid, job_id, account_generation, device_id, status, attempts, lease_until, next_attempt_at " +
      "FROM cf_task_intelligence_jobs WHERE uid = ? AND job_id = ?",
  ).bind(uid, jobId).first();
  return parseJob(row);
}

export async function processTaskIntelligenceMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  if (
    message.body.kind !== "task_intelligence_evaluate" ||
    !validString(message.body.uid, 256) ||
    !validString(message.body.jobId, 128)
  ) {
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1_000);
  const job = await currentJob(env, message.body.uid, message.body.jobId);
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

  let response: Response;
  try {
    response = await callProcessor(env, job);
  } catch {
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }
  if (response.ok) {
    message.ack();
    return;
  }

  const updated = await currentJob(env, job.uid, job.job_id);
  if (!updated || updated.status === "completed" || updated.status === "failed") {
    message.ack();
    return;
  }
  // Deletion and generation fences are terminal for this delivery.  The
  // account deletion owner removes the row; retrying would only resurrect
  // work against a fenced account.
  if (response.status === 409 || response.status === 404 || response.status === 400) {
    message.ack();
    return;
  }
  message.retry({
    delaySeconds: updated.next_attempt_at > now
      ? Math.max(1, updated.next_attempt_at - now)
      : RETRY_DELAY_SECONDS,
  });
}

export async function reconcileTaskIntelligenceJobs(
  env: JobsEnv,
  now: number,
): Promise<void> {
  const result = await env.APP_DB.prepare(
    "SELECT uid, job_id, account_generation, device_id, status, attempts, lease_until, next_attempt_at " +
      "FROM cf_task_intelligence_jobs WHERE (status = 'queued' AND next_attempt_at <= ?) " +
      "OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?) " +
      "ORDER BY next_attempt_at, updated_at LIMIT ?",
  ).bind(now, now, RECONCILE_BATCH_SIZE).all();
  for (const raw of result.results || []) {
    const job = parseJob(raw);
    if (!job || job.status === "completed" || job.status === "failed") continue;
    const repaired = await env.APP_DB.prepare(
      "UPDATE cf_task_intelligence_jobs SET status = 'queued', lease_token = NULL, lease_until = NULL, " +
        "next_attempt_at = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND " +
        "(status = 'queued' AND next_attempt_at <= ? OR status = 'running' AND lease_until <= ?)",
    ).bind(now, now, job.uid, job.job_id, now, now).run();
    if (Number(repaired.meta?.changes ?? 0) !== 1) continue;
    try {
      await env.JOBS.send(taskMessage({ ...job, status: "queued", next_attempt_at: now, lease_until: null }));
    } catch {
      try {
        await env.APP_DB.prepare(
          "UPDATE cf_task_intelligence_jobs SET next_attempt_at = ?, last_error = ?, updated_at = ? " +
            "WHERE uid = ? AND job_id = ? AND status = 'queued'",
        ).bind(now + RETRY_DELAY_SECONDS, "task intelligence queue unavailable", now, job.uid, job.job_id).run();
      } catch {
        // The next scheduled reconciliation remains the recovery path.
      }
    }
  }
}
