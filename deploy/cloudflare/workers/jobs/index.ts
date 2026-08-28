import { Hono, type Context } from "hono";
import { verifyRequestAuthContext } from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";

const app = new Hono<{ Bindings: JobsEnv }>();
const MAX_PAYLOAD_BYTES = 16_000;
const MAX_TRANSCRIPTION_AUDIO_BYTES = 5_000_000;
const MAX_TRANSCRIPTION_RESULT_BYTES = 1_000_000;
const MAX_IDEMPOTENCY_KEY_LENGTH = 128;
const JOB_LEASE_SECONDS = 15 * 60;
const QUEUE_RETRY_DELAY_SECONDS = 10;
const MAX_TRANSCRIPTION_PROVIDER_ATTEMPTS = 3;
const DEFAULT_WORKERS_AI_ASR_MODEL = "@cf/openai/whisper-large-v3-turbo";

app.get("/health", (c) =>
  c.json({ status: "ok", service: "jobs", version: "cf-07" }),
);

type ExistingJob = {
  job_id: string;
  status: string;
  last_error: string | null;
  request_fingerprint: string | null;
};

async function requestContext(c: Context<{ Bindings: JobsEnv }>) {
  return verifyRequestAuthContext(
    c.req.raw,
    "jobs",
    c.env.INTERNAL_ASSERTION_SECRET,
  );
}

