import type { Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

const MAX_HUME_WEBHOOK_BYTES = 2 * 1024 * 1024;
const HUME_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60;
const HUME_WEBHOOK_RETRY_ERROR = "queue admission failed";
const HUME_WEBHOOK_RESULT_UNAVAILABLE_ERROR = "hume result unavailable";
const HUME_JOB_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const HUME_STATUS = /^[A-Za-z0-9._:-]{1,64}$/;
const HUME_CALLBACK_STATUSES = new Set(["COMPLETED", "FAILED"]);
const MAX_HUME_PREDICTIONS = 2_000;
const MAX_HUME_EMOTIONS_PER_PREDICTION = 128;
const MAX_HUME_TIME_SECONDS = 24 * 60 * 60;
const MAX_HUME_PREDICTIONS_JSON_BYTES = 512 * 1024;

type HumeWebhookRow = {
  event_id: string;
  job_id: string;
  payload_sha256: string;
  status: "pending" | "queued" | "failed";
  last_error: string | null;
};

type HumeWebhookResultRow = {
  event_id: string;
  job_id: string;
  callback_status: "COMPLETED" | "FAILED";
  mapping_status: "unmapped" | "attested";
  processing_status: "pending" | "completed" | "failed";
  prediction_count: number;
  predictions_json: string;
  result_json: string | null;
  last_error: string | null;
};

type HumeEmotion = { name: string; score: number };
type HumePrediction = { start: number; end: number; emotions: HumeEmotion[] };

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function finiteSeconds(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  if (value < 0 || value > MAX_HUME_TIME_SECONDS) return null;
  return value;
}

function boundedPredictionsJson(predictions: HumePrediction[]) {
  // Keep the persisted normalized result below the D1 CHECK even when a
  // provider sends the maximum number of nested predictions/emotions. The
  // order is provider order; truncating from the tail is deterministic and
  // never changes an earlier interval's values.
  let bounded = predictions;
  let json = JSON.stringify(bounded);
  while (
    new TextEncoder().encode(json).byteLength >
      MAX_HUME_PREDICTIONS_JSON_BYTES &&
    bounded.length > 0
  ) {
    bounded = bounded.slice(0, -1);
    json = JSON.stringify(bounded);
  }
  return { predictions: bounded, json };
}

/**
 * Normalize only the bounded prosody shape used by the legacy parser.
 * Unknown/malformed nested entries are ignored; no caller-controlled uid or
 * conversation id is inferred from provider data.
 */
export function parseHumeWebhookPredictions(
  body: Record<string, unknown>,
): HumePrediction[] {
  if (!Array.isArray(body.predictions) || body.predictions.length === 0) {
    return [];
  }
  const firstResult = objectValue(body.predictions[0]);
  const results = objectValue(firstResult?.results);
  const rawPredictions = results?.predictions;
  if (!Array.isArray(rawPredictions)) return [];

  const normalized: HumePrediction[] = [];
  for (const rawPrediction of rawPredictions) {
    if (normalized.length >= MAX_HUME_PREDICTIONS) break;
    const prediction = objectValue(rawPrediction);
    const models = objectValue(prediction?.models);
    const prosody = objectValue(models?.prosody);
    const grouped = prosody?.grouped_predictions;
    if (!Array.isArray(grouped)) continue;
    for (const rawGroup of grouped) {
      if (normalized.length >= MAX_HUME_PREDICTIONS) break;
      const group = objectValue(rawGroup);
      if (!Array.isArray(group?.predictions)) continue;
      for (const rawSegment of group.predictions) {
        if (normalized.length >= MAX_HUME_PREDICTIONS) break;
        const segment = objectValue(rawSegment);
        const time = objectValue(segment?.time);
        const start = finiteSeconds(time?.begin);
        const end = finiteSeconds(time?.end);
        if (start === null || end === null || end < start) continue;
        const emotions: HumeEmotion[] = [];
        if (Array.isArray(segment?.emotions)) {
          for (const rawEmotion of segment.emotions) {
            if (emotions.length >= MAX_HUME_EMOTIONS_PER_PREDICTION) break;
            const emotion = objectValue(rawEmotion);
            const name =
              typeof emotion?.name === "string" ? emotion.name.trim() : "";
            const score = emotion?.score;
            if (
              !name ||
              name.length > 128 ||
              typeof score !== "number" ||
              !Number.isFinite(score)
            ) {
              continue;
            }
            // Hume emotion scores are probabilities. Rejecting out-of-range
            // values keeps downstream aggregation numeric and bounded.
            if (score < 0 || score > 1) continue;
            emotions.push({ name, score });
          }
        }
        normalized.push({ start, end, emotions });
      }
    }
  }
  return normalized;
}

function ownedArrayBuffer(value: Uint8Array): ArrayBuffer {
  return Uint8Array.from(value).buffer;
}

function constantTimeEqual(left: Uint8Array, right: Uint8Array): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

async function hmacSha256(secret: string, value: Uint8Array) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return new Uint8Array(
    await crypto.subtle.sign("HMAC", key, ownedArrayBuffer(value)),
  );
}

