import { Hono, type Context } from "hono";
import {
  decodeAuthContext,
  verifyAuthContextSignature,
} from "../shared/auth-context";
import type { JobMessage, JobsEnv } from "./env";

const app = new Hono<{ Bindings: JobsEnv }>();
const MAX_PAYLOAD_BYTES = 16_000;
const MAX_TRANSCRIPTION_AUDIO_BYTES = 5_000_000;
const MAX_TRANSCRIPTION_RESULT_BYTES = 1_000_000;
const MAX_IDEMPOTENCY_KEY_LENGTH = 128;
const DEFAULT_WORKERS_AI_ASR_MODEL = "@cf/openai/whisper-large-v3-turbo";

app.get("/health", (c) =>
  c.json({ status: "ok", service: "jobs", version: "cf-06" }),
);

function requestContext(c: Context<{ Bindings: JobsEnv }>) {
  const encodedContext = c.req.header("x-omi-auth-context") ?? null;
  const context = decodeAuthContext(encodedContext);
  return { encodedContext, context };
}

async function hasValidContext(
  c: Context<{ Bindings: JobsEnv }>,
): Promise<boolean> {
  const { encodedContext, context } = requestContext(c);
  return Boolean(
    context &&
    (await verifyAuthContextSignature(
      encodedContext || "",
      c.req.header("x-omi-internal-signature") ?? null,
      c.env.INTERNAL_ASSERTION_SECRET,
    )),
  );
}

function objectPayload(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
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
) {
  const payloadJson = JSON.stringify(payload);
  if (payloadJson.length > MAX_PAYLOAD_BYTES)
    return c.json({ error: "job payload too large" }, 413);

  if (idempotencyKey) {
    const existing = await c.env.APP_DB.prepare(
      "SELECT job_id, status FROM cf_jobs WHERE uid = ? AND kind = ? AND idempotency_key = ?",
    )
      .bind(context.uid, kind, idempotencyKey)
      .first<{ job_id: string; status: string }>();
    if (existing)
      return c.json({
        status: "already_queued",
        jobId: existing.job_id,
        state: existing.status,
      });
  }

  const now = Math.floor(Date.now() / 1000);
  const inserted = await c.env.APP_DB.prepare(
    "INSERT INTO cf_jobs (job_id, uid, kind, payload_json, status, attempts, created_at, updated_at, idempotency_key) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?) ON CONFLICT(job_id) DO NOTHING",
  )
    .bind(jobId, context.uid, kind, payloadJson, now, now, idempotencyKey)
    .run();
  if (inserted.meta?.changes !== 1) {
    const existing = await c.env.APP_DB.prepare(
      "SELECT status FROM cf_jobs WHERE job_id = ? AND uid = ?",
    )
      .bind(jobId, context.uid)
      .first<{ status: string }>();
    if (!existing) return c.json({ error: "job id conflict" }, 409);
    return c.json({ status: "already_queued", jobId, state: existing.status });
  }

  try {
    await c.env.JOBS.send({ jobId, uid: context.uid, kind, payload });
  } catch {
    await c.env.APP_DB.prepare(
      "UPDATE cf_jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE job_id = ?",
    )
      .bind("queue unavailable", now, jobId)
      .run();
    return c.json({ error: "queue unavailable" }, 503);
  }
  return c.json({ status: "queued", jobId }, 202);
}

