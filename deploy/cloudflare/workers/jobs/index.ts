import { Hono, type Context } from "hono";
import { verifyRequestAuthContext } from "../shared/auth-context";
import {
  recordFairUseUsage,
  speechMsFromTranscription,
} from "../shared/fair-use-meter";
import {
  fairUseRestrictionResponse,
  readFairUseRestriction,
} from "../shared/fair-use-enforcement";
import type { JobMessage, JobsEnv } from "./env";
import { evaluateFairUseBatch } from "./fair-use-evaluator";
import { drainNotifications } from "./firebase-messaging";
import {
  LegacyAudioSourceError,
  legacyAudioFiles,
  legacyAudioFilesFingerprint,
  legacyAudioReadiness,
  isLegacyAudioPathSegment,
  rebuildLegacyConversationAudio,
} from "./legacy-audio-import";
import {
  cleanupExpiredSyncState,
  cleanupOrphanPlaybackObjects,
  processSyncJobMessage,
  reconcileSyncJobs,
  registerSyncRoutes,
} from "./sync-local-files";
import {
  readAccountProductResidual,
  validAccountDeletionUid,
} from "./account-deletion-residual";
import {
  cleanupExpiredAccountDeletionTombstones,
  processAccountDeletionMessage,
  reconcileAccountDeletions,
  registerAccountDeletionRoutes,
} from "./account-deletion";
import {
  processRecordingDeletionMessage,
  reconcileRecordingDeletions,
  registerRecordingDeletionRoutes,
} from "./recording-deletion";
import {
  processStripeWebhookMessage,
  reconcileStripeWebhookEvents,
  registerStripeBillingRoutes,
} from "./stripe-billing";
import { registerCreatorPaymentRoutes } from "./creator-payments";
import {
  processAppDeletionMessage,
  reconcileAppDeletions,
  registerAppDeletionRoutes,
} from "./app-deletion";
import { registerAppMutationRoutes } from "./app-mutations";
import { registerAppModerationRoutes } from "./app-moderation";
import { registerAppApiKeyRoutes } from "./app-api-keys";
import { registerMcpApiKeyRoutes } from "./mcp-api-keys";
import { registerDeveloperApiKeyRoutes } from "./developer-api-keys";
import { drainIntegrationWebhooks } from "./integration-webhooks";
import { drainDeveloperWebhooks } from "./developer-webhooks";
import {
  processVectorProjectionMessage,
  reconcileVectorProjections,
} from "./vector-projection";
import {
  processConversationFinalizationMessage,
  reconcileConversationFinalizations,
} from "./conversation-finalization";
import {
  processConversationMergeMessage,
  reconcileConversationMerges,
} from "./conversation-merge";
import { reconcileXConnections, registerXConnectorRoutes } from "./x-connector";
import {
  cleanupExpiredTaskIntegrationOAuthStates,
  registerTaskIntegrationRoutes,
} from "./task-integrations";
import {
  cleanupExpiredGoogleCalendarOAuthStates,
  registerGoogleCalendarRoutes,
} from "./google-calendar";
import { registerAdminNotificationRoutes } from "./admin-notification";
import { registerTwitterProfileRoutes } from "./twitter-profile";

const app = new Hono<{ Bindings: JobsEnv }>();
const MAX_PAYLOAD_BYTES = 16_000;
const MAX_TRANSCRIPTION_AUDIO_BYTES = 5_000_000;
const MAX_TRANSCRIPTION_RESULT_BYTES = 1_000_000;
const MAX_IDEMPOTENCY_KEY_LENGTH = 128;
const JOB_LEASE_SECONDS = 15 * 60;
const QUEUE_RETRY_DELAY_SECONDS = 10;
const MAX_TRANSCRIPTION_PROVIDER_ATTEMPTS = 3;
const MAX_LEGACY_REBUILD_ATTEMPTS = 3;
const ASSET_CLEANUP_BATCH_SIZE = 50;
const DEFAULT_WORKERS_AI_ASR_MODEL = "@cf/openai/whisper-large-v3-turbo";

