import type { Message } from "@cloudflare/workers-types";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";
import {
  LegacyAudioSourceError,
  legacyAudioFiles,
  legacyAudioFilesFingerprint,
  rebuildLegacyConversationAudio,
} from "./legacy-audio-import";
import { recordingStorageEnabled } from "./sync-local-files";

const MAX_PAYLOAD_BYTES = 16_000;
const MAX_ATTEMPTS = 3;
const LEASE_SECONDS = 15 * 60;
const RETRY_DELAY_SECONDS = 10;
const OUTPUT_FORMAT = "wav" as const;
const AUDIO_FILE_ID = "conversation" as const;
const SAFE_FINGERPRINT = /^[a-f0-9]{64}$/;

type AudioMergeContext = Context<{ Bindings: JobsEnv }>;
type AuthContext = { uid: string };
type AudioMergeStatus =
  "queued" | "running" | "completed" | "failed" | "cancelled";

type ConversationRow = {
  created_at: number;
  updated_at: number | null;
  is_locked: number;
  audio_files_json: string;
};

type AudioMergeJobRow = {
  uid: string;
  job_id: string;
  conversation_id: string;
  audio_file_id: string;
  source_fingerprint: string;
  source_prefix: string;
  artifact_key: string;
  output_format: string;
  account_generation: number;
  status: AudioMergeStatus;
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

class AudioMergeHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function validConversationId(value: string): boolean {
  return (
    value.length > 0 &&
    value.length <= 128 &&
    value !== "." &&
    value !== ".." &&
    !value.includes("/") &&
    !value.includes("\\")
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

function objectPayload(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function publicJob(row: AudioMergeJobRow): Record<string, unknown> {
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

async function parseRequest(c: AudioMergeContext): Promise<{
  conversationId: string;
  sourceFingerprint?: string;
}> {
  const declaredLength = Number(c.req.header("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_PAYLOAD_BYTES)
    throw new AudioMergeHttpError(
      413,
      "payload_too_large",
      "audio merge request is too large",
    );
  let value: unknown;
  try {
    const body = await c.req.raw.arrayBuffer();
    if (body.byteLength > MAX_PAYLOAD_BYTES)
      throw new AudioMergeHttpError(
        413,
        "payload_too_large",
        "audio merge request is too large",
      );
    value = JSON.parse(new TextDecoder().decode(body));
  } catch (error) {
    if (error instanceof AudioMergeHttpError) throw error;
    throw new AudioMergeHttpError(
      400,
      "invalid_request",
      "audio merge request must be JSON",
    );
  }
  const payload = objectPayload(value);
  if (!payload)
    throw new AudioMergeHttpError(
      400,
      "invalid_request",
      "audio merge request must be an object",
    );
  const allowed = new Set([
    "conversation_id",
    "audio_file_id",
    "output_format",
    "source_fingerprint",
  ]);
  if (Object.keys(payload).some((key) => !allowed.has(key)))
    throw new AudioMergeHttpError(
      400,
      "invalid_request",
      "audio merge request contains unsupported fields",
    );
  const conversationId =
    typeof payload.conversation_id === "string"
      ? payload.conversation_id.trim()
      : "";
  if (!validConversationId(conversationId))
    throw new AudioMergeHttpError(
      400,
      "invalid_conversation_id",
      "conversation id is invalid",
    );
  if (
    payload.audio_file_id !== undefined &&
    payload.audio_file_id !== AUDIO_FILE_ID
  )
    throw new AudioMergeHttpError(
      422,
      "unsupported_audio_scope",
      "the staging boundary only builds the conversation artifact",
    );
  if (payload.output_format !== OUTPUT_FORMAT)
    throw new AudioMergeHttpError(
      422,
      "unsupported_output_format",
      "the staging boundary only produces WAV",
    );
  const sourceFingerprint = payload.source_fingerprint;
  if (
    sourceFingerprint !== undefined &&
    (typeof sourceFingerprint !== "string" ||
      !SAFE_FINGERPRINT.test(sourceFingerprint))
  )
    throw new AudioMergeHttpError(
      400,
      "invalid_source_fingerprint",
      "source fingerprint is invalid",
    );
  return {
    conversationId,
    sourceFingerprint: sourceFingerprint as string | undefined,
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
    throw new AudioMergeHttpError(
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
): Promise<AudioMergeJobRow | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_audio_merge_jobs WHERE uid = ? AND job_id = ?",
  )
    .bind(uid, jobId)
    .first<AudioMergeJobRow>();
}

function jobMessage(row: AudioMergeJobRow): JobMessage {
  return {
    jobId: row.job_id,
    uid: row.uid,
    kind: "audio_merge",
    payload: {
      conversationId: row.conversation_id,
      sourceFingerprint: row.source_fingerprint,
      outputFormat: row.output_format,
    },
  };
}

async function enqueue(env: JobsEnv, row: AudioMergeJobRow): Promise<void> {
  try {
    await env.JOBS.send(jobMessage(row));
  } catch {
    await env.APP_DB.prepare(
      "UPDATE cf_audio_merge_jobs SET status = 'failed', lease_until = NULL, last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND status = 'queued'",
    )
      .bind(
        "queue unavailable",
        Math.floor(Date.now() / 1000),
        row.uid,
        row.job_id,
      )
      .run();
    throw new AudioMergeHttpError(
      503,
      "queue_unavailable",
      "audio merge queue is unavailable",
    );
  }
}

export function registerAudioMergeRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (c: AudioMergeContext) => Promise<AuthContext | null>,
): void {
  app.post("/v2/cf/audio-merge-jobs/run", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const { conversationId, sourceFingerprint: requestedFingerprint } =
        await parseRequest(c);
      const row = await c.env.APP_DB.prepare(
        "SELECT created_at, updated_at, is_locked, audio_files_json FROM cf_conversations WHERE uid = ? AND id = ?",
      )
        .bind(context.uid, conversationId)
        .first<ConversationRow>();
      if (!row) return c.json({ error: "conversation_not_found" }, 404);
      if (row.is_locked) return c.json({ error: "conversation_locked" }, 402);
      if (!(await recordingStorageEnabled(c.env, context.uid)))
        return c.json({ error: "audio_storage_disabled" }, 403);
      const audioFiles = legacyAudioFiles(row.audio_files_json);
      if (!audioFiles.length) return c.json({ error: "no_audio_files" }, 400);
      const sourceFingerprint = await legacyAudioFilesFingerprint(audioFiles);
      if (
        requestedFingerprint !== undefined &&
        requestedFingerprint !== sourceFingerprint
      )
        return c.json({ error: "audio_metadata_changed" }, 409);
      const generation = await accountGeneration(c.env, context.uid);
      const requestFingerprint = await sha256Hex(
        `audio_merge\0${context.uid}\0${conversationId}\0${sourceFingerprint}\0${OUTPUT_FORMAT}`,
      );
      const jobId = `audio-merge-${requestFingerprint.slice(0, 48)}`;
      const now = Math.floor(Date.now() / 1000);
      const sourcePrefix = `chunks/${context.uid}/${conversationId}/`;
      const artifactKey = `sync-playback/${context.uid}/${conversationId}/conversation.wav`;
      const inserted = await c.env.APP_DB.prepare(
        "INSERT INTO cf_audio_merge_jobs (uid, job_id, conversation_id, audio_file_id, source_fingerprint, source_prefix, artifact_key, output_format, account_generation, status, attempts, next_attempt_at, request_fingerprint, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?) ON CONFLICT(uid, request_fingerprint) DO NOTHING",
      )
        .bind(
          context.uid,
          jobId,
          conversationId,
          AUDIO_FILE_ID,
          sourceFingerprint,
          sourcePrefix,
          artifactKey,
          OUTPUT_FORMAT,
          generation,
          now,
          requestFingerprint,
          now,
          now,
        )
        .run();
      let job = await readJob(c.env, context.uid, jobId);
      if (inserted.meta?.changes !== 1) {
        if (!job) return c.json({ error: "audio merge job conflict" }, 409);
        let shouldEnqueue = false;
        if (job.status === "failed" && job.last_error === "queue unavailable") {
          const repaired = await c.env.APP_DB.prepare(
            "UPDATE cf_audio_merge_jobs SET status = 'queued', attempts = 0, next_attempt_at = ?, last_error = NULL, updated_at = ? WHERE uid = ? AND job_id = ? AND status = 'failed' AND last_error = ?",
          )
            .bind(now, now, context.uid, jobId, "queue unavailable")
            .run();
          if (repaired.meta?.changes === 1) {
            shouldEnqueue = true;
            job = await readJob(c.env, context.uid, jobId);
          }
        }
        if (!job) return c.json({ error: "audio merge job unavailable" }, 503);
        if (shouldEnqueue) {
          await enqueue(c.env, job);
          return c.json({ ...publicJob(job), status: "queued" }, 202);
        }
        return c.json(publicJob(job));
      }
      if (!job) return c.json({ error: "audio merge job unavailable" }, 503);
      await enqueue(c.env, job);
      return c.json(publicJob(job), 202);
    } catch (error) {
      if (error instanceof AudioMergeHttpError)
        return c.json(
          { error: error.code, message: error.message },
          error.status as 400,
        );
      return c.json({ error: "audio_merge_unavailable" }, 503);
    }
  });

  app.get("/v2/cf/audio-merge-jobs/:jobId", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const jobId = c.req.param("jobId");
    if (!/^audio-merge-[a-f0-9]{48}$/.test(jobId))
      return c.json({ error: "not_found" }, 404);
    const job = await readJob(c.env, context.uid, jobId);
    return job ? c.json(publicJob(job)) : c.json({ error: "not_found" }, 404);
  });
}

function parseQueuePayload(payload: Record<string, unknown>): {
  conversationId: string;
  sourceFingerprint: string;
  outputFormat: "wav";
} | null {
  const conversationId =
    typeof payload.conversationId === "string" ? payload.conversationId : "";
  const sourceFingerprint =
    typeof payload.sourceFingerprint === "string"
      ? payload.sourceFingerprint
      : "";
  if (
    !validConversationId(conversationId) ||
    !SAFE_FINGERPRINT.test(sourceFingerprint) ||
    payload.outputFormat !== OUTPUT_FORMAT
  )
    return null;
  return { conversationId, sourceFingerprint, outputFormat: OUTPUT_FORMAT };
}

async function markFailed(
  env: JobsEnv,
  row: AudioMergeJobRow,
  reason: string,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_audio_merge_jobs SET status = 'failed', lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
  )
    .bind(reason.slice(0, 2048), now, row.uid, row.job_id, row.lease_token)
    .run();
}

async function markRetry(
  env: JobsEnv,
  row: AudioMergeJobRow,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_audio_merge_jobs SET status = 'queued', lease_token = NULL, lease_until = NULL, next_attempt_at = ?, last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
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

export async function processAudioMergeJobMessage(
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
    current.conversation_id !== payload.conversationId ||
    current.source_fingerprint !== payload.sourceFingerprint
  ) {
    await env.APP_DB.prepare(
      "UPDATE cf_audio_merge_jobs SET status = 'failed', lease_token = NULL, lease_until = NULL, last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND status NOT IN ('completed', 'cancelled')",
    )
      .bind(
        "audio merge job payload mismatch",
        Math.floor(Date.now() / 1000),
        current.uid,
        current.job_id,
      )
      .run();
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1000);
  const leaseToken = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_audio_merge_jobs SET status = 'running', attempts = attempts + 1, lease_token = ?, lease_until = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND ((status = 'queued' AND next_attempt_at <= ?) OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))",
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
    .run<AudioMergeJobRow>();
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
    const result = await rebuildLegacyConversationAudio(
      env,
      { uid: row.uid, job_id: row.job_id },
      row.conversation_id,
      row.source_fingerprint,
      now,
    );
    if (
      result.dense_storage_key !== row.artifact_key ||
      result.audio_files_fingerprint !== row.source_fingerprint
    )
      throw new Error("audio merge artifact contract mismatch");
    const updated = await env.APP_DB.prepare(
      "UPDATE cf_audio_merge_jobs SET status = 'completed', lease_token = NULL, lease_until = NULL, result_json = ?, last_error = NULL, updated_at = ? WHERE uid = ? AND job_id = ? AND status = 'running' AND lease_token = ?",
    )
      .bind(JSON.stringify(result), now, row.uid, row.job_id, leaseToken)
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