function hexBytes(value: string): Uint8Array | null {
  if (!/^[a-f0-9]{64}$/i.test(value)) return null;
  const result = new Uint8Array(32);
  for (let index = 0; index < result.length; index += 1) {
    result[index] = Number.parseInt(value.slice(index * 2, index * 2 + 2), 16);
  }
  return result;
}

export async function verifyHumeWebhookSignature(
  rawBody: Uint8Array,
  signatureHeader: string | null,
  timestampHeader: string | null,
  signingKey: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1_000),
): Promise<boolean> {
  if (!signatureHeader || !timestampHeader || !signingKey?.trim()) return false;
  const timestamp = timestampHeader.trim();
  if (!/^\d{1,12}$/.test(timestamp)) return false;
  const timestampSeconds = Number(timestamp);
  if (
    !Number.isSafeInteger(timestampSeconds) ||
    Math.abs(nowSeconds - timestampSeconds) > HUME_TIMESTAMP_TOLERANCE_SECONDS
  ) {
    return false;
  }
  const normalizedSignature = signatureHeader.trim().replace(/^sha256=/i, "");
  const signature = hexBytes(normalizedSignature);
  if (!signature) return false;
  const timestampBytes = new TextEncoder().encode(`.${timestamp}`);
  const signedPayload = new Uint8Array(timestampBytes.length + rawBody.length);
  signedPayload.set(rawBody);
  signedPayload.set(timestampBytes, rawBody.length);
  const expected = await hmacSha256(signingKey.trim(), signedPayload);
  return constantTimeEqual(signature, expected);
}