app.get("/health", (c) =>
  c.json({ status: "ok", service: "jobs", version: "cf-09" }),
);

app.get("/ready", async (c) => {
  try {
    const [auth, database] = await Promise.all([
      c.env.AUTH.fetch(new Request("https://auth.internal/ready")),
      c.env.APP_DB.prepare("SELECT 1 AS ready").first<{ ready?: unknown }>(),
    ]);
    await auth.arrayBuffer();
    if (!auth.ok || Number(database?.ready) !== 1) {
      return c.json({ status: "degraded", service: "jobs" }, 503);
    }
    return c.json({ status: "ready", service: "jobs" });
  } catch {
    return c.json({ status: "degraded", service: "jobs" }, 503);
  }
});

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

registerSyncRoutes(app, requestContext);
registerAccountDeletionRoutes(app, requestContext);
registerRecordingDeletionRoutes(app, requestContext);
registerStripeBillingRoutes(app, requestContext);
registerCreatorPaymentRoutes(app, requestContext);
registerAppMutationRoutes(app, requestContext);
registerAppDeletionRoutes(app, requestContext);
registerAppModerationRoutes(app);
registerAppApiKeyRoutes(app, requestContext);
registerMcpApiKeyRoutes(app, requestContext);
registerDeveloperApiKeyRoutes(app, requestContext);
registerXConnectorRoutes(app, requestContext);
registerTaskIntegrationRoutes(app, requestContext);
registerGoogleCalendarRoutes(app, requestContext);
registerAdminNotificationRoutes(app);
registerTwitterProfileRoutes(app, requestContext);

// The same exhaustive product-D1/R2 residual boundary is used by the local
// deletion state machine and remains available to signed internal audits.
app.get("/internal/users/:uid/residual", async (c) => {
  const uid = c.req.param("uid");
  if (!validAccountDeletionUid(uid)) {
    return c.json({ error: "invalid_request" }, 400);
  }
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  if (context.uid !== uid) return c.json({ error: "forbidden" }, 403);
  try {
    return c.json(await readAccountProductResidual(c.env, uid));
  } catch {
    return c.json({ error: "product_residual_unavailable" }, 503);
  }
});

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
  requeueTerminal = false,
): Promise<Response> {
  if (existing.request_fingerprint !== fingerprint) {
    return c.json({ error: `${identity} reused with different payload` }, 409);
  }
  const canRepairQueue =
    existing.status === "failed" && existing.last_error === "queue unavailable";
  const canRebuildArtifact =
    requeueTerminal &&
    (existing.status === "failed" || existing.status === "completed");
  if (canRepairQueue || canRebuildArtifact) {
    const now = Math.floor(Date.now() / 1000);
    const repaired = await c.env.APP_DB.prepare(
      "UPDATE cf_jobs SET payload_json = ?, status = 'queued', attempts = 0, result_json = NULL, last_error = NULL, updated_at = ? " +
        "WHERE job_id = ? AND uid = ? AND status = ? AND request_fingerprint = ?",
    )
      .bind(
        payloadJson,
        now,
        existing.job_id,
        context.uid,
        existing.status,
        fingerprint,
      )
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

function parseLegacyAudioRebuildPayload(payload: Record<string, unknown>) {
  const conversationId =
    typeof payload.conversationId === "string" ? payload.conversationId : "";
  const audioFilesFingerprint =
    typeof payload.audioFilesFingerprint === "string"
      ? payload.audioFilesFingerprint
      : "";
  if (
    !isLegacyAudioPathSegment(conversationId) ||
    !/^[a-f0-9]{64}$/.test(audioFilesFingerprint)
  ) {
    return null;
  }
  return { conversationId, audioFilesFingerprint };
}

async function enqueueJob(
  c: Context<{ Bindings: JobsEnv }>,
  context: { uid: string },
  jobId: string,
  kind: JobMessage["kind"],
  payload: Record<string, unknown>,
  idempotencyKey: string | null = null,
  fingerprintOverride: string | null = null,
  requeueTerminal = false,
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
        requeueTerminal,
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
      requeueTerminal,
    );
  }
  return publishJob(c, context, jobId, kind, payload);
}