app.post("/v1/cf/jobs", async (c) => {
  if (!(await hasValidContext(c)))
    return c.json({ error: "unauthorized" }, 401);
  const { context } = requestContext(c);
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
  if (!(await hasValidContext(c)))
    return c.json({ error: "unauthorized" }, 401);
  const { context } = requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);

  const idempotencyKey = c.req.header("idempotency-key")?.trim() || null;
  if (idempotencyKey && idempotencyKey.length > MAX_IDEMPOTENCY_KEY_LENGTH) {
    return c.json({ error: "idempotency key too long" }, 400);
  }
  if (idempotencyKey) {
    const existing = await c.env.APP_DB.prepare(
      "SELECT job_id, status FROM cf_jobs WHERE uid = ? AND kind = 'transcribe' AND idempotency_key = ?",
    )
      .bind(context.uid, idempotencyKey)
      .first<{ job_id: string; status: string }>();
    if (existing)
      return c.json({
        status: "already_queued",
        jobId: existing.job_id,
        state: existing.status,
      });
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

  const jobId = crypto.randomUUID();
  const objectKey = `cf-transcriptions/${context.uid}/${jobId}`;
  try {
    await c.env.ASSETS.put(objectKey, body, {
      httpMetadata: { contentType },
      customMetadata: { uid: context.uid, jobId },
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
      { objectKey, contentType },
      idempotencyKey,
    );
  } catch {
    await c.env.ASSETS.delete(objectKey);
    return c.json({ error: "transcription job unavailable" }, 503);
  }
  if (response.status >= 400) await c.env.ASSETS.delete(objectKey);
  return response;
});

async function getJobStatus(c: Context<{ Bindings: JobsEnv }>) {
  if (!(await hasValidContext(c)))
    return c.json({ error: "unauthorized" }, 401);
  const { context } = requestContext(c);
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

async function markJobFailed(env: JobsEnv, jobId: string, error: string) {
  const now = Math.floor(Date.now() / 1000);
  await env.APP_DB.prepare(
    "UPDATE cf_jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE job_id = ?",
  )
    .bind(error, now, jobId)
    .run();
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
      "invalid transcription payload",
    );
    message.ack();
    return;
  }
  const object = await env.ASSETS.get(payload.objectKey);
  if (!object) {
    await markJobFailed(env, message.body.jobId, "staged audio not found");
    message.ack();
    return;
  }
  const body = await object.arrayBuffer();
  if (!body.byteLength || body.byteLength > MAX_TRANSCRIPTION_AUDIO_BYTES) {
    await env.ASSETS.delete(payload.objectKey);
    await markJobFailed(env, message.body.jobId, "staged audio is invalid");
    message.ack();
    return;
  }
  const model =
    payload.model || env.WORKERS_AI_ASR_MODEL || DEFAULT_WORKERS_AI_ASR_MODEL;
  try {
    const result = await env.AI.run(model, { audio: base64Encode(body) });
    const normalized = normalizedTranscription(result, model);
    const resultJson = normalized ? JSON.stringify(normalized) : "";
    if (!normalized || resultJson.length > MAX_TRANSCRIPTION_RESULT_BYTES) {
      await env.ASSETS.delete(payload.objectKey);
      await markJobFailed(
        env,
        message.body.jobId,
        "workers ai returned invalid transcription",
      );
      message.ack();
      return;
    }
    await env.APP_DB.prepare(
      "UPDATE cf_jobs SET status = 'completed', result_json = ?, last_error = NULL, updated_at = ? WHERE job_id = ?",
    )
      .bind(resultJson, now, message.body.jobId)
      .run();
    await env.ASSETS.delete(payload.objectKey);
    message.ack();
  } catch {
    if (message.attempts >= 3) {
      await env.ASSETS.delete(payload.objectKey);
      await markJobFailed(
        env,
        message.body.jobId,
        "workers ai transcription unavailable",
      );
      message.ack();
      return;
    }
    await env.APP_DB.prepare(
      "UPDATE cf_jobs SET status = 'queued', last_error = ?, updated_at = ? WHERE job_id = ?",
    )
      .bind("workers ai transcription unavailable", now, message.body.jobId)
      .run();
    message.retry({ delaySeconds: 10 });
  }
}

export default {
  fetch: app.fetch,
  async queue(batch: MessageBatch<JobMessage>, env: JobsEnv): Promise<void> {
    for (const message of batch.messages) {
      const row = await env.APP_DB.prepare(
        "SELECT status, kind FROM cf_jobs WHERE job_id = ?",
      )
        .bind(message.body.jobId)
        .first<{ status: string; kind: string }>();
      if (!row || row.status === "completed") {
        message.ack();
        continue;
      }
      const now = Math.floor(Date.now() / 1000);
      await env.APP_DB.prepare(
        "UPDATE cf_jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE job_id = ?",
      )
        .bind(now, message.body.jobId)
        .run();
      if (row.kind === "transcribe" && message.body.kind === "transcribe") {
        await processTranscription(message, env, now);
        continue;
      }
      if (row.kind !== "probe" || message.body.kind !== "probe") {
        await markJobFailed(env, message.body.jobId, "unsupported job kind");
        message.ack();
        continue;
      }
      await env.APP_DB.prepare(
        "UPDATE cf_jobs SET status = 'completed', updated_at = ? WHERE job_id = ?",
      )
        .bind(now, message.body.jobId)
        .run();
      message.ack();
    }
  },
};
