import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

/**
 * Cloudflare does not expose a Queue read/list API to a Worker.  The DLQ is
 * consequently indexed here as it is delivered, and replay only accepts the
 * immutable message ids captured in D1.  A caller can never provide a new
 * JobMessage payload through this boundary.
 */
const MAX_MESSAGE_BODY_BYTES = 16_000;
const MAX_REQUEST_BODY_BYTES = 16_000;
const MAX_MESSAGE_IDS = 50;
const MAX_MESSAGE_ID_LENGTH = 128;
const MAX_IDEMPOTENCY_KEY_LENGTH = 128;
const SIGNATURE_MAX_AGE_SECONDS = 5 * 60;
const DLQ_QUEUE_NAME =
  /^omi-cf-(?:jobs|sync-(?:fresh|backfill))-dlq-[a-z0-9-]+$/;
const JOB_ID = /^[^/\u0000-\u001f\u007f]{1,128}$/;
const UID = /^[^/\u0000-\u001f\u007f]{0,256}$/;
const MESSAGE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;

type JobsContext = Context<{ Bindings: JobsEnv }>;

type DlqJob = {
  jobId: string;
  uid: string;
  kind: JobMessage["kind"];
  payload: Record<string, unknown>;
};

type DlqMessageRow = {
  queue_name: string;
  message_id: string;
  body_sha256: string;
  job_id: string | null;
  uid: string | null;
  kind: JobMessage["kind"] | null;
  payload_json: string | null;
  delivery_attempts: number;
  status:
    "captured" | "invalid" | "replay_queued" | "replayed" | "replay_failed";
  invalid_reason: string | null;
  replay_id: string | null;
  replay_count: number;
};

type ReplayRequestRow = {
  replay_id: string;
  idempotency_key: string;
  request_fingerprint: string;
  requested_count: number;
  queued_count: number;
  skipped_count: number;
  failed_count: number;
  status: "queued" | "completed" | "partial" | "failed";
};

const JOB_KINDS: ReadonlySet<JobMessage["kind"]> = new Set([
  "probe",
  "transcribe",
  "sync_local_files",
  "legacy_audio_rebuild",
  "vector_project",
  "account_delete",
  "recording_delete",
  "app_delete",
  "app_owner_migration",
  "stripe_webhook",
  "conversation_finalize",
  "conversation_reprocess",
  "conversation_merge",
  "audio_merge",
  "audio_merge_legacy",
  "task_intelligence_evaluate",
  "wrapped_generate",
  "hume_webhook",
  "limitless_import",
  "chat_assistant_poll",
  "memory_short_term_lifecycle",
]);

function noStoreHeaders(): Record<string, string> {
  return { "cache-control": "no-store" };
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  let difference = a.length ^ b.length;
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    difference |= (a[index] || 0) ^ (b[index] || 0);
  }
  return difference === 0;
}

function adminAuthorized(c: JobsContext): boolean {
  const expected = c.env.ADMIN_KEY;
  const provided = c.req.header("secret-key");
  return Boolean(expected && provided && constantTimeEqual(provided, expected));
}

function bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function base64Url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