type LegacyConversationRow = {
  is_locked: number;
  audio_files_json: string;
  conversation_audio_json: string | null;
};

app.post("/v1/sync/audio/:conversationId/precache", async (c) => {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  const conversationId = c.req.param("conversationId").trim();
  if (!isLegacyAudioPathSegment(conversationId))
    return c.json({ error: "invalid conversation id" }, 400);

  let row: LegacyConversationRow | null;
  try {
    row = await c.env.APP_DB.prepare(
      "SELECT is_locked, audio_files_json, conversation_audio_json FROM cf_conversations WHERE uid = ? AND id = ?",
    )
      .bind(context.uid, conversationId)
      .first<LegacyConversationRow>();
  } catch {
    return c.json({ error: "recordings unavailable" }, 503);
  }
  if (!row) return c.json({ error: "conversation not found" }, 404);
  if (row.is_locked) {
    return c.json(
      { error: "A paid plan is required to access this conversation." },
      402,
    );
  }
  const audioFiles = legacyAudioFiles(row.audio_files_json);
  if (!audioFiles.length) {
    return c.json({
      status: "no_audio",
      message: "No audio files in conversation",
    });
  }

  let readiness;
  try {
    readiness = await legacyAudioReadiness(
      c.env,
      context.uid,
      conversationId,
      audioFiles,
      row.conversation_audio_json,
    );
  } catch {
    return c.json({ error: "recordings unavailable" }, 503);
  }
  if (
    readiness.readyAudioFileCount === readiness.audioFileCount &&
    readiness.denseReady
  ) {
    return c.json({
      status: "started",
      audio_file_count: readiness.audioFileCount,
    });
  }

  const audioFilesFingerprint = await legacyAudioFilesFingerprint(audioFiles);
  const identityDigest = await sha256Hex(
    `${context.uid}\0${conversationId}\0${audioFilesFingerprint}`,
  );
  const jobId = `legacy-audio-${identityDigest.slice(0, 48)}`;
  const response = await enqueueJob(
    c,
    context,
    jobId,
    "legacy_audio_rebuild",
    { conversationId, audioFilesFingerprint },
    `legacy-audio:${identityDigest}`,
    await requestFingerprint("legacy_audio_rebuild", {
      conversationId,
      audioFilesFingerprint,
    }),
    true,
  );
  if (!response.ok) return response;
  const queued = (await response.json()) as {
    jobId?: string;
    state?: string;
  };
  return c.json(
    {
      status: "started",
      audio_file_count: readiness.audioFileCount,
      ready_audio_file_count: readiness.readyAudioFileCount,
      job_id: queued.jobId || jobId,
      ...(queued.state ? { job_state: queued.state } : {}),
    },
    response.status === 202 ? 202 : 200,
  );
});

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

  const restriction = await readFairUseRestriction(c.env.APP_DB, context.uid);
  if (restriction) return fairUseRestrictionResponse(restriction);

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

async function retryTranscriptionFailure(
  message: Message<JobMessage>,
  env: JobsEnv,
  now: number,
  error: string,
): Promise<void> {
  if (message.attempts >= MAX_TRANSCRIPTION_PROVIDER_ATTEMPTS) {
    await markJobFailed(env, message.body.jobId, message.body.uid, error);
    await retryTerminalFailure(message, env);
    return;
  }
  await env.APP_DB.prepare(
    "UPDATE cf_jobs SET status = 'queued', last_error = ?, updated_at = ? WHERE job_id = ? AND uid = ?",
  )
    .bind(error, now, message.body.jobId, message.body.uid)
    .run();
  message.retry({ delaySeconds: QUEUE_RETRY_DELAY_SECONDS });
}

