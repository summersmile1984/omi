import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";
import {
  LegacyAudioSourceError,
  legacyAudioFiles,
  rebuildLegacyAudioFileMp3,
  rebuildLegacyConversationMp3,
} from "./legacy-audio-import";
import { recordingStorageEnabled } from "./sync-local-files";

const MAX_PAYLOAD_BYTES = 16_000;
const MAX_ATTEMPTS = 3;
const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const MAX_TIMESTAMPS = 20_000;
const SAFE_FINGERPRINT = /^(?:[a-f0-9]{12}|[a-f0-9]{64})$/;

type LegacyAudioMergeContext = Context<{ Bindings: JobsEnv }>;
type AuthContext = { uid: string };
type LegacyAudioMergeStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

type LegacyAudioMergeJobRow = {
  uid: string;
  job_id: string;
  schema_version: 1 | 2;
  conversation_id: string;
  audio_file_id: string;
  timestamps_json: string | null;
  source_fingerprint: string | null;
  source_prefix: string;
  artifact_key: string;
  output_format: "mp3";
  account_generation: number;
  status: LegacyAudioMergeStatus;
  attempts: number;
  lease_token: string | null;
  lease_until: number | null;
  next_attempt_at: number;
  result_json: string | null;
  last_error: string | null;
  request_fingerprint: string;
  created_at: number;
  updated_at: number;
};

class LegacyAudioMergeHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function validId(value: string): boolean {
  return (
    value.length > 0 &&
    value.length <= 128 &&
    value !== "." &&
    value !== ".." &&
    !value.includes("/") &&
    !value.includes("\\")
  );
}