function decodeBase64Url(value: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]{43}$/.test(value)) return null;
  try {
    const padded = `${value}${"=".repeat((4 - (value.length % 4)) % 4)}`;
    const binary = atob(padded.replaceAll("-", "+").replaceAll("_", "/"));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function asArrayBuffer(value: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(value.byteLength);
  copy.set(value);
  return copy.buffer;
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

async function validRequestSignature(
  c: JobsContext,
  timestamp: string,
  idempotencyKey: string,
  body: string,
): Promise<boolean> {
  const secret = c.env.DLQ_REPLAY_SIGNING_SECRET?.trim() || "";
  const timestampNumber = Number(timestamp);
  if (
    secret.length < 32 ||
    !/^\d{1,12}$/.test(timestamp) ||
    !Number.isSafeInteger(timestampNumber) ||
    Math.abs(Math.floor(Date.now() / 1_000) - timestampNumber) >
      SIGNATURE_MAX_AGE_SECONDS
  ) {
    return false;
  }
  const encoded = c.req.header("x-dlq-replay-signature") || "";
  const signature = decodeBase64Url(encoded);
  if (!signature) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  return crypto.subtle.verify(
    "HMAC",
    key,
    asArrayBuffer(signature),
    new TextEncoder().encode(`${timestamp}\n${idempotencyKey}\n${body}`),
  );
}

function jsonObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parseJobMessage(value: unknown): DlqJob | null {
  const object = jsonObject(value);
  if (!object) return null;
  if (
    Object.keys(object).some(
      (key) => !["jobId", "uid", "kind", "payload"].includes(key),
    )
  )
    return null;
  const jobId = typeof object.jobId === "string" ? object.jobId : "";
  const uid = typeof object.uid === "string" ? object.uid : "";
  const kind = object.kind;
  const payload = jsonObject(object.payload);
  if (
    !JOB_ID.test(jobId) ||
    !UID.test(uid) ||
    typeof kind !== "string" ||
    !JOB_KINDS.has(kind as JobMessage["kind"]) ||
    !payload
  )
    return null;
  const payloadJson = JSON.stringify(payload);
  if (!payloadJson || bytes(payloadJson) > MAX_MESSAGE_BODY_BYTES) return null;
  return { jobId, uid, kind: kind as JobMessage["kind"], payload };
}

function serializedBody(value: unknown): string {
  try {
    const body = JSON.stringify(value);
    return body === undefined ? "" : body;
  } catch {
    return "";
  }
}

function boundedMessageId(value: unknown): string | null {
  return typeof value === "string" &&
    value.length <= MAX_MESSAGE_ID_LENGTH &&
    MESSAGE_ID.test(value)
    ? value
    : null;
}

function targetQueue(
  row: DlqMessageRow,
  payload: Record<string, unknown>,
  env: JobsEnv,
): Queue<JobMessage> | null {
  if (row.kind === "sync_local_files") {
    if (payload.lane === "fresh") return env.SYNC_FRESH;
    if (payload.lane === "backfill") return env.SYNC_BACKFILL;
    return null;
  }
  return env.JOBS;
}

function replayResponse(
  c: JobsContext,
  body: Record<string, unknown>,
  status = 200,
): Response {
  return c.json(body, status as 200, noStoreHeaders());
}

/** Record one delivery from the configured DLQ.  The DLQ itself is never read. */
export async function captureDlqMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
  queueName: string,
): Promise<void> {
  if (!DLQ_QUEUE_NAME.test(queueName)) {
    message.ack();
    return;
  }
  const rawBody = serializedBody(message.body);
  const bodySha256 = await sha256Hex(rawBody || "<unserializable>");
  const parsed =
    rawBody && bytes(rawBody) <= MAX_MESSAGE_BODY_BYTES
      ? parseJobMessage(JSON.parse(rawBody))
      : null;
  const now = Math.floor(Date.now() / 1_000);
  const payloadJson = parsed ? JSON.stringify(parsed.payload) : null;
  const status = parsed ? "captured" : "invalid";
  const invalidReason = parsed
    ? null
    : !rawBody
      ? "body is not JSON serializable"
      : bytes(rawBody) > MAX_MESSAGE_BODY_BYTES
        ? "message body too large"
        : "invalid JobMessage envelope";
  // A late delivery for a deleted account must not keep a poison message in
  // the DLQ consumer, and it must not become replayable after the deletion
  // tombstone is written.  The D1 trigger is a second line of defence.
  if (parsed?.uid) {
    const fenced = await env.APP_DB.prepare(
      "SELECT 1 AS fenced FROM cf_account_deletion_intents WHERE uid = ? UNION ALL SELECT 1 AS fenced FROM cf_account_deletion_tombstones WHERE uid = ? LIMIT 1",
    )
      .bind(parsed.uid, parsed.uid)
      .first<{ fenced?: number }>();
    if (fenced) {
      message.ack();
      return;
    }
  }
  await env.APP_DB.prepare(
    "INSERT INTO cf_queue_dlq_messages (queue_name, message_id, body_sha256, job_id, uid, kind, payload_json, delivery_attempts, status, invalid_reason, replay_id, replay_count, captured_at, updated_at, replayed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, NULL) " +
      "ON CONFLICT(queue_name, message_id) DO UPDATE SET delivery_attempts = MAX(delivery_attempts, excluded.delivery_attempts), updated_at = excluded.updated_at WHERE status IN ('captured', 'replay_failed')",
  )
    .bind(
      queueName,
      message.id,
      bodySha256,
      parsed?.jobId || null,
      parsed?.uid || null,
      parsed?.kind || null,
      payloadJson,
      message.attempts,
      status,
      invalidReason,
      now,
      now,
    )
    .run();
  message.ack();
}

async function readRequestBody(c: JobsContext): Promise<string | null> {
  const declaredLength = Number(c.req.header("content-length"));
  if (
    Number.isFinite(declaredLength) &&
    (declaredLength < 1 || declaredLength > MAX_REQUEST_BODY_BYTES)
  )
    return null;
  const raw = await c.req.raw.text();
  return raw && bytes(raw) <= MAX_REQUEST_BODY_BYTES ? raw : null;
}

function parseReplayRequest(
  raw: string,
): { messageIds: string[]; fingerprintInput: string } | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  const object = jsonObject(parsed);
  if (
    !object ||
    Object.keys(object).some((key) => key !== "message_ids") ||
    !Array.isArray(object.message_ids) ||
    object.message_ids.length < 1 ||
    object.message_ids.length > MAX_MESSAGE_IDS
  )
    return null;
  const messageIds = object.message_ids.map(boundedMessageId);
  if (messageIds.some((id): id is null => id === null)) return null;
  const ids = messageIds as string[];
  if (new Set(ids).size !== ids.length) return null;
  const sorted = [...ids].sort();
  return {
    messageIds: ids,
    fingerprintInput: JSON.stringify({ message_ids: sorted }),
  };
}