async function processTranscription(
  message: Message<JobMessage>,
  env: JobsEnv,
  now: number,
): Promise<void> {
  const restriction = await readFairUseRestriction(
    env.APP_DB,
    message.body.uid,
    now,
  );
  if (restriction) {
    await markJobFailed(
      env,
      message.body.jobId,
      message.body.uid,
      "fair use restricted",
    );
    await acknowledgeAfterCleanup(message, env);
    return;
  }
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
  let result: unknown;
  try {
    result = await env.AI.run(model, { audio: base64Encode(body) });
  } catch {
    await retryTranscriptionFailure(
      message,
      env,
      now,
      "workers ai transcription unavailable",
    );
    return;
  }
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
  const speechMs = speechMsFromTranscription(normalized);
  try {
    if (speechMs > 0) {
      await recordFairUseUsage(env.APP_DB, {
        uid: message.body.uid,
        sourceKind: "sync_fresh",
        sourceId: `async:${message.body.jobId}`,
        occurredAt: now,
        speechMs,
      });
    }
  } catch {
    await retryTranscriptionFailure(
      message,
      env,
      now,
      "fair use meter unavailable",
    );
    return;
  }
  await env.APP_DB.prepare(
    "UPDATE cf_jobs SET status = 'completed', result_json = ?, last_error = NULL, updated_at = ? WHERE job_id = ? AND uid = ?",
  )
    .bind(resultJson, now, message.body.jobId, message.body.uid)
    .run();
  await acknowledgeAfterCleanup(message, env);
}