function objectPayload(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function validTimestamps(value: unknown): value is number[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.length <= MAX_TIMESTAMPS &&
    value.every(
      (timestamp) =>
        typeof timestamp === "number" &&
        Number.isFinite(timestamp) &&
        timestamp > 0 &&
        timestamp <= 4_102_444_800,
    )
  );
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

type LegacyAudioRequest = {
  schemaVersion: 1 | 2;
  uid?: string;
  conversationId: string;
  audioFileId: string;
  timestamps: number[] | null;
  sourceFingerprint: string | null;
};

async function parseRequest(
  c: LegacyAudioMergeContext,
  authenticatedUid: string,
): Promise<LegacyAudioRequest> {
  const declaredLength = Number(c.req.header("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_PAYLOAD_BYTES)
    throw new LegacyAudioMergeHttpError(
      413,
      "payload_too_large",
      "audio merge request is too large",
    );
  let value: unknown;
  try {
    const body = await c.req.raw.arrayBuffer();
    if (body.byteLength > MAX_PAYLOAD_BYTES)
      throw new LegacyAudioMergeHttpError(
        413,
        "payload_too_large",
        "audio merge request is too large",
      );
    value = JSON.parse(new TextDecoder().decode(body));
  } catch (error) {
    if (error instanceof LegacyAudioMergeHttpError) throw error;
    throw new LegacyAudioMergeHttpError(
      400,
      "invalid_request",
      "audio merge request must be JSON",
    );
  }
  const payload = objectPayload(value);
  if (!payload)
    throw new LegacyAudioMergeHttpError(
      400,
      "invalid_request",
      "audio merge request must be an object",
    );
  const allowed = new Set([
    "schema_version",
    "uid",
    "conversation_id",
    "audio_file_id",
    "timestamps",
    "fingerprint",
    "source_fingerprint",
    "output_format",
  ]);
  if (Object.keys(payload).some((key) => !allowed.has(key)))
    throw new LegacyAudioMergeHttpError(
      400,
      "invalid_request",
      "audio merge request contains unsupported fields",
    );
  const schemaVersion = payload.schema_version === undefined
    ? 1
    : payload.schema_version;
  if (schemaVersion !== 1 && schemaVersion !== 2)
    throw new LegacyAudioMergeHttpError(
      400,
      "invalid_schema_version",
      "audio merge schema version is unsupported",
    );
  if (
    payload.uid !== undefined &&
    (typeof payload.uid !== "string" || payload.uid !== authenticatedUid)
  )
    throw new LegacyAudioMergeHttpError(
      403,
      "uid_mismatch",
      "audio merge uid does not match authenticated principal",
    );
  const conversationId =
    typeof payload.conversation_id === "string"
      ? payload.conversation_id.trim()
      : "";
  if (!validId(conversationId))
    throw new LegacyAudioMergeHttpError(
      400,
      "invalid_conversation_id",
      "conversation id is invalid",
    );
  if (payload.output_format !== undefined && payload.output_format !== "mp3")
    throw new LegacyAudioMergeHttpError(
      422,
      "unsupported_output_format",
      "the legacy audio contract produces MP3",
    );
  const audioFileId =
    typeof payload.audio_file_id === "string"
      ? payload.audio_file_id.trim()
      : schemaVersion === 2
        ? "conversation"
        : "";
  if (!validId(audioFileId))
    throw new LegacyAudioMergeHttpError(
      400,
      "invalid_audio_file_id",
      "audio file id is invalid",
    );
  if (schemaVersion === 2 && audioFileId !== "conversation")
    throw new LegacyAudioMergeHttpError(
      422,
      "unsupported_audio_scope",
      "schema version 2 only builds the conversation artifact",
    );
  const timestamps = payload.timestamps;
  if (schemaVersion === 1 && !validTimestamps(timestamps))
    throw new LegacyAudioMergeHttpError(
      400,
      "invalid_timestamps",
      "schema version 1 requires bounded timestamps",
    );
  const suppliedFingerprint = payload.fingerprint ?? payload.source_fingerprint;
  if (
    suppliedFingerprint !== undefined &&
    (typeof suppliedFingerprint !== "string" ||
      !SAFE_FINGERPRINT.test(suppliedFingerprint))
  )
    throw new LegacyAudioMergeHttpError(
      400,
      "invalid_source_fingerprint",
      "source fingerprint is invalid",
    );
  return {
    schemaVersion,
    uid: typeof payload.uid === "string" ? payload.uid : undefined,
    conversationId,
    audioFileId,
    timestamps: schemaVersion === 1 ? [...(timestamps as number[])].sort((a, b) => a - b) : null,
    sourceFingerprint:
      typeof suppliedFingerprint === "string" ? suppliedFingerprint : null,
  };
}

async function accountGeneration(env: JobsEnv, uid: string): Promise<number> {
  const row = await env.APP_DB.prepare(
    "SELECT account_generation FROM cf_account_cutover WHERE uid = ?",
  )
    .bind(uid)
    .first<{ account_generation?: unknown }>();
  const generation = Number(row?.account_generation ?? 0);
  if (!Number.isSafeInteger(generation) || generation < 0)
    throw new LegacyAudioMergeHttpError(
      503,
      "metadata_unavailable",
      "account generation is invalid",
    );
  return generation;
}

async function readJob(
  env: JobsEnv,
  uid: string,
  jobId: string,
): Promise<LegacyAudioMergeJobRow | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_audio_merge_legacy_jobs WHERE uid = ? AND job_id = ?",
  )
    .bind(uid, jobId)
    .first<LegacyAudioMergeJobRow>();
}

function publicJob(row: LegacyAudioMergeJobRow): Record<string, unknown> {
  let result: unknown = null;
  if (row.result_json) {
    try {
      result = JSON.parse(row.result_json);
    } catch {
      result = null;
    }
  }
  return {
    job_id: row.job_id,
    schema_version: row.schema_version,
    conversation_id: row.conversation_id,
    audio_file_id: row.audio_file_id,
    output_format: row.output_format,
    status: row.status,
    attempts: row.attempts,
    result,
    error: row.last_error,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

function queueMessage(row: LegacyAudioMergeJobRow): JobMessage {
  return {
    jobId: row.job_id,
    uid: row.uid,
    kind: "audio_merge_legacy",
    payload: {
      schemaVersion: row.schema_version,
      conversationId: row.conversation_id,
      audioFileId: row.audio_file_id,
      timestamps: row.timestamps_json ? JSON.parse(row.timestamps_json) : null,
      sourceFingerprint: row.source_fingerprint,
      outputFormat: row.output_format,
    },
  };
}

async function enqueue(
  env: JobsEnv,
  row: LegacyAudioMergeJobRow,
): Promise<void> {
  try {
    await env.JOBS.send(queueMessage(row));
  } catch {
    await env.APP_DB.prepare(
      "UPDATE cf_audio_merge_legacy_jobs SET status = 'failed', lease_until = NULL, last_error = ?, updated_at = ? " +
        "WHERE uid = ? AND job_id = ? AND status = 'queued'",
    )
      .bind("queue unavailable", Math.floor(Date.now() / 1000), row.uid, row.job_id)
      .run();
    throw new LegacyAudioMergeHttpError(
      503,
      "queue_unavailable",
      "audio merge queue is unavailable",
    );
  }
}

export function registerLegacyAudioMergeRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (c: LegacyAudioMergeContext) => Promise<AuthContext | null>,
): void {
  const run = async (c: LegacyAudioMergeContext) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const request = await parseRequest(c, context.uid);
      const conversation = await c.env.APP_DB.prepare(
        "SELECT is_locked, audio_files_json FROM cf_conversations WHERE uid = ? AND id = ?",
      )
        .bind(context.uid, request.conversationId)
        .first<{ is_locked: number; audio_files_json: string }>();
      if (!conversation) return c.json({ error: "conversation_not_found" }, 404);
      if (conversation.is_locked) return c.json({ error: "conversation_locked" }, 402);
      if (!(await recordingStorageEnabled(c.env, context.uid)))
        return c.json({ error: "audio_storage_disabled" }, 403);
      const files = legacyAudioFiles(conversation.audio_files_json);
      if (!files.length) return c.json({ error: "no_audio_files" }, 400);
      if (
        request.schemaVersion === 1 &&
        !files.some((file) => file.id === request.audioFileId)
      )
        return c.json({ error: "audio_file_not_found" }, 404);
      const generation = await accountGeneration(c.env, context.uid);
      const requestFingerprint = await sha256Hex(
        JSON.stringify({
          schemaVersion: request.schemaVersion,
          uid: context.uid,
          conversationId: request.conversationId,
          audioFileId: request.audioFileId,
          timestamps: request.timestamps,
          sourceFingerprint: request.sourceFingerprint,
          outputFormat: "mp3",
        }),
      );
      const jobId = `audio-merge-legacy-${requestFingerprint.slice(0, 48)}`;
      const now = Math.floor(Date.now() / 1000);
      const artifactName = request.audioFileId === "conversation"
        ? "conversation"
        : request.audioFileId;
      const artifactKey = `playback/${context.uid}/${request.conversationId}/${artifactName}.mp3`;
      const inserted = await c.env.APP_DB.prepare(
        "INSERT INTO cf_audio_merge_legacy_jobs " +
          "(uid, job_id, schema_version, conversation_id, audio_file_id, timestamps_json, source_fingerprint, source_prefix, artifact_key, output_format, account_generation, status, attempts, next_attempt_at, request_fingerprint, created_at, updated_at) " +
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'mp3', ?, 'queued', 0, ?, ?, ?, ?) " +
          "ON CONFLICT(uid, request_fingerprint) DO NOTHING",
      )
        .bind(
          context.uid,
          jobId,
          request.schemaVersion,
          request.conversationId,
          request.audioFileId,
          request.timestamps ? JSON.stringify(request.timestamps) : null,
          request.sourceFingerprint,
          `chunks/${context.uid}/${request.conversationId}/`,
          artifactKey,
          generation,
          now,
          requestFingerprint,
          now,
          now,
        )
        .run();
      const job = await readJob(c.env, context.uid, jobId);
      if (!job) return c.json({ error: "audio_merge_job_unavailable" }, 503);
      if (inserted.meta?.changes !== 1) return c.json(publicJob(job));
      await enqueue(c.env, job);
      return c.json(publicJob(job), 202);
    } catch (error) {
      if (error instanceof LegacyAudioMergeHttpError)
        return c.json(
          { error: error.code, message: error.message },
          error.status as 400,
        );
      return c.json({ error: "audio_merge_unavailable" }, 503);
    }
  };

  // Keep the canonical Cloudflare adapter available for explicit callers and
  // expose the original legacy path once the Edge owner is switched in
  // staging. Both paths share exactly the same D1/Queue authority.
  app.post("/v2/cf/audio-merge-jobs/legacy/run", run);
  app.post("/v2/audio-merge-jobs/run", run);

  app.get("/v2/cf/audio-merge-jobs/legacy/:jobId", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const jobId = c.req.param("jobId");
    if (!/^audio-merge-legacy-[a-f0-9]{48}$/.test(jobId))
      return c.json({ error: "not_found" }, 404);
    const job = await readJob(c.env, context.uid, jobId);
    return job ? c.json(publicJob(job)) : c.json({ error: "not_found" }, 404);
  });
}