function replayStatus(row: ReplayRequestRow): Record<string, unknown> {
  return {
    replayId: row.replay_id,
    status: row.status,
    requestedCount: row.requested_count,
    queuedCount: row.queued_count,
    skippedCount: row.skipped_count,
    failedCount: row.failed_count,
  };
}

/** Register a bounded, signed operator replay endpoint over the D1 DLQ index. */
export function registerDlqReplayRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
): void {
  app.post("/internal/cf/jobs/dlq/replay", async (c) => {
    if (c.env.DLQ_REPLAY_STAGING_ENABLED !== "true") {
      return replayResponse(c, { error: "dlq_replay_unavailable" }, 503);
    }
    if (!adminAuthorized(c))
      return replayResponse(c, { error: "unauthorized" }, 403);
    const idempotencyKey = c.req.header("idempotency-key")?.trim() || "";
    if (
      !IDEMPOTENCY_KEY.test(idempotencyKey) ||
      idempotencyKey.length > MAX_IDEMPOTENCY_KEY_LENGTH
    ) {
      return replayResponse(c, { error: "idempotency_key_required" }, 400);
    }
    const raw = await readRequestBody(c);
    if (!raw) return replayResponse(c, { error: "invalid_request" }, 400);
    const request = parseReplayRequest(raw);
    if (!request) return replayResponse(c, { error: "invalid_request" }, 400);
    if (
      !(await validRequestSignature(
        c,
        c.req.header("x-dlq-replay-timestamp") || "",
        idempotencyKey,
        raw,
      ))
    ) {
      return replayResponse(c, { error: "invalid_signature" }, 403);
    }

    const requestFingerprint = await sha256Hex(
      `${idempotencyKey}\n${request.fingerprintInput}`,
    );
    const existing = await c.env.APP_DB.prepare(
      "SELECT replay_id, idempotency_key, request_fingerprint, requested_count, queued_count, skipped_count, failed_count, status FROM cf_queue_dlq_replay_requests WHERE idempotency_key = ?",
    )
      .bind(idempotencyKey)
      .first<ReplayRequestRow>();
    if (existing) {
      if (existing.request_fingerprint !== requestFingerprint)
        return replayResponse(c, { error: "idempotency_key_reused" }, 409);
      return replayResponse(c, replayStatus(existing));
    }

    const now = Math.floor(Date.now() / 1_000);
    const replayId = crypto.randomUUID();
    try {
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_queue_dlq_replay_requests (replay_id, idempotency_key, request_fingerprint, requested_count, queued_count, skipped_count, failed_count, status, created_at, updated_at) VALUES (?, ?, ?, ?, 0, 0, 0, 'queued', ?, ?)",
      )
        .bind(
          replayId,
          idempotencyKey,
          requestFingerprint,
          request.messageIds.length,
          now,
          now,
        )
        .run();
    } catch {
      const raced = await c.env.APP_DB.prepare(
        "SELECT replay_id, idempotency_key, request_fingerprint, requested_count, queued_count, skipped_count, failed_count, status FROM cf_queue_dlq_replay_requests WHERE idempotency_key = ?",
      )
        .bind(idempotencyKey)
        .first<ReplayRequestRow>();
      if (raced?.request_fingerprint === requestFingerprint)
        return replayResponse(c, replayStatus(raced));
      return replayResponse(c, { error: "replay_unavailable" }, 503);
    }

    let queuedCount = 0;
    let skippedCount = 0;
    let failedCount = 0;
    for (const messageId of request.messageIds) {
      const row = await c.env.APP_DB.prepare(
        "SELECT queue_name, message_id, body_sha256, job_id, uid, kind, payload_json, delivery_attempts, status, invalid_reason, replay_id, replay_count FROM cf_queue_dlq_messages WHERE queue_name LIKE '%-dlq-%' AND message_id = ?",
      )
        .bind(messageId)
        .first<DlqMessageRow>();
      let itemStatus: "queued" | "skipped" | "failed" = "skipped";
      let reason: string | null = null;
      if (!row) {
        reason = "message_not_found";
      } else if (!DLQ_QUEUE_NAME.test(row.queue_name)) {
        reason = "queue_not_replayable";
      } else if (row.status === "invalid") {
        reason = row.invalid_reason || "invalid_message";
      } else if (row.status === "replay_queued" || row.status === "replayed") {
        reason =
          row.status === "replayed" ? "already_replayed" : "replay_in_flight";
      } else if (
        !row.payload_json ||
        !row.job_id ||
        row.uid === null ||
        !row.kind ||
        !SHA256.test(row.body_sha256)
      ) {
        reason = "message_record_invalid";
      } else {
        const fenced = row.uid
          ? await c.env.APP_DB.prepare(
              "SELECT 1 AS fenced FROM cf_account_deletion_intents WHERE uid = ? UNION ALL SELECT 1 AS fenced FROM cf_account_deletion_tombstones WHERE uid = ? LIMIT 1",
            )
              .bind(row.uid, row.uid)
              .first<{ fenced?: number }>()
          : null;
        if (fenced) {
          reason = "account_deletion_fence";
        }
        let payload: Record<string, unknown> | null = null;
        if (!fenced) {
          try {
            payload = jsonObject(JSON.parse(row.payload_json));
          } catch {
            payload = null;
          }
        }
        const job =
          !fenced && payload
            ? parseJobMessage({
                jobId: row.job_id,
                uid: row.uid,
                kind: row.kind,
                payload,
              })
            : null;
        const queue =
          !fenced && payload ? targetQueue(row, payload, c.env) : null;
        if (!fenced && (!job || !queue)) {
          reason = "replay_target_unavailable";
        } else if (!fenced && job && queue) {
          const claimed = await c.env.APP_DB.prepare(
            "UPDATE cf_queue_dlq_messages SET status = 'replay_queued', replay_id = ?, replay_count = replay_count + 1, updated_at = ? WHERE queue_name = ? AND message_id = ? AND status IN ('captured', 'replay_failed')",
          )
            .bind(replayId, now, row.queue_name, row.message_id)
            .run();
          if (claimed.meta?.changes !== 1) {
            reason = "replay_in_flight";
          } else {
            try {
              await queue.send(job);
              await c.env.APP_DB.prepare(
                "UPDATE cf_queue_dlq_messages SET status = 'replayed', updated_at = ?, replayed_at = ? WHERE queue_name = ? AND message_id = ? AND replay_id = ? AND status = 'replay_queued'",
              )
                .bind(now, now, row.queue_name, row.message_id, replayId)
                .run();
              itemStatus = "queued";
              queuedCount += 1;
            } catch {
              await c.env.APP_DB.prepare(
                "UPDATE cf_queue_dlq_messages SET status = 'replay_failed', invalid_reason = ?, updated_at = ? WHERE queue_name = ? AND message_id = ? AND replay_id = ? AND status = 'replay_queued'",
              )
                .bind(
                  "queue unavailable",
                  now,
                  row.queue_name,
                  row.message_id,
                  replayId,
                )
                .run();
              itemStatus = "failed";
              reason = "queue_unavailable";
              failedCount += 1;
            }
          }
        }
      }
      if (itemStatus === "skipped") skippedCount += 1;
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_queue_dlq_replay_items (replay_id, queue_name, message_id, status, reason, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
      )
        .bind(
          replayId,
          row?.queue_name || "unknown",
          messageId,
          itemStatus,
          reason,
          now,
        )
        .run();
    }
    const status: ReplayRequestRow["status"] =
      failedCount > 0 && queuedCount === 0
        ? "failed"
        : failedCount > 0 || skippedCount > 0
          ? "partial"
          : "completed";
    await c.env.APP_DB.prepare(
      "UPDATE cf_queue_dlq_replay_requests SET queued_count = ?, skipped_count = ?, failed_count = ?, status = ?, updated_at = ? WHERE replay_id = ?",
    )
      .bind(queuedCount, skippedCount, failedCount, status, now, replayId)
      .run();
    const response = {
      replayId,
      status,
      requestedCount: request.messageIds.length,
      queuedCount,
      skippedCount,
      failedCount,
    };
    return replayResponse(c, response, queuedCount > 0 ? 202 : 200);
  });
}