async function boundedRequestBody(request: Request): Promise<Uint8Array> {
  const declared = Number(request.headers.get("content-length"));
  if (
    Number.isFinite(declared) &&
    (declared < 0 || declared > MAX_HUME_WEBHOOK_BYTES)
  ) {
    throw new Error("request body too large");
  }
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_HUME_WEBHOOK_BYTES) {
        await reader.cancel();
        throw new Error("request body too large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

async function sha256Hex(value: Uint8Array): Promise<string> {
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", ownedArrayBuffer(value)),
  );
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

async function webhookRow(env: JobsEnv, eventId: string) {
  return env.APP_DB.prepare(
    "SELECT event_id, job_id, payload_sha256, status, last_error " +
      "FROM cf_hume_webhook_events WHERE event_id = ?",
  )
    .bind(eventId)
    .first<HumeWebhookRow>();
}

async function webhookResultRow(env: JobsEnv, eventId: string) {
  return env.APP_DB.prepare(
    "SELECT event_id, job_id, callback_status, mapping_status, processing_status, " +
      "prediction_count, predictions_json, result_json, last_error " +
      "FROM cf_hume_webhook_results WHERE event_id = ?",
  )
    .bind(eventId)
    .first<HumeWebhookResultRow>();
}

function accepted(jobId: string) {
  return Response.json(
    { status: "accepted", job_id: jobId },
    { status: 202, headers: { "cache-control": "no-store" } },
  );
}

export function registerHumeWebhookRoutes(app: Hono<{ Bindings: JobsEnv }>) {
  app.post("/v1/agents/hume/callback", async (c) => {
    if (!c.env.HUME_WEBHOOK_SIGNING_KEY?.trim()) {
      return c.json({ error: "hume_callback_unavailable" }, 503);
    }

    let rawBody: Uint8Array;
    try {
      rawBody = await boundedRequestBody(c.req.raw);
    } catch (error) {
      return c.json(
        {
          error:
            error instanceof Error && error.message === "request body too large"
              ? "request_body_too_large"
              : "invalid_request",
        },
        error instanceof Error && error.message === "request body too large"
          ? 413
          : 400,
      );
    }

    if (
      !(await verifyHumeWebhookSignature(
        rawBody,
        c.req.header("x-hume-ai-webhook-signature") ?? null,
        c.req.header("x-hume-ai-webhook-timestamp") ?? null,
        c.env.HUME_WEBHOOK_SIGNING_KEY,
      ))
    ) {
      return c.json({ detail: "Invalid signature" }, 400);
    }

    let body: Record<string, unknown> | null;
    try {
      body = objectValue(
        JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(rawBody)),
      );
    } catch {
      return c.json({ error: "invalid_request" }, 400);
    }
    if (!body) return c.json({ error: "invalid_request" }, 400);
    const jobId = typeof body?.job_id === "string" ? body.job_id : "";
    const callbackStatus = typeof body?.status === "string" ? body.status : "";
    if (
      !HUME_JOB_ID.test(jobId) ||
      !HUME_STATUS.test(callbackStatus) ||
      !HUME_CALLBACK_STATUSES.has(callbackStatus)
    ) {
      return c.json({ error: "invalid_request" }, 400);
    }

    const eventId = `hume:${jobId}`;
    const payloadSha256 = await sha256Hex(rawBody);
    const parsedPredictions =
      callbackStatus === "COMPLETED" ? parseHumeWebhookPredictions(body) : [];
    const bounded = boundedPredictionsJson(parsedPredictions);
    const predictions = bounded.predictions;
    const predictionsJson = bounded.json;
    const now = Math.floor(Date.now() / 1_000);
    try {
      await c.env.APP_DB.prepare(
        "INSERT OR IGNORE INTO cf_hume_webhook_events " +
          "(event_id, job_id, callback_status, payload_sha256, status, created_at, updated_at) " +
          "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
      )
        .bind(eventId, jobId, callbackStatus, payloadSha256, now, now)
        .run();
      const existing = await webhookRow(c.env, eventId);
      if (!existing) return c.json({ error: "hume_callback_unavailable" }, 503);
      if (existing.payload_sha256 !== payloadSha256) {
        return c.json({ error: "hume_payload_mismatch" }, 409);
      }
      await c.env.APP_DB.prepare(
        "INSERT OR IGNORE INTO cf_hume_webhook_results " +
          "(event_id, job_id, callback_status, mapping_status, processing_status, " +
          "prediction_count, predictions_json, created_at, updated_at) " +
          "VALUES (?, ?, ?, 'unmapped', 'pending', ?, ?, ?, ?)",
      )
        .bind(
          eventId,
          jobId,
          callbackStatus,
          predictions.length,
          predictionsJson,
          now,
          now,
        )
        .run();
      if (existing.status === "queued") return accepted(jobId);
      if (
        existing.status === "failed" &&
        existing.last_error !== HUME_WEBHOOK_RETRY_ERROR
      ) {
        return c.json({ error: "hume_callback_unavailable" }, 503);
      }
      const claimed = await c.env.APP_DB.prepare(
        "UPDATE cf_hume_webhook_events SET status = 'queued', updated_at = ? " +
          "WHERE event_id = ? AND payload_sha256 = ? AND " +
          "(status = 'pending' OR (status = 'failed' AND last_error = ?))",
      )
        .bind(now, eventId, payloadSha256, HUME_WEBHOOK_RETRY_ERROR)
        .run();
      if (claimed.meta?.changes !== 1) return accepted(jobId);
      try {
        await c.env.JOBS.send({
          jobId: eventId,
          uid: "hume-webhook",
          kind: "hume_webhook",
          payload: { event_id: eventId },
        });
      } catch {
        await c.env.APP_DB.prepare(
          "UPDATE cf_hume_webhook_events SET status = 'failed', attempts = attempts + 1, " +
            "last_error = ?, updated_at = ? WHERE event_id = ? AND status = 'queued'",
        )
          .bind(HUME_WEBHOOK_RETRY_ERROR, now, eventId)
          .run();
        return c.json({ error: "hume_callback_unavailable" }, 503);
      }
      return accepted(jobId);
    } catch {
      return c.json({ error: "hume_callback_unavailable" }, 503);
    }
  });
}

export async function processHumeWebhookMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const eventId =
    typeof message.body.payload.event_id === "string"
      ? message.body.payload.event_id
      : "";
  if (!eventId) {
    message.ack();
    return;
  }
  const row = await webhookRow(env, eventId);
  if (!row || row.status !== "queued") {
    message.ack();
    return;
  }
  const result = await webhookResultRow(env, eventId);
  const now = Math.floor(Date.now() / 1_000);
  if (!result) {
    // This can only happen for an event admitted before the result migration
    // (or after manual data loss). Keep the old receipt auditable and settle
    // explicitly; never infer a task/conversation from job_id alone.
    await env.APP_DB.prepare(
      "UPDATE cf_hume_webhook_events SET status = 'failed', attempts = attempts + 1, " +
        "last_error = ?, updated_at = ? WHERE event_id = ? AND status = 'queued'",
    )
      .bind(HUME_WEBHOOK_RESULT_UNAVAILABLE_ERROR, now, eventId)
      .run();
    message.ack();
    return;
  }
  if (result.processing_status !== "pending") {
    message.ack();
    return;
  }
  const resultJson = JSON.stringify({
    schema_version: 1,
    provider: "hume",
    job_id: result.job_id,
    status: result.callback_status,
    mapping_status: result.mapping_status,
    prediction_count: result.prediction_count,
    predictions: JSON.parse(result.predictions_json),
  });
  const settled = await env.APP_DB.prepare(
    "UPDATE cf_hume_webhook_results SET processing_status = 'completed', result_json = ?, " +
      "last_error = NULL, processed_at = ?, updated_at = ? " +
      "WHERE event_id = ? AND processing_status = 'pending'",
  )
    .bind(resultJson, now, now, eventId)
    .run();
  if (settled.meta?.changes !== 1) {
    message.ack();
    return;
  }
  await env.APP_DB.prepare(
    "UPDATE cf_hume_webhook_events SET attempts = attempts + 1, last_error = NULL, updated_at = ? " +
      "WHERE event_id = ? AND status = 'queued'",
  )
    .bind(now, eventId)
    .run();
  message.ack();
}

export async function cleanupExpiredHumeWebhookEvents(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
): Promise<void> {
  const cutoff = now - 30 * 24 * 60 * 60;
  await env.APP_DB.prepare(
    "DELETE FROM cf_hume_webhook_results WHERE updated_at < ?",
  )
    .bind(cutoff)
    .run();
  await env.APP_DB.prepare(
    "DELETE FROM cf_hume_webhook_events WHERE updated_at < ?",
  )
    .bind(cutoff)
    .run();
}