type QueuePayload = {
  schemaVersion: 1 | 2;
  conversationId: string;
  audioFileId: string;
  timestamps: number[] | null;
  sourceFingerprint: string | null;
  outputFormat: "mp3";
};

function parseQueuePayload(value: Record<string, unknown>): QueuePayload | null {
  const schemaVersion = value.schemaVersion;
  const conversationId = value.conversationId;
  const audioFileId = value.audioFileId;
  const timestamps = value.timestamps;
  const sourceFingerprint = value.sourceFingerprint;
  if (
    (schemaVersion !== 1 && schemaVersion !== 2) ||
    typeof conversationId !== "string" ||
    !validId(conversationId) ||
    typeof audioFileId !== "string" ||
    !validId(audioFileId) ||
    (schemaVersion === 2 && audioFileId !== "conversation") ||
    (schemaVersion === 1 && !validTimestamps(timestamps)) ||
    (schemaVersion === 2 && timestamps !== null) ||
    (sourceFingerprint !== null &&
      (typeof sourceFingerprint !== "string" ||
        !SAFE_FINGERPRINT.test(sourceFingerprint))) ||
    value.outputFormat !== "mp3"
  )
    return null;
  return {
    schemaVersion,
    conversationId,
    audioFileId,
    timestamps: schemaVersion === 1 ? (timestamps as number[]) : null,
    sourceFingerprint: sourceFingerprint as string | null,
    outputFormat: "mp3",
  };
}