function objectPayload(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

async function sha256Hex(value: string | ArrayBuffer): Promise<string> {
  const bytes =
    typeof value === "string" ? new TextEncoder().encode(value) : value;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

async function requestFingerprint(
  kind: JobMessage["kind"],
  payload: Record<string, unknown>,
): Promise<string> {
  return sha256Hex(`${kind}\0${stableJson(payload)}`);
}

async function publishJob(
  c: Context<{ Bindings: JobsEnv }>,
  context: { uid: string },
  jobId: string,
  kind: JobMessage["kind"],
  payload: Record<string, unknown>,
): Promise<Response> {
  try {
    await c.env.JOBS.send({ jobId, uid: context.uid, kind, payload });
  } catch {
    const now = Math.floor(Date.now() / 1000);
    try {
      await c.env.APP_DB.prepare(
        "UPDATE cf_jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE job_id = ? AND uid = ?",
      )
        .bind("queue unavailable", now, jobId, context.uid)
        .run();
    } catch {
      // The request still fails closed. A later exact retry can only repair the
      // row if D1 recorded the queue publication failure.
    }
    return c.json({ error: "queue unavailable" }, 503);
  }
  return c.json({ status: "queued", jobId }, 202);
}

async function resolveExistingJob(
  c: Context<{ Bindings: JobsEnv }>,
  context: { uid: string },
  existing: ExistingJob,
  kind: JobMessage["kind"],
  payload: Record<string, unknown>,
  payloadJson: string,
  fingerprint: string,
  identity: "idempotency key" | "job id",
): Promise<Response> {
  if (existing.request_fingerprint !== fingerprint) {
    return c.json({ error: `${identity} reused with different payload` }, 409);
  }
  if (
    existing.status === "failed" &&
    existing.last_error === "queue unavailable"
  ) {
    const now = Math.floor(Date.now() / 1000);
    const repaired = await c.env.APP_DB.prepare(
      "UPDATE cf_jobs SET payload_json = ?, status = 'queued', last_error = NULL, updated_at = ? " +
        "WHERE job_id = ? AND uid = ? AND status = 'failed' AND last_error = 'queue unavailable' AND request_fingerprint = ?",
    )
      .bind(payloadJson, now, existing.job_id, context.uid, fingerprint)
      .run();
    if (repaired.meta?.changes === 1) {
      return publishJob(c, context, existing.job_id, kind, payload);
    }
    const current = await c.env.APP_DB.prepare(
      "SELECT job_id, status, last_error, request_fingerprint FROM cf_jobs WHERE job_id = ? AND uid = ?",
    )
      .bind(existing.job_id, context.uid)
      .first<ExistingJob>();
    if (!current) return c.json({ error: "job id conflict" }, 409);
    existing = current;
  }
  return c.json({
    status: "already_queued",
    jobId: existing.job_id,
    state: existing.status,
  });
}

function parseTranscriptionPayload(payload: Record<string, unknown>) {
  const objectKey =
    typeof payload.objectKey === "string" ? payload.objectKey : "";
  const contentType =
    typeof payload.contentType === "string"
      ? payload.contentType
      : "application/octet-stream";
  const model =
    typeof payload.model === "string" && payload.model.length <= 128
      ? payload.model
      : undefined;
  if (!objectKey.startsWith("cf-transcriptions/") || objectKey.length > 512)
    return null;
  if (!(
    contentType.startsWith("audio/") ||
    contentType === "application/octet-stream"
  ))
    return null;
  return { objectKey, contentType, model };
}

async function enqueueJob(
  c: Context<{ Bindings: JobsEnv }>,
  context: { uid: string },
  jobId: string,
  kind: JobMessage["kind"],
  payload: Record<string, unknown>,
  idempotencyKey: string | null = null,
  fingerprintOverride: string | null = null,
) {
  const payloadJson = JSON.stringify(payload);
  if (payloadJson.length > MAX_PAYLOAD_BYTES)
    return c.json({ error: "job payload too large" }, 413);

  const fingerprint =
    fingerprintOverride || (await requestFingerprint(kind, payload));
  if (idempotencyKey) {
    const existing = await c.env.APP_DB.prepare(
      "SELECT job_id, status, last_error, request_fingerprint FROM cf_jobs WHERE uid = ? AND kind = ? AND idempotency_key = ?",
    )
      .bind(context.uid, kind, idempotencyKey)
      .first<ExistingJob>();
    if (existing) {
      return resolveExistingJob(
        c,
        context,
        existing,
        kind,
        payload,
        payloadJson,
        fingerprint,
        "idempotency key",
      );
    }
  }

  const now = Math.floor(Date.now() / 1000);
  const inserted = await c.env.APP_DB.prepare(
    "INSERT INTO cf_jobs (job_id, uid, kind, payload_json, status, attempts, created_at, updated_at, idempotency_key, request_fingerprint) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
  )
    .bind(
      jobId,
      context.uid,
      kind,
      payloadJson,
      now,
      now,
      idempotencyKey,
      fingerprint,
    )
    .run();
  if (inserted.meta?.changes !== 1) {
    const existing = idempotencyKey
      ? await c.env.APP_DB.prepare(
          "SELECT job_id, status, last_error, request_fingerprint FROM cf_jobs WHERE uid = ? AND kind = ? AND idempotency_key = ?",
        )
          .bind(context.uid, kind, idempotencyKey)
          .first<ExistingJob>()
      : await c.env.APP_DB.prepare(
          "SELECT job_id, status, last_error, request_fingerprint FROM cf_jobs WHERE job_id = ? AND uid = ?",
        )
          .bind(jobId, context.uid)
          .first<ExistingJob>();
    if (!existing) return c.json({ error: "job id conflict" }, 409);
    return resolveExistingJob(
      c,
      context,
      existing,
      kind,
      payload,
      payloadJson,
      fingerprint,
      idempotencyKey ? "idempotency key" : "job id",
    );
  }
  return publishJob(c, context, jobId, kind, payload);
}

app.post("/v1/cf/jobs", async (c) => {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);

  let body: { jobId?: unknown; kind?: unknown; payload?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "invalid JSON" }, 400);
  }
  const jobId = typeof body.jobId === "string" ? body.jobId.trim() : "";
  const kind: JobMessage["kind"] | "" =
    body.kind === "probe" || body.kind === "transcribe" ? body.kind : "";
  const payload = objectPayload(body.payload) || {};
  if (!jobId || jobId.length > 128 || !kind)
    return c.json({ error: "invalid job" }, 400);
  if (kind === "transcribe" && !parseTranscriptionPayload(payload))
    return c.json({ error: "invalid transcription job" }, 400);
  return enqueueJob(c, context, jobId, kind, payload);
});

app.post("/v1/cf/transcription-jobs", async (c) => {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);

  const idempotencyKey = c.req.header("idempotency-key")?.trim() || null;
  if (idempotencyKey && idempotencyKey.length > MAX_IDEMPOTENCY_KEY_LENGTH) {
    return c.json({ error: "idempotency key too long" }, 400);
  }

  const contentType =
    c.req.header("content-type")?.split(";", 1)[0].trim().toLowerCase() ||
    "application/octet-stream";
  if (!(
    contentType.startsWith("audio/") ||
    contentType === "application/octet-stream"
  )) {
    return c.json({ error: "transcription expects an audio body" }, 415);
  }
  const declaredLength = Number(c.req.header("content-length"));
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_TRANSCRIPTION_AUDIO_BYTES
  ) {
    return c.json({ error: "audio body too large" }, 413);
  }
  const body = await c.req.raw.arrayBuffer();
  if (!body.byteLength) return c.json({ error: "no audio data provided" }, 400);
  if (body.byteLength > MAX_TRANSCRIPTION_AUDIO_BYTES)
    return c.json({ error: "audio body too large" }, 413);
  const audioSha256 = await sha256Hex(body);
  const fingerprint = await sha256Hex(
    `transcribe\0${contentType}\0${audioSha256}`,
  );

  const jobId = crypto.randomUUID();
  const objectKey = `cf-transcriptions/${context.uid}/${jobId}`;
  try {
    await c.env.ASSETS.put(objectKey, body, {
      httpMetadata: { contentType },
      customMetadata: { uid: context.uid, jobId, sha256: audioSha256 },
    });
  } catch {
    return c.json({ error: "audio staging unavailable" }, 503);
  }

  let response: Response;
  try {
    response = await enqueueJob(
      c,
      context,
      jobId,
      "transcribe",
      { objectKey, contentType, sha256: audioSha256 },
      idempotencyKey,
      fingerprint,
    );
  } catch {
    try {
      await c.env.ASSETS.delete(objectKey);
    } catch {
      return c.json({ error: "audio staging cleanup unavailable" }, 503);
    }
    return c.json({ error: "transcription job unavailable" }, 503);
  }
  if (response.status !== 202) {
    try {
      await c.env.ASSETS.delete(objectKey);
    } catch {
      return c.json({ error: "audio staging cleanup unavailable" }, 503);
    }
  }
  return response;
});