async function processLegacyAudioRebuild(
  message: Message<JobMessage>,
  env: JobsEnv,
  now: number,
): Promise<void> {
  const payload = parseLegacyAudioRebuildPayload(message.body.payload);
  if (!payload) {
    await markJobFailed(
      env,
      message.body.jobId,
      message.body.uid,
      "invalid legacy audio rebuild payload",
    );
    message.ack();
    return;
  }
  try {
    const result = await rebuildLegacyConversationAudio(
      env,
      { uid: message.body.uid, job_id: message.body.jobId },
      payload.conversationId,
      payload.audioFilesFingerprint,
      now,
    );
    await env.APP_DB.prepare(
      "UPDATE cf_jobs SET status = 'completed', result_json = ?, last_error = NULL, updated_at = ? WHERE job_id = ? AND uid = ?",
    )
      .bind(JSON.stringify(result), now, message.body.jobId, message.body.uid)
      .run();
    message.ack();
  } catch (error) {
    if (error instanceof LegacyAudioSourceError) {
      await markJobFailed(
        env,
        message.body.jobId,
        message.body.uid,
        error.message,
      );
      message.ack();
      return;
    }
    if (message.attempts >= MAX_LEGACY_REBUILD_ATTEMPTS) {
      await markJobFailed(
        env,
        message.body.jobId,
        message.body.uid,
        "legacy audio rebuild unavailable",
      );
      message.ack();
      return;
    }
    await env.APP_DB.prepare(
      "UPDATE cf_jobs SET status = 'queued', last_error = ?, updated_at = ? WHERE job_id = ? AND uid = ?",
    )
      .bind(
        "legacy audio rebuild unavailable",
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
  if (message.body.kind === "account_delete") {
    await processAccountDeletionMessage(message, env);
    return;
  }
  if (message.body.kind === "recording_delete") {
    await processRecordingDeletionMessage(message, env);
    return;
  }
  if (message.body.kind === "sync_local_files") {
    await processSyncJobMessage(message, env);
    return;
  }
  if (message.body.kind === "stripe_webhook") {
    await processStripeWebhookMessage(message, env);
    return;
  }
  if (message.body.kind === "vector_project") {
    await processVectorProjectionMessage(message, env);
    return;
  }
  if (
    message.body.kind === "conversation_finalize" ||
    message.body.kind === "conversation_reprocess"
  ) {
    await processConversationFinalizationMessage(message, env);
    return;
  }
  if (message.body.kind === "conversation_merge") {
    await processConversationMergeMessage(message, env);
    return;
  }
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
    if (message.body.kind === "legacy_audio_rebuild") {
      message.ack();
      return;
    }
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
  if (row.kind === "legacy_audio_rebuild") {
    await processLegacyAudioRebuild(message, env, now);
    return;
  }
  if (row.kind === "app_delete") {
    await processAppDeletionMessage(message, env);
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

async function recordAssetCleanupFailure(
  env: JobsEnv,
  storageKey: string,
  uid: string,
  now: number,
): Promise<void> {
  try {
    await env.APP_DB.prepare(
      "UPDATE cf_asset_cleanup_tasks SET attempts = attempts + 1, last_error = ?, updated_at = ? " +
        "WHERE storage_key = ? AND uid = ?",
    )
      .bind("r2 delete unavailable", now, storageKey, uid)
      .run();
  } catch {
    // The durable task remains eligible for the next scheduled sweep.
  }
}

async function drainAssetCleanup(env: JobsEnv): Promise<void> {
  const now = Math.floor(Date.now() / 1000);
  const result = await env.APP_DB.prepare(
    "SELECT storage_key, uid FROM cf_asset_cleanup_tasks " +
      "WHERE not_before <= ? ORDER BY created_at LIMIT ?",
  )
    .bind(now, ASSET_CLEANUP_BATCH_SIZE)
    .all<{ storage_key: string; uid: string }>();
  for (const row of result.results || []) {
    if (
      typeof row.storage_key !== "string" ||
      typeof row.uid !== "string" ||
      !row.storage_key
    ) {
      continue;
    }
    try {
      const active = await env.APP_DB.prepare(
        "SELECT 1 AS active FROM cf_asset_objects WHERE uid = ? AND storage_key = ? LIMIT 1",
      )
        .bind(row.uid, row.storage_key)
        .first<{ active: number }>();
      if (!active) await env.ASSETS.delete(row.storage_key);
      await env.APP_DB.prepare(
        "DELETE FROM cf_asset_cleanup_tasks WHERE storage_key = ? AND uid = ?",
      )
        .bind(row.storage_key, row.uid)
        .run();
    } catch {
      await recordAssetCleanupFailure(env, row.storage_key, row.uid, now);
    }
  }
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
  async scheduled(
    _controller: ScheduledController,
    env: JobsEnv,
  ): Promise<void> {
    const now = Math.floor(Date.now() / 1_000);
    const syncMaintenance =
      env.SYNC_FRESH && env.SYNC_BACKFILL
        ? [
            reconcileSyncJobs(env, now),
            cleanupExpiredSyncState(env, now),
            cleanupOrphanPlaybackObjects(env, now),
          ]
        : [];
    const results = await Promise.allSettled([
      drainAssetCleanup(env),
      evaluateFairUseBatch(env),
      drainNotifications(env),
      drainIntegrationWebhooks(env, now),
      drainDeveloperWebhooks(env, now),
      reconcileAccountDeletions(env, now),
      reconcileRecordingDeletions(env, now),
      reconcileAppDeletions(env, now),
      cleanupExpiredAccountDeletionTombstones(env, now),
      reconcileStripeWebhookEvents(env, now),
      reconcileVectorProjections(env, now),
      reconcileConversationFinalizations(env, now),
      reconcileConversationMerges(env, now),
      reconcileXConnections(env, now),
      cleanupExpiredTaskIntegrationOAuthStates(env, now),
      cleanupExpiredGoogleCalendarOAuthStates(env, now),
      ...syncMaintenance,
    ]);
    const failure = results.find(
      (result): result is PromiseRejectedResult => result.status === "rejected",
    );
    if (failure) throw failure.reason;
  },
};