async function markFailed(
  env: JobsEnv,
  row: LegacyAudioMergeJobRow,
  reason: string,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_audio_merge_legacy_jobs SET status = 'failed', lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ? " +
      "WHERE uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
  )
    .bind(reason.slice(0, 2048), now, row.uid, row.job_id, row.lease_token)
    .run();
}

async function markRetry(
  env: JobsEnv,
  row: LegacyAudioMergeJobRow,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_audio_merge_legacy_jobs SET status = 'queued', lease_token = NULL, lease_until = NULL, next_attempt_at = ?, last_error = ?, updated_at = ? " +
      "WHERE uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
  )
    .bind(
      now + RETRY_DELAY_SECONDS,
      "audio merge processor unavailable",
      now,
      row.uid,
      row.job_id,
      row.lease_token,
    )
    .run();
}

export async function processLegacyAudioMergeJobMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const payload = parseQueuePayload(message.body.payload);
  if (!payload) {
    message.ack();
    return;
  }
  const current = await readJob(env, message.body.uid, message.body.jobId);
  if (!current) {
    message.ack();
    return;
  }
  if (current.status === "completed" || current.status === "cancelled") {
    message.ack();
    return;
  }
  if (
    current.schema_version !== payload.schemaVersion ||
    current.conversation_id !== payload.conversationId ||
    current.audio_file_id !== payload.audioFileId ||
    current.source_fingerprint !== payload.sourceFingerprint ||
    current.output_format !== payload.outputFormat
  ) {
    await markFailed(env, current, "audio merge job payload mismatch", Math.floor(Date.now() / 1000));
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1000);
  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_audio_merge_legacy_jobs SET status = 'running', attempts = attempts + 1, lease_token = ?, lease_until = ?, updated_at = ? " +
      "WHERE uid = ? AND job_id = ? AND ((status = 'queued' AND next_attempt_at <= ?) OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))",
  )
    .bind(
      leaseToken,
      now + LEASE_SECONDS,
      now,
      message.body.uid,
      message.body.jobId,
      now,
      now,
    )
    .run();
  if (claimed.meta?.changes !== 1) {
    if (current.status === "failed") {
      message.ack();
      return;
    }
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }
  const row = await readJob(env, message.body.uid, message.body.jobId);
  if (!row || row.status !== "running" || row.lease_token !== leaseToken) {
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
    return;
  }
  try {
    if (row.account_generation !== (await accountGeneration(env, row.uid)))
      throw new LegacyAudioSourceError("account generation changed");
    const result = payload.schemaVersion === 1
      ? await rebuildLegacyAudioFileMp3(
          env,
          { uid: row.uid, job_id: row.job_id },
          row.conversation_id,
          row.audio_file_id,
          payload.timestamps as number[],
          now,
        )
      : await rebuildLegacyConversationMp3(
          env,
          { uid: row.uid, job_id: row.job_id },
          row.conversation_id,
          payload.sourceFingerprint,
          now,
        );
    const updated = await env.APP_DB.prepare(
      "UPDATE cf_audio_merge_legacy_jobs SET status = 'completed', lease_token = NULL, lease_until = NULL, result_json = ?, last_error = NULL, updated_at = ? " +
        "WHERE uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
    )
      .bind(
        JSON.stringify(result),
        now,
        row.uid,
        row.job_id,
        leaseToken,
      )
      .run();
    if (updated.meta?.changes !== 1) {
      message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
      return;
    }
    message.ack();
  } catch (error) {
    if (error instanceof LegacyAudioSourceError) {
      await markFailed(env, row, error.message, now);
      message.ack();
      return;
    }
    if (row.attempts >= MAX_ATTEMPTS) {
      await markFailed(env, row, "audio merge failed after retry budget", now);
      message.ack();
      return;
    }
    await markRetry(env, row, now);
    message.retry({ delaySeconds: RETRY_DELAY_SECONDS });
  }
}