async function getJobStatus(c: Context<{ Bindings: JobsEnv }>) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  const jobId = (c.req.param("jobId") || "").trim();
  if (!jobId || jobId.length > 128)
    return c.json({ error: "invalid job id" }, 400);
  const row = await c.env.APP_DB.prepare(
    "SELECT job_id, kind, status, attempts, last_error, result_json, created_at, updated_at FROM cf_jobs WHERE job_id = ? AND uid = ?",
  )
    .bind(jobId, context.uid)
    .first<{
      job_id: string;
      kind: string;
      status: string;
      attempts: number;
      last_error: string | null;
      result_json: string | null;
      created_at: number;
      updated_at: number;
    }>();
  if (!row) return c.json({ error: "job not found" }, 404);
  let result: unknown;
  if (row.result_json) {
    try {
      result = JSON.parse(row.result_json);
    } catch {
      result = undefined;
    }
  }
  return c.json({
    jobId: row.job_id,
    kind: row.kind,
    status: row.status,
    attempts: row.attempts,
    lastError: row.last_error,
    ...(result === undefined ? {} : { result }),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  });
}

app.get("/v1/cf/jobs/:jobId", getJobStatus);
app.get("/v1/cf/transcription-jobs/:jobId", getJobStatus);

function base64Encode(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(
      ...bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length)),
    );
  }
  return btoa(binary);
}

function resultMapping(result: unknown): Record<string, unknown> {
  if (result && typeof result === "object" && !Array.isArray(result))
    return result as Record<string, unknown>;
  return {};
}

function normalizedTranscription(
  result: unknown,
  model: string,
): Record<string, unknown> | null {
  const payload = resultMapping(result);
  if (typeof payload.text !== "string") return null;
  const normalized: Record<string, unknown> = {
    text: payload.text,
    segments: Array.isArray(payload.segments) ? payload.segments : [],
    detected_language:
      typeof payload.detected_language === "string"
        ? payload.detected_language
        : null,
    provider: "workers-ai",
    model,
  };
  for (const field of ["word_count", "words", "vtt"]) {
    if (field in payload) normalized[field] = payload[field];
  }
  return normalized;
}

async function markJobFailed(
  env: JobsEnv,
  jobId: string,
  uid: string,
  error: string,
) {
  const now = Math.floor(Date.now() / 1000);
  await env.APP_DB.prepare(
    "UPDATE cf_jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE job_id = ? AND uid = ?",
  )
    .bind(error, now, jobId, uid)
    .run();
}

async function cleanupTranscriptionObject(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  if (message.body.kind !== "transcribe") return;
  const payload = parseTranscriptionPayload(message.body.payload);
  if (
    payload &&
    payload.objectKey.startsWith(`cf-transcriptions/${message.body.uid}/`)
  ) {
    await env.ASSETS.delete(payload.objectKey);
  }
}

async function acknowledgeAfterCleanup(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  await cleanupTranscriptionObject(message, env);
  message.ack();
}

async function retryTerminalFailure(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  await cleanupTranscriptionObject(message, env);
  message.retry({ delaySeconds: QUEUE_RETRY_DELAY_SECONDS });
}

