import type { Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

const MAX_HUME_WEBHOOK_BYTES = 2 * 1024 * 1024;
const HUME_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60;
const HUME_WEBHOOK_RETRY_ERROR = "queue admission failed";
const HUME_WEBHOOK_PROCESSING_ERROR = "hume processing unavailable";
const HUME_JOB_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const HUME_STATUS = /^[A-Za-z0-9._:-]{1,64}$/;
const HUME_CALLBACK_STATUSES = new Set(["COMPLETED", "FAILED"]);

type HumeWebhookRow = {
  event_id: string;
  job_id: string;
  payload_sha256: string;
  status: "pending" | "queued" | "failed";
  last_error: string | null;
};

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
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
  await env.APP_DB.prepare(
    "UPDATE cf_hume_webhook_events SET status = 'failed', attempts = attempts + 1, " +
      "last_error = ?, updated_at = ? WHERE event_id = ? AND status = 'queued'",
  )
    .bind(
      HUME_WEBHOOK_PROCESSING_ERROR,
      Math.floor(Date.now() / 1_000),
      eventId,
    )
    .run();
  message.ack();
}

export async function cleanupExpiredHumeWebhookEvents(
  env: JobsEnv,
  now = Math.floor(Date.now() / 1_000),
): Promise<void> {
  await env.APP_DB.prepare(
    "DELETE FROM cf_hume_webhook_events WHERE updated_at < ?",
  )
    .bind(now - 30 * 24 * 60 * 60)
    .run();
}