async function processTranscription(
  message: Message<JobMessage>,
  env: JobsEnv,
  now: number,
): Promise<void> {
  const payload = parseTranscriptionPayload(message.body.payload);
  if (
    !payload ||
    !payload.objectKey.startsWith(`cf-transcriptions/${message.body.uid}/`)
  ) {
    await markJobFailed(
      env,
      message.body.jobId,
      message.body.uid,
      "invalid transcription payload",
    );
    message.retry({ delaySeconds: QUEUE_RETRY_DELAY_SECONDS });
    return;
  }
  const object = await env.ASSETS.get(payload.objectKey);
  if (!object) {
    await markJobFailed(
      env,
      message.body.jobId,
      message.body.uid,
      "staged audio not found",
    );
    message.retry({ delaySeconds: QUEUE_RETRY_DELAY_SECONDS });
    return;
  }
  const body = await object.arrayBuffer();
  if (!body.byteLength || body.byteLength > MAX_TRANSCRIPTION_AUDIO_BYTES) {
    await markJobFailed(
      env,
      message.body.jobId,
      message.body.uid,
      "staged audio is invalid",
    );
    await retryTerminalFailure(message, env);
    return;
  }
  const model =
    payload.model || env.WORKERS_AI_ASR_MODEL || DEFAULT_WORKERS_AI_ASR_MODEL;
  try {
    const result = await env.AI.run(model, { audio: base64Encode(body) });
    const normalized = normalizedTranscription(result, model);
    const resultJson = normalized ? JSON.stringify(normalized) : "";
    if (!normalized || resultJson.length > MAX_TRANSCRIPTION_RESULT_BYTES) {
      await markJobFailed(
        env,
        message.body.jobId,
        message.body.uid,
        "workers ai returned invalid transcription",
      );
      await retryTerminalFailure(message, env);
      return;
    }
    await env.APP_DB.prepare(
      "UPDATE cf_jobs SET status = 'completed', result_json = ?, last_error = NULL, updated_at = ? WHERE job_id = ? AND uid = ?",
    )
      .bind(resultJson, now, message.body.jobId, message.body.uid)
      .run();
    await acknowledgeAfterCleanup(message, env);
  } catch {
    if (message.attempts >= MAX_TRANSCRIPTION_PROVIDER_ATTEMPTS) {
      await markJobFailed(
        env,
        message.body.jobId,
        message.body.uid,
        "workers ai transcription unavailable",
      );
      await retryTerminalFailure(message, env);
      return;
    }
    await env.APP_DB.prepare(
      "UPDATE cf_jobs SET status = 'queued', last_error = ?, updated_at = ? WHERE job_id = ? AND uid = ?",
    )
      .bind(
        "workers ai transcription unavailable",
        now,
        message.body.jobId,
        message.body.uid,
      )
      .run();
    message.retry({ delaySeconds: QUEUE_RETRY_DELAY_SECONDS });
  }
}

async function processJobMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const row = await env.APP_DB.prepare(
    "SELECT status, kind, updated_at FROM cf_jobs WHERE job_id = ? AND uid = ?",
  )
    .bind(message.body.jobId, message.body.uid)
    .first<{ status: string; kind: string; updated_at: number }>();
  if (!row) {
    await acknowledgeAfterCleanup(message, env);
    return;
  }
  if (row.status === "completed") {
    await acknowledgeAfterCleanup(message, env);
    return;
  }
  if (row.status === "failed") {
    await retryTerminalFailure(message, env);
    return;
  }
  if (row.kind !== message.body.kind) {
    await markJobFailed(
      env,
      message.body.jobId,
      message.body.uid,
      "job kind mismatch",
    );
    await retryTerminalFailure(message, env);
    return;
  }

  const now = Math.floor(Date.now() / 1000);
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_jobs SET status = 'running', attempts = attempts + 1, updated_at = ? " +
      "WHERE job_id = ? AND uid = ? AND (status = 'queued' OR (status = 'running' AND updated_at <= ?))",
  )
    .bind(now, message.body.jobId, message.body.uid, now - JOB_LEASE_SECONDS)
    .run();
  if (claimed.meta?.changes !== 1) {
    message.retry({ delaySeconds: QUEUE_RETRY_DELAY_SECONDS });
    return;
  }
  if (row.kind === "transcribe") {
    await processTranscription(message, env, now);
    return;
  }
  if (row.kind !== "probe") {
    await markJobFailed(
      env,
      message.body.jobId,
      message.body.uid,
      "unsupported job kind",
    );
    await retryTerminalFailure(message, env);
    return;
  }
  await env.APP_DB.prepare(
    "UPDATE cf_jobs SET status = 'completed', updated_at = ? WHERE job_id = ? AND uid = ?",
  )
    .bind(now, message.body.jobId, message.body.uid)
    .run();
  message.ack();
}

export default {
  fetch: app.fetch,
  async queue(batch: MessageBatch<JobMessage>, env: JobsEnv): Promise<void> {
    for (const message of batch.messages) {
      try {
        await processJobMessage(message, env);
      } catch {
        message.retry({ delaySeconds: QUEUE_RETRY_DELAY_SECONDS });
      }
    }
  },
};
