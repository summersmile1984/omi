import {
  FormDataParseError,
  MaxFilesExceededError,
  MaxFileSizeExceededError,
  MaxPartsExceededError,
  MaxTotalSizeExceededError,
  parseFormData,
  type FileUpload,
} from "@remix-run/form-data-parser";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import type { Context, Hono } from "hono";
import {
  recordFairUseUsage,
  speechMsFromTranscription,
} from "../shared/fair-use-meter";
import {
  fairUseRestrictionResponse,
  readFairUseRestriction,
} from "../shared/fair-use-enforcement";
import type { JobMessage, JobsEnv } from "./env";
import {
  decodeWalToWavChunks,
  parseSyncFilename,
  pcm16WavHeader,
  PLAYBACK_CHANNELS,
  PLAYBACK_SAMPLE_RATE,
  type SyncFileIdentity,
} from "./sync-audio";

const MAX_SYNC_FILES = 20;
const MAX_SYNC_FILE_BYTES = 40 * 1024 * 1024;
const MAX_SYNC_REQUEST_BYTES = 100_000_000;
const FRESH_MAX_AGE_SECONDS = 6 * 60 * 60;
const BACKFILL_MAX_AGE_SECONDS = 30 * 24 * 60 * 60;
const MAX_FUTURE_SKEW_SECONDS = 5 * 60;
const CAPTURE_WINDOW_SLOP_SECONDS = 30 * 60;
const MANIFEST_TTL_SECONDS = 15 * 60;
const MANIFEST_CLAIM_TTL_SECONDS = 6 * 60 * 60;
const CONTENT_LEDGER_RETENTION_SECONDS = 45 * 24 * 60 * 60;
const CONTENT_CLAIM_STALE_SECONDS = 2 * 24 * 60 * 60;
const JOB_RETENTION_SECONDS = 24 * 60 * 60;
const JOB_LEASE_SECONDS = 15 * 60;
const MAX_SYNC_RUN_REQUEST_BYTES = 4_096;
const SYNC_RUN_JOB_ID = /^[A-Za-z0-9_-]{1,128}$/;
const MAX_PROVIDER_ATTEMPTS = 3;
const QUEUE_RETRY_SECONDS = 15;
const BACKFILL_USER_DAILY_MS = 4 * 60 * 60 * 1000;
const BACKFILL_GLOBAL_DAILY_MS = 555 * 60 * 60 * 1000;
const DEFAULT_ASR_MODEL = "@cf/openai/whisper-large-v3-turbo";
const DEFAULT_SUMMARY_MODEL = "@cf/meta/llama-3.1-8b-instruct-fast";
const MAX_TRANSCRIPTION_JSON_BYTES = 1_000_000;
const MAX_SUMMARY_TRANSCRIPT_CHARS = 60_000;
const SYNC_RECONCILE_BATCH_SIZE = 25;
const SYNC_CLEANUP_BATCH_SIZE = 100;
const MAX_CONVERSATION_AUDIO_JSON_BYTES = 1_000_000;
const PLAYBACK_INTENT_STALE_SECONDS = 60 * 60;

type JobsContext = Context<{ Bindings: JobsEnv }>;
type Lane = "fresh" | "backfill";
type CaptureTrust = "device_bound" | "legacy" | "untrusted";
type SyncAuthContext = { uid: string; authority?: string };

type ManifestClaim = { name: string; sha256: string };

type StagedSyncFile = SyncFileIdentity & {
  ordinal: number;
  objectKey: string;
  sha256: string;
  size: number;
};

type SyncJobRow = {
  job_id: string;
  uid: string;
  content_id: string;
  status: string;
  lane: Lane;
  capture_time_trust: CaptureTrust;
  conversation_id: string | null;
  source: string;
  client_device_id: string | null;
  client_platform: string | null;
  recording_age_seconds: number | null;
  total_files: number;
  total_segments: number;
  processed_segments: number;
  successful_segments: number;
  failed_segments: number;
  attempts: number;
  result_json: string | null;
  last_error: string | null;
  reason_code: string | null;
  created_at: number;
  updated_at: number;
};

type SyncFileRow = {
  job_id: string;
  uid: string;
  ordinal: number;
  filename: string;
  object_key: string;
  sha256: string;
  size: number;
  capture_at: number;
  codec: "opus" | "pcm16" | "pcm8";
  sample_rate: 8000 | 12000 | 16000 | 24000 | 48000;
  channels: 1 | 2;
  frame_size: number;
  status: "staged" | "transcribed" | "failed";
  transcription_json: string | null;
  speech_ms: number;
  duration_ms: number;
  detected_language: string | null;
  last_error: string | null;
};

type LaneDecision = {
  lane: Lane;
  trust: CaptureTrust;
  reason: string;
  maximumAgeSeconds: number;
  automaticRecoveryAllowed: boolean;
};

type ConversationRow = {
  id: string;
  created_at: number;
  updated_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  source: string;
  language: string | null;
  status: string;
  visibility: string;
  starred: number;
  discarded: number;
  is_locked: number;
  deferred: number;
  private_cloud_sync_enabled: number;
  folder_id: string | null;
  client_device_id: string | null;
  client_platform: string | null;
  structured_json: string;
  transcript_segments_json: string;
  photos_json: string;
  audio_files_json: string;
  conversation_audio_json: string | null;
  apps_results_json: string;
  suggested_apps_json: string;
  geolocation_json: string | null;
  external_data_json: string | null;
  calendar_event_json: string | null;
};

type NormalizedSegment = {
  id: string;
  text: string;
  start: number;
  end: number;
  speaker: string;
  speaker_id: number;
  is_user: boolean;
  person_id: null;
};

type FileTranscription = {
  text: string;
  segments: NormalizedSegment[];
  detected_language: string | null;
  provider: "workers-ai";
  model: string;
  speech_ms: number;
  duration_ms: number;
  chunk_count: number;
};

export type PlaybackAudioFile = {
  id: string;
  uid: string;
  conversation_id: string;
  chunk_timestamps: number[];
  provider: "cloudflare-r2";
  started_at: string;
  duration: number;
  storage_key: string;
  content_type: "audio/wav";
  sample_rate: typeof PLAYBACK_SAMPLE_RATE;
  channels: typeof PLAYBACK_CHANNELS;
  pcm_bytes: number;
};

export type ConversationPlayback = {
  audio_files_fingerprint: string;
  duration: number;
  captured_duration: number;
  spans: Array<{
    file_id: string;
    wall_offset: number;
    artifact_offset: number;
    len: number;
  }>;
  content_type: "audio/wav";
  storage_key: string;
  built_at: number;
};

type StructuredConversation = {
  title: string;
  overview: string;
  category: string;
  action_items: unknown[];
  events: unknown[];
};

class SyncHttpError extends Error {
  readonly status: number;
  readonly code: string;
  readonly headers: Record<string, string>;

  constructor(
    status: number,
    code: string,
    message: string,
    headers: Record<string, string> = {},
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.headers = headers;
  }
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function jsonArray(value: string | null | undefined): unknown[] {
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function jsonObject(value: string | null | undefined): Record<string, unknown> {
  try {
    return objectValue(JSON.parse(value || "{}")) || {};
  } catch {
    return {};
  }
}

function base64Encode(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function shortError(error: unknown): string {
  return error instanceof Error
    ? error.message.slice(0, 500)
    : "sync processing unavailable";
}

function parseModelObject(value: unknown): Record<string, unknown> | null {
  const direct = objectValue(value);
  if (!direct) return null;
  const response = direct?.response;
  const structured = objectValue(response);
  if (structured) return structured;
  if (typeof response !== "string") return direct;
  const fenced = response.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
  try {
    return objectValue(JSON.parse((fenced || response).trim()));
  } catch {
    return null;
  }
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}

function base64UrlDecode(value: string): Uint8Array | null {
  try {
    const padded = value
      .replaceAll("-", "+")
      .replaceAll("_", "/")
      .padEnd(Math.ceil(value.length / 4) * 4, "=");
    const binary = atob(padded);
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function safeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1)
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function syncSecret(env: JobsEnv): string | null {
  return env.SYNC_CONTENT_ID_SECRET || env.INTERNAL_ASSERTION_SECRET || null;
}

async function hmacHex(secret: string, value: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(value),
  );
  return bytesToHex(new Uint8Array(signature));
}

async function hashFile(file: File): Promise<string> {
  const hash = sha256.create();
  const reader = file.stream().getReader();
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) break;
      hash.update(item.value);
    }
  } finally {
    reader.releaseLock();
  }
  return bytesToHex(hash.digest());
}

function normalizedDevice(request: Request): {
  id: string | null;
  platform: string | null;
} {
  const platform = (request.headers.get("x-app-platform") || "")
    .trim()
    .toLowerCase();
  const deviceHash = (request.headers.get("x-device-id-hash") || "")
    .trim()
    .toLowerCase();
  const validPlatform = [
    "android",
    "ios",
    "linux",
    "macos",
    "web",
    "windows",
  ].includes(platform);
  return {
    id:
      validPlatform && /^[0-9a-f]{8}$/.test(deviceHash)
        ? `${platform}_${deviceHash}`
        : null,
    platform: platform || null,
  };
}

function validateClaims(value: unknown): ManifestClaim[] | null {
  if (!Array.isArray(value) || !value.length || value.length > MAX_SYNC_FILES)
    return null;
  const claims: ManifestClaim[] = [];
  const names = new Set<string>();
  for (const item of value) {
    const claim = objectValue(item);
    const name = typeof claim?.name === "string" ? claim.name : "";
    const digest =
      typeof claim?.sha256 === "string" ? claim.sha256.toLowerCase() : "";
    if (
      !parseSyncFilename(name) ||
      !/^[0-9a-f]{64}$/.test(digest) ||
      names.has(name)
    )
      return null;
    names.add(name);
    claims.push({ name, sha256: digest });
  }
  return claims.sort((left, right) =>
    `${left.name}:${left.sha256}`.localeCompare(
      `${right.name}:${right.sha256}`,
    ),
  );
}

function claimFingerprint(claims: ManifestClaim[]): string {
  return bytesToHex(sha256(new TextEncoder().encode(JSON.stringify(claims))));
}

async function issueManifest(
  env: JobsEnv,
  uid: string,
  device: string,
  conversation: string,
  files: ManifestClaim[],
  now: number,
): Promise<string | null> {
  const secret = syncSecret(env);
  if (!secret) return null;
  const payload = {
    v: 1,
    uid,
    device,
    conversation,
    files,
    iat: now,
    exp: now + MANIFEST_TTL_SECONDS,
  };
  const encoded = base64UrlEncode(
    new TextEncoder().encode(JSON.stringify(payload)),
  );
  return `${encoded}.${await hmacHex(secret, encoded)}`;
}

async function verifyManifest(
  env: JobsEnv,
  token: string | null,
  uid: string,
  device: string | null,
  conversation: string | null,
  now: number,
): Promise<ManifestClaim[] | null> {
  const secret = syncSecret(env);
  if (!secret || !token || !device || !conversation) return null;
  const [encoded, suppliedSignature, extra] = token.split(".");
  if (!encoded || !suppliedSignature || extra) return null;
  const expected = await hmacHex(secret, encoded);
  if (!safeEqual(suppliedSignature, expected)) return null;
  const bytes = base64UrlDecode(encoded);
  if (!bytes) return null;
  try {
    const payload = objectValue(JSON.parse(new TextDecoder().decode(bytes)));
    if (
      payload?.v !== 1 ||
      payload.uid !== uid ||
      payload.device !== device ||
      payload.conversation !== conversation ||
      typeof payload.iat !== "number" ||
      typeof payload.exp !== "number" ||
      payload.iat > now + 60 ||
      payload.exp < now
    ) {
      return null;
    }
    return validateClaims(payload.files);
  } catch {
    return null;
  }
}

async function conversationMatchesCapture(
  database: D1Database,
  uid: string,
  conversationId: string | null,
  deviceId: string | null,
  filenames: string[],
): Promise<boolean> {
  if (!conversationId || !deviceId || !filenames.length) return false;
  const row = await database
    .prepare(
      "SELECT started_at, finished_at, client_device_id FROM cf_conversations WHERE uid = ? AND id = ?",
    )
    .bind(uid, conversationId)
    .first<{
      started_at: number | null;
      finished_at: number | null;
      client_device_id: string | null;
    }>();
  if (
    !row ||
    row.client_device_id !== deviceId ||
    typeof row.started_at !== "number"
  ) {
    return false;
  }
  const upper =
    (row.finished_at ?? row.started_at) + CAPTURE_WINDOW_SLOP_SECONDS;
  const lower = row.started_at - CAPTURE_WINDOW_SLOP_SECONDS;
  return filenames.every((filename) => {
    const parsed = parseSyncFilename(filename);
    return parsed && parsed.captureAt >= lower && parsed.captureAt <= upper;
  });
}

export function classifySyncLane(
  files: SyncFileIdentity[],
  hasServerCaptureProof: boolean,
  now: number,
): LaneDecision {
  const oldest = Math.min(...files.map((file) => file.captureAt));
  const newest = Math.max(...files.map((file) => file.captureAt));
  const maximumAgeSeconds = Math.max(0, Math.floor(now - oldest));
  if (newest > now + MAX_FUTURE_SKEW_SECONDS) {
    return {
      lane: "backfill",
      trust: "untrusted",
      reason: "future_capture_time",
      maximumAgeSeconds,
      automaticRecoveryAllowed: true,
    };
  }
  if (maximumAgeSeconds > BACKFILL_MAX_AGE_SECONDS) {
    return {
      lane: "backfill",
      trust: hasServerCaptureProof ? "device_bound" : "legacy",
      reason: "lookback_exceeded",
      maximumAgeSeconds,
      automaticRecoveryAllowed: false,
    };
  }
  if (!hasServerCaptureProof) {
    return {
      lane: "backfill",
      trust: "legacy",
      reason: "unbound_capture_time",
      maximumAgeSeconds,
      automaticRecoveryAllowed: true,
    };
  }
  if (maximumAgeSeconds > FRESH_MAX_AGE_SECONDS) {
    return {
      lane: "backfill",
      trust: "device_bound",
      reason: "historical_capture",
      maximumAgeSeconds,
      automaticRecoveryAllowed: true,
    };
  }
  return {
    lane: "fresh",
    trust: "device_bound",
    reason: "recent_capture",
    maximumAgeSeconds,
    automaticRecoveryAllowed: true,
  };
}

function detectSource(filenames: string[]): string {
  for (const raw of filenames) {
    const filename = raw.toLowerCase();
    if (filename.includes("limitless")) return "limitless";
    if (filename.includes("omibatchphone") || filename.includes("phonemic"))
      return "phone";
  }
  return "omi";
}

async function cleanupObjects(
  env: JobsEnv,
  files: StagedSyncFile[],
): Promise<void> {
  await Promise.all(
    files.map(async (file) => {
      try {
        await env.ASSETS.delete(file.objectKey);
      } catch {
        // The one-day R2 lifecycle is the durable cleanup fallback.
      }
    }),
  );
}

async function stageSyncFiles(
  request: Request,
  env: JobsEnv,
  uid: string,
  jobId: string,
): Promise<StagedSyncFile[]> {
  const staged: StagedSyncFile[] = [];
  try {
    await parseFormData(
      request,
      {
        maxFiles: MAX_SYNC_FILES,
        maxFileSize: MAX_SYNC_FILE_BYTES,
        maxParts: MAX_SYNC_FILES + 4,
        maxTotalSize: MAX_SYNC_REQUEST_BYTES,
      },
      async (file: FileUpload) => {
        if (file.fieldName !== "files") return null;
        const identity = parseSyncFilename(file.name);
        if (!identity)
          throw new SyncHttpError(
            400,
            "invalid_sync_file",
            "Audio file has an invalid sync filename",
          );
        if (!file.size)
          throw new SyncHttpError(
            400,
            "empty_sync_file",
            "Audio file is empty",
          );
        const ordinal = staged.length;
        const objectKey = `cf-sync/${uid}/${jobId}/${ordinal}`;
        const digest = await hashFile(file);
        await env.ASSETS.put(objectKey, file, {
          httpMetadata: { contentType: "application/octet-stream" },
          customMetadata: {
            uid,
            jobId,
            ordinal: String(ordinal),
            filename: identity.filename,
            sha256: digest,
          },
        });
        staged.push({
          ...identity,
          ordinal,
          objectKey,
          sha256: digest,
          size: file.size,
        });
        return objectKey;
      },
    );
  } catch (error) {
    await cleanupObjects(env, staged);
    if (error instanceof SyncHttpError) throw error;
    if (
      error instanceof MaxFilesExceededError ||
      error instanceof MaxFileSizeExceededError ||
      error instanceof MaxTotalSizeExceededError
    ) {
      throw new SyncHttpError(
        413,
        "sync_upload_too_large",
        "Audio upload is too large",
      );
    }
    if (
      error instanceof FormDataParseError ||
      error instanceof MaxPartsExceededError
    ) {
      throw new SyncHttpError(
        400,
        "invalid_multipart",
        "Audio upload is not valid multipart data",
      );
    }
    throw new SyncHttpError(
      503,
      "sync_staging_unavailable",
      "Audio staging is temporarily unavailable",
    );
  }
  if (!staged.length)
    throw new SyncHttpError(
      400,
      "missing_sync_files",
      "No audio files were provided",
    );
  return staged;
}

async function computeContentId(
  env: JobsEnv,
  uid: string,
  files: StagedSyncFile[],
): Promise<string | null> {
  const secret = syncSecret(env);
  if (!secret) return null;
  const canonical = files
    .map((file) => `${file.filename}:${file.sha256}`)
    .sort()
    .join("\n");
  return hmacHex(secret, `${uid}\n${canonical}`);
}

function secondsUntilNextUtcDay(now: number): number {
  return Math.max(1, Math.floor((Math.floor(now / 86_400) + 1) * 86_400 - now));
}

async function enforceBackfillAdmission(
  database: D1Database,
  uid: string,
  now: number,
): Promise<void> {
  const dayStart = Math.floor(now / 86_400) * 86_400;
  const user = await database
    .prepare(
      "SELECT COALESCE(SUM(speech_ms), 0) AS speech_ms FROM cf_fair_use_usage_sources " +
        "WHERE uid = ? AND source_kind = 'sync_backfill' AND occurred_at >= ?",
    )
    .bind(uid, dayStart)
    .first<{ speech_ms: number }>();
  const global = await database
    .prepare(
      "SELECT COALESCE(SUM(speech_ms), 0) AS speech_ms FROM cf_fair_use_usage_sources " +
        "WHERE source_kind = 'sync_backfill' AND occurred_at >= ?",
    )
    .bind(dayStart)
    .first<{ speech_ms: number }>();
  if (
    Number(user?.speech_ms || 0) >= BACKFILL_USER_DAILY_MS ||
    Number(global?.speech_ms || 0) >= BACKFILL_GLOBAL_DAILY_MS
  ) {
    const retryAfter = secondsUntilNextUtcDay(now);
    throw new SyncHttpError(
      429,
      "backfill_paced",
      "Historical recovery reached its daily processing allowance",
      {
        "Retry-After": String(retryAfter),
        "X-Omi-Rate-Limit-Reason": "backfill_paced",
      },
    );
  }
}

function startResponse(
  jobId: string,
  totalFiles: number,
  lane: Lane,
  status = "queued",
  totalSegments = 0,
) {
  return {
    job_id: jobId,
    status,
    total_files: totalFiles,
    total_segments: totalSegments,
    poll_after_ms: status === "completed" ? 0 : 1_000,
    lane,
  };
}

async function queueSyncJob(env: JobsEnv, message: JobMessage, lane: Lane) {
  const queue = lane === "fresh" ? env.SYNC_FRESH : env.SYNC_BACKFILL;
  await queue.send(message);
}

async function readBoundedSyncRunBody(request: Request): Promise<unknown> {
  const declaredLength = Number(request.headers.get("content-length"));
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_SYNC_RUN_REQUEST_BYTES
  ) {
    throw new SyncHttpError(
      413,
      "request_too_large",
      "Sync run request is too large",
    );
  }
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength > MAX_SYNC_RUN_REQUEST_BYTES) {
    throw new SyncHttpError(
      413,
      "request_too_large",
      "Sync run request is too large",
    );
  }
  if (!bytes.byteLength) {
    throw new SyncHttpError(
      400,
      "invalid_request",
      "Sync run request must be JSON",
    );
  }
  try {
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new SyncHttpError(
      400,
      "invalid_request",
      "Sync run request must be valid JSON",
    );
  }
}

async function insertCompletedReplayJob(
  env: JobsEnv,
  uid: string,
  jobId: string,
  contentId: string,
  lane: LaneDecision,
  conversationId: string | null,
  source: string,
  device: { id: string | null; platform: string | null },
  totalFiles: number,
  resultJson: string,
  now: number,
): Promise<void> {
  const result = objectValue(JSON.parse(resultJson)) || {};
  const totalSegments = Number(result.total_segments || 0);
  await env.APP_DB.prepare(
    "INSERT INTO cf_sync_jobs " +
      "(job_id, uid, content_id, status, lane, capture_time_trust, conversation_id, source, " +
      "client_device_id, client_platform, recording_age_seconds, total_files, total_segments, " +
      "processed_segments, successful_segments, failed_segments, attempts, result_json, created_at, updated_at, expires_at) " +
      "VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)",
  )
    .bind(
      jobId,
      uid,
      contentId,
      lane.lane,
      lane.trust,
      conversationId,
      source,
      device.id,
      device.platform,
      lane.maximumAgeSeconds,
      totalFiles,
      totalSegments,
      totalSegments,
      totalSegments,
      resultJson,
      now,
      now,
      now + JOB_RETENTION_SECONDS,
    )
    .run();
}

async function admitSyncJob(
  c: JobsContext,
  uid: string,
  jobId: string,
  files: StagedSyncFile[],
  lane: LaneDecision,
  conversationId: string | null,
  source: string,
  device: { id: string | null; platform: string | null },
  contentId: string,
  now: number,
): Promise<Response> {
  const ledger = await c.env.APP_DB.prepare(
    "SELECT status, job_id, result_json, updated_at FROM cf_sync_content_ledger WHERE uid = ? AND content_id = ?",
  )
    .bind(uid, contentId)
    .first<{
      status: string;
      job_id: string;
      result_json: string | null;
      updated_at: number;
    }>();
  if (ledger?.status === "completed" && ledger.result_json) {
    await insertCompletedReplayJob(
      c.env,
      uid,
      jobId,
      contentId,
      lane,
      conversationId,
      source,
      device,
      files.length,
      ledger.result_json,
      now,
    );
    await cleanupObjects(c.env, files);
    const result = objectValue(JSON.parse(ledger.result_json)) || {};
    return c.json(
      startResponse(
        jobId,
        files.length,
        lane.lane,
        "completed",
        Number(result.total_segments || 0),
      ),
      202,
    );
  }
  if (
    ledger?.status === "processing" &&
    ledger.updated_at > now - CONTENT_CLAIM_STALE_SECONDS
  ) {
    await cleanupObjects(c.env, files);
    return c.json(
      {
        code: "sync_content_in_progress",
        detail: "The same audio is already processing",
      },
      409,
      { "Retry-After": "10" },
    );
  }

  const statements: D1PreparedStatement[] = [
    c.env.APP_DB.prepare(
      "INSERT INTO cf_sync_jobs " +
        "(job_id, uid, content_id, status, lane, capture_time_trust, conversation_id, source, " +
        "client_device_id, client_platform, recording_age_seconds, total_files, created_at, updated_at, expires_at) " +
        "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ).bind(
      jobId,
      uid,
      contentId,
      lane.lane,
      lane.trust,
      conversationId,
      source,
      device.id,
      device.platform,
      lane.maximumAgeSeconds,
      files.length,
      now,
      now,
      now + JOB_RETENTION_SECONDS,
    ),
    ...files.map((file) =>
      c.env.APP_DB.prepare(
        "INSERT INTO cf_sync_job_files " +
          "(job_id, uid, ordinal, filename, object_key, sha256, size, capture_at, codec, sample_rate, channels, frame_size) " +
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      ).bind(
        jobId,
        uid,
        file.ordinal,
        file.filename,
        file.objectKey,
        file.sha256,
        file.size,
        file.captureAt,
        file.codec,
        file.sampleRate,
        file.channels,
        file.frameSize,
      ),
    ),
  ];
  if (ledger) {
    statements.push(
      c.env.APP_DB.prepare(
        "UPDATE cf_sync_content_ledger SET status = 'processing', job_id = ?, lane = ?, result_json = NULL, " +
          "updated_at = ?, expires_at = ? WHERE uid = ? AND content_id = ? AND " +
          "(status = 'retryable' OR updated_at <= ?)",
      ).bind(
        jobId,
        lane.lane,
        now,
        now + CONTENT_LEDGER_RETENTION_SECONDS,
        uid,
        contentId,
        now - CONTENT_CLAIM_STALE_SECONDS,
      ),
    );
  } else {
    statements.push(
      c.env.APP_DB.prepare(
        "INSERT INTO cf_sync_content_ledger " +
          "(uid, content_id, status, job_id, lane, created_at, updated_at, expires_at) " +
          "VALUES (?, ?, 'processing', ?, ?, ?, ?, ?)",
      ).bind(
        uid,
        contentId,
        jobId,
        lane.lane,
        now,
        now,
        now + CONTENT_LEDGER_RETENTION_SECONDS,
      ),
    );
  }

  try {
    const results = await c.env.APP_DB.batch(statements);
    if (ledger && results.at(-1)?.meta?.changes !== 1) {
      await c.env.APP_DB.prepare(
        "DELETE FROM cf_sync_jobs WHERE job_id = ? AND uid = ?",
      )
        .bind(jobId, uid)
        .run();
      await cleanupObjects(c.env, files);
      return c.json(
        {
          code: "sync_content_in_progress",
          detail: "The same audio is already processing",
        },
        409,
        { "Retry-After": "10" },
      );
    }
  } catch {
    await cleanupObjects(c.env, files);
    const contentInFlight = await c.env.APP_DB.prepare(
      "SELECT job_id FROM cf_sync_content_ledger WHERE uid = ? AND content_id = ? " +
        "AND status = 'processing' AND updated_at > ?",
    )
      .bind(uid, contentId, now - CONTENT_CLAIM_STALE_SECONDS)
      .first<{ job_id: string }>();
    if (contentInFlight) {
      return c.json(
        {
          code: "sync_content_in_progress",
          detail: "The same audio is already processing",
        },
        409,
        { "Retry-After": "10" },
      );
    }
    const backfillInFlight =
      lane.lane === "backfill"
        ? await c.env.APP_DB.prepare(
            "SELECT job_id FROM cf_sync_jobs WHERE uid = ? AND lane = 'backfill' " +
              "AND status IN ('queued', 'running') LIMIT 1",
          )
            .bind(uid)
            .first<{ job_id: string }>()
        : null;
    if (backfillInFlight) {
      return c.json(
        {
          code: "backfill_paced",
          detail: "Another historical recovery job is still in flight",
        },
        429,
        {
          "Retry-After": "30",
          "X-Omi-Rate-Limit-Reason": "backfill_paced",
        },
      );
    }
    return c.json(
      {
        code: "sync_admission_unavailable",
        detail: "Sync admission is temporarily unavailable",
      },
      503,
    );
  }

  const message: JobMessage = {
    jobId,
    uid,
    kind: "sync_local_files",
    payload: { lane: lane.lane },
  };
  try {
    await queueSyncJob(c.env, message, lane.lane);
  } catch {
    // A Queue acknowledgement can be ambiguous. Keep the pollable job and R2
    // bytes; the scheduled reconciler republishes it instead of starting an
    // unfenced inline twin or deleting possibly runnable work.
  }
  return c.json(startResponse(jobId, files.length, lane.lane), 202);
}

function normalizedAsrSegments(
  value: unknown,
  file: SyncFileRow,
  chunkIndex: number,
  chunkStartSeconds: number,
): NormalizedSegment[] {
  const payload = objectValue(value) || {};
  const candidates = Array.isArray(payload.segments)
    ? payload.segments
    : Array.isArray(payload.words)
      ? payload.words
      : [];
  const segments: NormalizedSegment[] = [];
  for (let index = 0; index < candidates.length; index += 1) {
    const candidate = objectValue(candidates[index]);
    const text =
      typeof candidate?.text === "string"
        ? candidate.text.trim()
        : typeof candidate?.word === "string"
          ? candidate.word.trim()
          : "";
    const start = Number(candidate?.start);
    const end = Number(candidate?.end);
    if (
      !text ||
      !Number.isFinite(start) ||
      !Number.isFinite(end) ||
      start < 0 ||
      end <= start
    )
      continue;
    segments.push({
      id: `${file.sha256.slice(0, 24)}-${chunkIndex}-${index}`,
      text,
      start: chunkStartSeconds + start,
      end: chunkStartSeconds + end,
      speaker: "SPEAKER_00",
      speaker_id: 0,
      is_user: false,
      person_id: null,
    });
  }
  return segments;
}

async function transcribeSyncFile(
  env: JobsEnv,
  file: SyncFileRow,
): Promise<FileTranscription> {
  if (file.status === "transcribed" && file.transcription_json) {
    const checkpoint = objectValue(JSON.parse(file.transcription_json));
    if (checkpoint) return checkpoint as FileTranscription;
  }
  const object = await env.ASSETS.get(file.object_key);
  if (!object) throw new Error("staged sync audio not found");
  const raw = await object.arrayBuffer();
  if (!raw.byteLength || raw.byteLength !== file.size)
    throw new Error("staged sync audio size mismatch");
  const digest = bytesToHex(sha256(new Uint8Array(raw)));
  if (!safeEqual(digest, file.sha256))
    throw new Error("staged sync audio digest mismatch");

  const model = env.WORKERS_AI_ASR_MODEL || DEFAULT_ASR_MODEL;
  const segments: NormalizedSegment[] = [];
  const texts: string[] = [];
  let detectedLanguage: string | null = null;
  let durationSeconds = 0;
  let chunkCount = 0;
  for await (const chunk of decodeWalToWavChunks(raw, {
    filename: file.filename,
    captureAt: file.capture_at,
    codec: file.codec,
    sampleRate: file.sample_rate,
    channels: file.channels,
    frameSize: file.frame_size,
  })) {
    let response: unknown;
    try {
      response = await env.AI.run(model, {
        audio: base64Encode(chunk.wav),
        vad_filter: true,
      });
    } catch {
      throw new Error("workers ai transcription unavailable");
    }
    const payload = objectValue(response);
    if (!payload || typeof payload.text !== "string")
      throw new Error("workers ai returned invalid transcription");
    const text = payload.text.trim();
    const chunkSegments = normalizedAsrSegments(
      payload,
      file,
      chunkCount,
      chunk.startSeconds,
    );
    if (text && !chunkSegments.length)
      throw new Error("workers ai omitted speech timestamps");
    if (text) texts.push(text);
    segments.push(...chunkSegments);
    if (typeof payload.detected_language === "string")
      detectedLanguage = payload.detected_language.slice(0, 32);
    durationSeconds = Math.max(
      durationSeconds,
      chunk.startSeconds + chunk.durationSeconds,
    );
    chunkCount += 1;
  }
  const transcription: FileTranscription = {
    text: texts.join(" ").trim(),
    segments,
    detected_language: detectedLanguage,
    provider: "workers-ai",
    model,
    speech_ms: speechMsFromTranscription({ segments }),
    duration_ms: Math.round(durationSeconds * 1_000),
    chunk_count: chunkCount,
  };
  const encoded = JSON.stringify(transcription);
  if (
    new TextEncoder().encode(encoded).byteLength > MAX_TRANSCRIPTION_JSON_BYTES
  )
    throw new Error("sync transcription result is too large");
  await env.APP_DB.prepare(
    "UPDATE cf_sync_job_files SET status = 'transcribed', transcription_json = ?, speech_ms = ?, " +
      "duration_ms = ?, detected_language = ?, last_error = NULL WHERE job_id = ? AND ordinal = ?",
  )
    .bind(
      encoded,
      transcription.speech_ms,
      transcription.duration_ms,
      transcription.detected_language,
      file.job_id,
      file.ordinal,
    )
    .run();
  return transcription;
}

function playbackAudioId(
  job: SyncJobRow,
  file: SyncFileRow,
  chunkIndex: number,
): string {
  return `cf-${job.content_id.slice(0, 20)}-${file.ordinal}-${chunkIndex}`;
}

export async function recordingStorageEnabled(
  env: JobsEnv,
  uid: string,
): Promise<boolean> {
  const row = await env.APP_DB.prepare(
    `SELECT EXISTS(
       SELECT 1 FROM cf_user_privacy_settings
       WHERE uid = ? AND private_cloud_sync_enabled = 1
         AND store_recording_permission = 1
     ) AND NOT EXISTS(
       SELECT 1 FROM cf_recording_deletion_intents WHERE uid = ?
     ) AS enabled`,
  )
    .bind(uid, uid)
    .first<{ enabled?: unknown }>();
  return Number(row?.enabled) === 1;
}

async function recordingDeletionActive(
  env: JobsEnv,
  uid: string,
): Promise<boolean> {
  const row = await env.APP_DB.prepare(
    "SELECT 1 AS active FROM cf_recording_deletion_intents WHERE uid = ? LIMIT 1",
  )
    .bind(uid)
    .first<{ active?: unknown }>();
  return Number(row?.active) === 1;
}

export type PlaybackJob = {
  uid: string;
  job_id: string;
};

export async function recordPlaybackIntent(
  env: JobsEnv,
  job: PlaybackJob,
  conversationId: string,
  audioFileId: string,
  storageKey: string,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "INSERT INTO cf_sync_playback_objects " +
      "(uid, storage_key, conversation_id, audio_file_id, job_id, state, created_at, updated_at) " +
      "VALUES (?, ?, ?, ?, ?, 'staging', ?, ?) ON CONFLICT(uid, storage_key) DO UPDATE SET " +
      "conversation_id = excluded.conversation_id, audio_file_id = excluded.audio_file_id, " +
      "job_id = excluded.job_id, state = CASE WHEN state = 'committed' THEN state ELSE 'staging' END, " +
      "updated_at = excluded.updated_at",
  )
    .bind(
      job.uid,
      storageKey,
      conversationId,
      audioFileId,
      job.job_id,
      now,
      now,
    )
    .run();
}

export async function markPlaybackStored(
  env: JobsEnv,
  uid: string,
  storageKey: string,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_sync_playback_objects SET state = 'stored', updated_at = ? " +
      "WHERE uid = ? AND storage_key = ? AND state <> 'committed'",
  )
    .bind(now, uid, storageKey)
    .run();
}

function playbackFile(value: unknown): PlaybackAudioFile | null {
  const file = objectValue(value);
  if (
    !file ||
    typeof file.id !== "string" ||
    typeof file.storage_key !== "string" ||
    !file.storage_key.startsWith("sync-playback/") ||
    file.content_type !== "audio/wav" ||
    file.sample_rate !== PLAYBACK_SAMPLE_RATE ||
    file.channels !== PLAYBACK_CHANNELS ||
    typeof file.pcm_bytes !== "number" ||
    !Number.isInteger(file.pcm_bytes) ||
    file.pcm_bytes <= 0 ||
    file.pcm_bytes % 2 !== 0
  ) {
    return null;
  }
  return file as PlaybackAudioFile;
}

function playbackStartedAt(file: PlaybackAudioFile): number | null {
  const raw = file.started_at;
  const value =
    typeof raw === "number" ? raw : Date.parse(String(raw || "")) / 1_000;
  return Number.isFinite(value) ? value : null;
}

function audioFilesFingerprint(audioFiles: PlaybackAudioFile[]): string {
  const parts = audioFiles
    .map((file) => {
      const timestamps = file.chunk_timestamps
        .map(Number)
        .filter(Number.isFinite)
        .sort((left, right) => left - right);
      return [
        file.id,
        timestamps.length,
        timestamps.length ? Math.round(timestamps.at(-1)! * 1_000) / 1_000 : 0,
        file.pcm_bytes,
      ];
    })
    .sort((left, right) => String(left[0]).localeCompare(String(right[0])));
  return bytesToHex(
    sha256(new TextEncoder().encode(JSON.stringify(parts))),
  ).slice(0, 12);
}

function densePlaybackStream(
  env: JobsEnv,
  audioFiles: PlaybackAudioFile[],
  totalPcmBytes: number,
): ReadableStream<Uint8Array> {
  const header = new Uint8Array(
    pcm16WavHeader(totalPcmBytes, PLAYBACK_SAMPLE_RATE, PLAYBACK_CHANNELS),
  );
  let index = -1;
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        if (index < 0) {
          index = 0;
          controller.enqueue(header);
          return;
        }
        const file = audioFiles[index];
        if (!file) {
          controller.close();
          return;
        }
        index += 1;
        const object = await env.ASSETS.get(file.storage_key, {
          range: { offset: 44, length: file.pcm_bytes },
        });
        if (!object)
          throw new Error("playback window disappeared during dense build");
        const pcm = new Uint8Array(await object.arrayBuffer());
        if (pcm.byteLength !== file.pcm_bytes)
          throw new Error("playback window size changed during dense build");
        controller.enqueue(pcm);
      } catch (error) {
        controller.error(error);
      }
    },
  });
}

export function fixedLengthPlaybackStream(
  source: ReadableStream<Uint8Array>,
  expectedLength: number,
): {
  readable: ReadableStream<Uint8Array>;
  completed: Promise<void>;
} {
  let stream: TransformStream<Uint8Array, Uint8Array>;
  const fixedLength = (
    globalThis as unknown as {
      FixedLengthStream?: typeof FixedLengthStream;
    }
  ).FixedLengthStream;
  if (typeof fixedLength === "function") {
    stream = new fixedLength(expectedLength) as TransformStream<
      Uint8Array,
      Uint8Array
    >;
  } else {
    let observed = 0;
    stream = new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        observed += chunk.byteLength;
        if (observed > expectedLength)
          throw new Error("dense playback exceeded its declared length");
        controller.enqueue(chunk);
      },
      flush() {
        if (observed !== expectedLength)
          throw new Error("dense playback did not reach its declared length");
      },
    });
  }
  return {
    readable: stream.readable,
    completed: source.pipeTo(stream.writable),
  };
}

export async function buildConversationPlayback(
  env: JobsEnv,
  job: PlaybackJob,
  conversationId: string,
  conversationStartedAt: number,
  values: unknown[],
  now: number,
): Promise<ConversationPlayback | null> {
  if (!(await recordingStorageEnabled(env, job.uid))) return null;
  const parsed = values.map(playbackFile);
  if (!parsed.length || parsed.some((file) => file === null)) return null;
  const audioFiles = (parsed as PlaybackAudioFile[]).sort((left, right) => {
    return (
      (playbackStartedAt(left) || 0) - (playbackStartedAt(right) || 0) ||
      left.id.localeCompare(right.id)
    );
  });
  if (audioFiles.some((file) => playbackStartedAt(file) === null)) return null;
  const totalPcmBytes = audioFiles.reduce(
    (total, file) => total + file.pcm_bytes,
    0,
  );
  if (totalPcmBytes <= 0 || totalPcmBytes > 0xffff_ffff - 36) return null;

  const storageKey = `sync-playback/${job.uid}/${conversationId}/conversation.wav`;
  await recordPlaybackIntent(
    env,
    job,
    conversationId,
    "conversation",
    storageKey,
    now,
  );
  const body = fixedLengthPlaybackStream(
    densePlaybackStream(env, audioFiles, totalPcmBytes),
    totalPcmBytes + 44,
  );
  await Promise.all([
    env.ASSETS.put(storageKey, body.readable, {
      httpMetadata: { contentType: "audio/wav" },
      customMetadata: {
        uid: job.uid,
        conversationId,
        audioFileId: "conversation",
        sampleRate: String(PLAYBACK_SAMPLE_RATE),
        channels: String(PLAYBACK_CHANNELS),
      },
    }),
    body.completed,
  ]);
  await markPlaybackStored(env, job.uid, storageKey, now);

  let artifactOffset = 0;
  const spans = audioFiles.map((file) => {
    const length = file.pcm_bytes / (PLAYBACK_SAMPLE_RATE * 2);
    const span = {
      file_id: file.id,
      wall_offset:
        Math.round(
          Math.max(0, playbackStartedAt(file)! - conversationStartedAt) * 1_000,
        ) / 1_000,
      artifact_offset: Math.round(artifactOffset * 1_000) / 1_000,
      len: Math.round(length * 1_000) / 1_000,
    };
    artifactOffset += length;
    return span;
  });
  return {
    audio_files_fingerprint: audioFilesFingerprint(audioFiles),
    duration:
      Math.round((spans.at(-1)!.wall_offset + spans.at(-1)!.len) * 1_000) /
      1_000,
    captured_duration: Math.round(artifactOffset * 1_000) / 1_000,
    spans,
    content_type: "audio/wav",
    storage_key: storageKey,
    built_at: now,
  };
}

async function persistConversationPlayback(
  env: JobsEnv,
  job: SyncJobRow,
  files: SyncFileRow[],
  transcriptions: Map<number, FileTranscription>,
  conversationId: string,
  now: number,
): Promise<PlaybackAudioFile[]> {
  if (!(await recordingStorageEnabled(env, job.uid))) return [];

  const playback: PlaybackAudioFile[] = [];
  for (const file of files) {
    if (!transcriptions.has(file.ordinal)) continue;
    const object = await env.ASSETS.get(file.object_key);
    if (!object)
      throw new Error("staged sync audio missing before playback persistence");
    const raw = await object.arrayBuffer();
    if (!raw.byteLength || raw.byteLength !== file.size)
      throw new Error(
        "staged sync audio size mismatch before playback persistence",
      );
    const digest = bytesToHex(sha256(new Uint8Array(raw)));
    if (!safeEqual(digest, file.sha256))
      throw new Error(
        "staged sync audio digest mismatch before playback persistence",
      );

    let chunkIndex = 0;
    for await (const chunk of decodeWalToWavChunks(raw, {
      filename: file.filename,
      captureAt: file.capture_at,
      codec: file.codec,
      sampleRate: file.sample_rate,
      channels: file.channels,
      frameSize: file.frame_size,
    })) {
      const id = playbackAudioId(job, file, chunkIndex);
      const storageKey = `sync-playback/${job.uid}/${conversationId}/${id}.wav`;
      await recordPlaybackIntent(env, job, conversationId, id, storageKey, now);
      await env.ASSETS.put(storageKey, chunk.wav, {
        httpMetadata: { contentType: "audio/wav" },
        customMetadata: {
          uid: job.uid,
          conversationId,
          audioFileId: id,
          sampleRate: String(PLAYBACK_SAMPLE_RATE),
          channels: String(PLAYBACK_CHANNELS),
        },
      });
      await markPlaybackStored(env, job.uid, storageKey, now);
      const startedAt = file.capture_at + chunk.startSeconds;
      playback.push({
        id,
        uid: job.uid,
        conversation_id: conversationId,
        chunk_timestamps: [startedAt],
        provider: "cloudflare-r2",
        started_at: new Date(startedAt * 1_000).toISOString(),
        duration: chunk.durationSeconds,
        storage_key: storageKey,
        content_type: "audio/wav",
        sample_rate: PLAYBACK_SAMPLE_RATE,
        channels: PLAYBACK_CHANNELS,
        pcm_bytes: chunk.wav.byteLength - 44,
      });
      chunkIndex += 1;
    }
  }
  if (!playback.length) return [];

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const row = await env.APP_DB.prepare(
      "SELECT created_at, updated_at, started_at, audio_files_json FROM cf_conversations WHERE uid = ? AND id = ?",
    )
      .bind(job.uid, conversationId)
      .first<{
        created_at: number;
        updated_at: number | null;
        started_at: number | null;
        audio_files_json: string;
      }>();
    if (!row)
      throw new Error("conversation missing before playback persistence");
    const current = jsonArray(row.audio_files_json).filter(
      (value) => objectValue(value) !== null,
    );
    const replacementIds = new Set(playback.map((file) => file.id));
    const merged = [
      ...current.filter(
        (value) => !replacementIds.has(String(objectValue(value)?.id || "")),
      ),
      ...playback,
    ];
    const encoded = JSON.stringify(merged);
    if (
      new TextEncoder().encode(encoded).byteLength >
      MAX_CONVERSATION_AUDIO_JSON_BYTES
    )
      throw new Error("conversation playback metadata is too large");
    const conversationAudio = await buildConversationPlayback(
      env,
      job,
      conversationId,
      row.started_at ?? row.created_at,
      merged,
      now,
    );
    const encodedConversationAudio = conversationAudio
      ? JSON.stringify(conversationAudio)
      : null;
    if (
      encodedConversationAudio &&
      new TextEncoder().encode(encodedConversationAudio).byteLength >
        MAX_CONVERSATION_AUDIO_JSON_BYTES
    )
      throw new Error("conversation playback artifact metadata is too large");
    const revision = row.updated_at ?? row.created_at;
    const nextRevision = Math.max(now, revision + 1);
    const updated = await env.APP_DB.prepare(
      "UPDATE cf_conversations SET updated_at = ?, private_cloud_sync_enabled = 1, audio_files_json = ?, " +
        "conversation_audio_json = ? WHERE uid = ? AND id = ? AND COALESCE(updated_at, created_at) = ? " +
        "AND EXISTS (SELECT 1 FROM cf_user_privacy_settings " +
        "WHERE uid = ? AND private_cloud_sync_enabled = 1 AND store_recording_permission = 1) " +
        "AND NOT EXISTS (SELECT 1 FROM cf_recording_deletion_intents WHERE uid = ?) RETURNING id",
    )
      .bind(
        nextRevision,
        encoded,
        encodedConversationAudio,
        job.uid,
        conversationId,
        revision,
        job.uid,
        job.uid,
      )
      .run<{ id: string }>();
    if (updated.results?.[0]?.id === conversationId) {
      const committedKeys = [
        ...playback.map((file) => file.storage_key),
        ...(conversationAudio ? [conversationAudio.storage_key] : []),
      ];
      await Promise.all(
        committedKeys.map((storageKey) =>
          env.APP_DB.prepare(
            "UPDATE cf_sync_playback_objects SET state = 'committed', updated_at = ? " +
              "WHERE uid = ? AND storage_key = ?",
          )
            .bind(nextRevision, job.uid, storageKey)
            .run(),
        ),
      );
      return playback;
    }
  }
  throw new Error("conversation changed during playback persistence");
}

function summarySchema() {
  return {
    type: "json_schema",
    json_schema: {
      name: "omi_sync_conversation",
      strict: true,
      schema: {
        type: "object",
        properties: {
          title: { type: "string" },
          overview: { type: "string" },
          category: { type: "string" },
          action_items: {
            type: "array",
            items: {
              type: "object",
              properties: {
                description: { type: "string" },
                completed: { type: "boolean" },
              },
              required: ["description", "completed"],
              additionalProperties: false,
            },
          },
          events: {
            type: "array",
            items: {
              type: "object",
              properties: {
                title: { type: "string" },
                start: { type: "string" },
                duration: { type: "number" },
              },
              required: ["title", "start", "duration"],
              additionalProperties: false,
            },
          },
        },
        required: ["title", "overview", "category", "action_items", "events"],
        additionalProperties: false,
      },
    },
  };
}

async function summarizeConversation(
  env: JobsEnv,
  segments: NormalizedSegment[],
): Promise<StructuredConversation> {
  const transcript = segments
    .map((segment) => segment.text)
    .join("\n")
    .slice(0, MAX_SUMMARY_TRANSCRIPT_CHARS);
  const model = env.WORKERS_AI_SYNC_SUMMARY_MODEL || DEFAULT_SUMMARY_MODEL;
  let response: unknown;
  try {
    response = await env.AI.run(model, {
      messages: [
        {
          role: "system",
          content:
            "Summarize a private personal conversation. Return only the requested JSON. " +
            "Do not invent facts. action_items and events must be arrays of concise objects.",
        },
        { role: "user", content: transcript },
      ],
      response_format: summarySchema(),
      max_tokens: 1_024,
      temperature: 0,
    });
  } catch {
    throw new Error("workers ai summarization unavailable");
  }
  const parsed = parseModelObject(response);
  const title =
    typeof parsed?.title === "string" ? parsed.title.trim().slice(0, 300) : "";
  const overview =
    typeof parsed?.overview === "string"
      ? parsed.overview.trim().slice(0, 4_000)
      : "";
  const category =
    typeof parsed?.category === "string"
      ? parsed.category.trim().slice(0, 100)
      : "";
  if (!title || !overview)
    throw new Error("workers ai returned invalid summary");
  return {
    title,
    overview,
    category: category || "other",
    action_items: Array.isArray(parsed?.action_items)
      ? parsed.action_items.slice(0, 100)
      : [],
    events: Array.isArray(parsed?.events) ? parsed.events.slice(0, 100) : [],
  };
}

const CONVERSATION_COLUMNS =
  "id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, " +
  "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, " +
  "client_platform, structured_json, transcript_segments_json, photos_json, audio_files_json, " +
  "conversation_audio_json, apps_results_json, suggested_apps_json, geolocation_json, external_data_json, " +
  "calendar_event_json";

async function findTargetConversation(
  env: JobsEnv,
  job: SyncJobRow,
  captureStart: number,
  captureEnd: number,
): Promise<ConversationRow | null> {
  if (job.conversation_id) {
    const exact = await env.APP_DB.prepare(
      `SELECT ${CONVERSATION_COLUMNS} FROM cf_conversations ` +
        "WHERE uid = ? AND id = ? AND discarded = 0 AND is_locked = 0",
    )
      .bind(job.uid, job.conversation_id)
      .first<ConversationRow>();
    if (exact) return exact;
  }
  return env.APP_DB.prepare(
    `SELECT ${CONVERSATION_COLUMNS} FROM cf_conversations WHERE uid = ? AND discarded = 0 AND is_locked = 0 ` +
      "AND COALESCE(finished_at, started_at, created_at) >= ? " +
      "AND COALESCE(started_at, created_at) <= ? " +
      "ORDER BY ABS(COALESCE(started_at, created_at) - ?) LIMIT 1",
  )
    .bind(job.uid, captureStart - 120, captureEnd + 120, captureStart)
    .first<ConversationRow>();
}

function existingSegments(row: ConversationRow): NormalizedSegment[] {
  const startedAt = row.started_at ?? row.created_at;
  const output: NormalizedSegment[] = [];
  const values = jsonArray(row.transcript_segments_json);
  for (let index = 0; index < values.length; index += 1) {
    const segment = objectValue(values[index]);
    const text = typeof segment?.text === "string" ? segment.text.trim() : "";
    const start = Number(segment?.start);
    const end = Number(segment?.end);
    if (
      !text ||
      !Number.isFinite(start) ||
      !Number.isFinite(end) ||
      end <= start
    )
      continue;
    output.push({
      id:
        typeof segment?.id === "string" && segment.id
          ? segment.id
          : `existing-${index}-${Math.round(start * 1_000)}`,
      text,
      start: startedAt + start,
      end: startedAt + end,
      speaker:
        typeof segment?.speaker === "string" ? segment.speaker : "SPEAKER_00",
      speaker_id: Number.isInteger(segment?.speaker_id)
        ? Number(segment?.speaker_id)
        : 0,
      is_user: segment?.is_user === true,
      person_id: null,
    });
  }
  return output;
}

function wordCount(segments: NormalizedSegment[]): number {
  return segments.reduce(
    (total, segment) => total + (segment.text.match(/\S+/g)?.length || 0),
    0,
  );
}

async function finalizeConversation(
  env: JobsEnv,
  job: SyncJobRow,
  files: SyncFileRow[],
  transcriptions: Map<number, FileTranscription>,
  now: number,
): Promise<{ id: string; created: boolean } | null> {
  const absolute: NormalizedSegment[] = [];
  let captureStart = Number.POSITIVE_INFINITY;
  let captureEnd = 0;
  let language: string | null = null;
  for (const file of files) {
    const transcription = transcriptions.get(file.ordinal);
    if (!transcription) continue;
    captureStart = Math.min(captureStart, file.capture_at);
    captureEnd = Math.max(
      file.capture_at + transcription.duration_ms / 1_000,
      captureEnd,
    );
    language ||= transcription.detected_language;
    absolute.push(
      ...transcription.segments.map((segment) => ({
        ...segment,
        start: file.capture_at + segment.start,
        end: file.capture_at + segment.end,
      })),
    );
  }
  if (!absolute.length) return null;
  const existing = await findTargetConversation(
    env,
    job,
    captureStart,
    captureEnd,
  );
  const all = existing
    ? [...existingSegments(existing), ...absolute]
    : absolute;
  const unique = new Map<string, NormalizedSegment>();
  for (const segment of all) unique.set(segment.id, segment);
  const sorted = [...unique.values()].sort(
    (left, right) =>
      left.start - right.start ||
      left.end - right.end ||
      left.id.localeCompare(right.id),
  );
  const startedAt = Math.floor(
    Math.min(...sorted.map((segment) => segment.start)),
  );
  const finishedAt = Math.ceil(
    Math.max(...sorted.map((segment) => segment.end)),
  );
  const relative = sorted.slice(0, 2_000).map((segment) => ({
    ...segment,
    start: Math.max(0, segment.start - startedAt),
    end: Math.max(0, segment.end - startedAt),
  }));
  const structured = await summarizeConversation(env, relative);
  const structuredJson = JSON.stringify(structured);
  const segmentsJson = JSON.stringify(relative);
  const id = existing?.id || `sync-${job.content_id.slice(0, 32)}`;
  const occurredAt = finishedAt || startedAt;
  const transcriptionSeconds = Math.min(
    Math.max(finishedAt - startedAt, 0),
    604_800,
  );
  const insights = structured.action_items.length + structured.events.length;

  if (existing) {
    const revision = existing.updated_at ?? existing.created_at;
    const updated = await env.APP_DB.prepare(
      "UPDATE cf_conversations SET updated_at = ?, started_at = ?, finished_at = ?, language = ?, " +
        "status = 'completed', structured_json = ?, transcript_segments_json = ? " +
        "WHERE uid = ? AND id = ? AND COALESCE(updated_at, created_at) = ? AND discarded = 0 AND is_locked = 0 " +
        "RETURNING id",
    )
      .bind(
        now,
        startedAt,
        finishedAt,
        language || existing.language,
        structuredJson,
        segmentsJson,
        job.uid,
        id,
        revision,
      )
      .run<{ id: string }>();
    if (updated.results?.[0]?.id !== id)
      throw new Error("conversation changed during sync finalization");
  } else {
    const inserted = await env.APP_DB.prepare(
      "INSERT INTO cf_conversations " +
        "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, " +
        "client_device_id, client_platform, structured_json, transcript_segments_json) " +
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', 'private', ?, ?, ?, ?) ON CONFLICT DO NOTHING " +
        "RETURNING id",
    )
      .bind(
        job.uid,
        id,
        startedAt,
        now,
        startedAt,
        finishedAt,
        job.source,
        language,
        job.client_device_id,
        job.client_platform,
        structuredJson,
        segmentsJson,
      )
      .run<{ id: string }>();
    if (inserted.results?.[0]?.id !== id)
      throw new Error("conversation identity changed during sync finalization");
  }
  await env.APP_DB.prepare(
    "INSERT INTO cf_usage_sources " +
      "(uid, source_kind, source_id, occurred_at, transcription_seconds, words_transcribed, insights_gained, memories_created, updated_at) " +
      "VALUES (?, 'conversation', ?, ?, ?, ?, ?, 0, ?) ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET " +
      "occurred_at = excluded.occurred_at, transcription_seconds = excluded.transcription_seconds, " +
      "words_transcribed = excluded.words_transcribed, insights_gained = excluded.insights_gained, updated_at = excluded.updated_at",
  )
    .bind(
      job.uid,
      id,
      occurredAt,
      transcriptionSeconds,
      wordCount(relative),
      insights,
      now,
    )
    .run();
  return { id, created: !existing };
}

async function syncJobFiles(
  env: JobsEnv,
  jobId: string,
  uid: string,
): Promise<SyncFileRow[]> {
  const result = await env.APP_DB.prepare(
    "SELECT job_id, uid, ordinal, filename, object_key, sha256, size, capture_at, codec, sample_rate, " +
      "channels, frame_size, status, transcription_json, speech_ms, duration_ms, detected_language, last_error " +
      "FROM cf_sync_job_files WHERE job_id = ? AND uid = ? ORDER BY ordinal",
  )
    .bind(jobId, uid)
    .all<SyncFileRow>();
  return result.results || [];
}

async function cleanupJobObjects(
  env: JobsEnv,
  jobId: string,
  uid: string,
): Promise<void> {
  const files = await syncJobFiles(env, jobId, uid);
  await cleanupObjects(
    env,
    files.map((file) => ({
      filename: file.filename,
      captureAt: file.capture_at,
      codec: file.codec,
      sampleRate: file.sample_rate,
      channels: file.channels,
      frameSize: file.frame_size,
      ordinal: file.ordinal,
      objectKey: file.object_key,
      sha256: file.sha256,
      size: file.size,
    })),
  );
}

async function resetSyncJobForRetry(
  message: Message<JobMessage>,
  env: JobsEnv,
  error: string,
): Promise<void> {
  const now = Math.floor(Date.now() / 1_000);
  await env.APP_DB.prepare(
    "UPDATE cf_sync_jobs SET status = 'queued', lease_until = NULL, last_error = ?, updated_at = ? " +
      "WHERE job_id = ? AND uid = ? AND status = 'running'",
  )
    .bind(error, now, message.body.jobId, message.body.uid)
    .run();
  message.retry({ delaySeconds: QUEUE_RETRY_SECONDS });
}

async function commitSyncTerminal(
  env: JobsEnv,
  job: SyncJobRow,
  status: "completed" | "partial_failure" | "failed",
  result: Record<string, unknown>,
  error: string | null,
  now: number,
): Promise<void> {
  const resultJson = JSON.stringify(result);
  await env.APP_DB.batch([
    env.APP_DB.prepare(
      "UPDATE cf_sync_jobs SET status = ?, total_segments = ?, processed_segments = ?, successful_segments = ?, " +
        "failed_segments = ?, result_json = ?, last_error = ?, reason_code = ?, lease_until = NULL, updated_at = ? " +
        "WHERE job_id = ? AND uid = ? AND status = 'running'",
    ).bind(
      status,
      Number(result.total_segments || 0),
      Number(result.total_segments || 0),
      Number(result.successful_segments || 0),
      Number(result.failed_segments || 0),
      resultJson,
      error,
      status === "completed" ? null : "processing_failed",
      now,
      job.job_id,
      job.uid,
    ),
    env.APP_DB.prepare(
      "UPDATE cf_sync_content_ledger SET status = ?, result_json = ?, updated_at = ?, expires_at = ? " +
        "WHERE uid = ? AND content_id = ? AND job_id = ?",
    ).bind(
      status === "completed" ? "completed" : "retryable",
      status === "completed" ? resultJson : null,
      now,
      now + CONTENT_LEDGER_RETENTION_SECONDS,
      job.uid,
      job.content_id,
      job.job_id,
    ),
  ]);
}

export async function processSyncJobMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const existing = await env.APP_DB.prepare(
    "SELECT status, updated_at FROM cf_sync_jobs WHERE job_id = ? AND uid = ?",
  )
    .bind(message.body.jobId, message.body.uid)
    .first<{ status: string; updated_at: number }>();
  if (!existing) {
    message.ack();
    return;
  }
  if (["completed", "partial_failure", "failed"].includes(existing.status)) {
    await cleanupJobObjects(env, message.body.jobId, message.body.uid);
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1_000);
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_sync_jobs SET status = 'running', attempts = attempts + 1, lease_until = ?, updated_at = ? " +
      "WHERE job_id = ? AND uid = ? AND (status = 'queued' OR (status = 'running' AND lease_until <= ?))",
  )
    .bind(
      now + JOB_LEASE_SECONDS,
      now,
      message.body.jobId,
      message.body.uid,
      now,
    )
    .run();
  if (claimed.meta?.changes !== 1) {
    message.retry({ delaySeconds: QUEUE_RETRY_SECONDS });
    return;
  }
  const job = await env.APP_DB.prepare(
    "SELECT job_id, uid, content_id, status, lane, capture_time_trust, conversation_id, source, " +
      "client_device_id, client_platform, recording_age_seconds, total_files, total_segments, processed_segments, " +
      "successful_segments, failed_segments, attempts, result_json, last_error, reason_code, created_at, updated_at " +
      "FROM cf_sync_jobs WHERE job_id = ? AND uid = ?",
  )
    .bind(message.body.jobId, message.body.uid)
    .first<SyncJobRow>();
  if (!job) {
    message.ack();
    return;
  }
  const files = await syncJobFiles(env, job.job_id, job.uid);
  if (!files.length || files.length !== job.total_files) {
    if (job.attempts < MAX_PROVIDER_ATTEMPTS) {
      await resetSyncJobForRetry(
        message,
        env,
        "sync file ledger is incomplete",
      );
      return;
    }
    const result = {
      new_memories: [],
      updated_memories: [],
      failed_segments: job.total_files,
      successful_segments: 0,
      total_segments: job.total_files,
      errors: ["sync file ledger is incomplete"],
      outcome: "failed",
    };
    await commitSyncTerminal(
      env,
      job,
      "failed",
      result,
      "sync file ledger is incomplete",
      now,
    );
    await cleanupJobObjects(env, job.job_id, job.uid);
    message.ack();
    return;
  }
  if (job.lane === "fresh") {
    const restriction = await readFairUseRestriction(env.APP_DB, job.uid, now);
    if (restriction) {
      const result = {
        new_memories: [],
        updated_memories: [],
        failed_segments: files.length,
        successful_segments: 0,
        total_segments: files.length,
        errors: ["fair use restricted"],
        outcome: "failed",
      };
      await commitSyncTerminal(
        env,
        job,
        "failed",
        result,
        "fair use restricted",
        now,
      );
      await cleanupJobObjects(env, job.job_id, job.uid);
      message.ack();
      return;
    }
  }

  const transcriptions = new Map<number, FileTranscription>();
  const errors: string[] = [];
  for (const file of files) {
    try {
      transcriptions.set(file.ordinal, await transcribeSyncFile(env, file));
    } catch (error) {
      const messageText = shortError(error);
      if (job.attempts < MAX_PROVIDER_ATTEMPTS) {
        await resetSyncJobForRetry(message, env, messageText);
        return;
      }
      errors.push(`${file.filename}: ${messageText}`);
      await env.APP_DB.prepare(
        "UPDATE cf_sync_job_files SET status = 'failed', last_error = ? WHERE job_id = ? AND ordinal = ?",
      )
        .bind(messageText, job.job_id, file.ordinal)
        .run();
    }
  }

  let finalized: { id: string; created: boolean } | null = null;
  try {
    finalized = await finalizeConversation(
      env,
      job,
      files,
      transcriptions,
      now,
    );
    if (finalized) {
      await persistConversationPlayback(
        env,
        job,
        files,
        transcriptions,
        finalized.id,
        now,
      );
    }
  } catch (error) {
    const messageText = shortError(error);
    if (job.attempts < MAX_PROVIDER_ATTEMPTS) {
      await resetSyncJobForRetry(message, env, messageText);
      return;
    }
    errors.push(messageText);
  }
  const speechMs = [...transcriptions.values()].reduce(
    (total, transcription) => total + transcription.speech_ms,
    0,
  );
  if (speechMs > 0) {
    try {
      await recordFairUseUsage(env.APP_DB, {
        uid: job.uid,
        sourceKind: job.lane === "fresh" ? "sync_fresh" : "sync_backfill",
        sourceId: job.content_id,
        occurredAt: Math.min(...files.map((file) => file.capture_at)),
        speechMs,
      });
    } catch (error) {
      const messageText = shortError(error);
      if (job.attempts < MAX_PROVIDER_ATTEMPTS) {
        await resetSyncJobForRetry(message, env, messageText);
        return;
      }
      errors.push("fair use meter unavailable");
    }
  }
  const successfulSegments = transcriptions.size;
  const failedSegments = files.length - successfulSegments;
  const status: "completed" | "partial_failure" | "failed" =
    errors.length === 0 && successfulSegments === files.length
      ? "completed"
      : successfulSegments > 0 && finalized
        ? "partial_failure"
        : "failed";
  const result = {
    new_memories: finalized?.created ? [finalized.id] : [],
    updated_memories: finalized && !finalized.created ? [finalized.id] : [],
    failed_segments: failedSegments,
    successful_segments: successfulSegments,
    total_segments: files.length,
    errors,
    outcome: status,
  };
  await commitSyncTerminal(env, job, status, result, errors[0] || null, now);
  await cleanupJobObjects(env, job.job_id, job.uid);
  message.ack();
}

export async function reconcileSyncJobs(
  env: JobsEnv,
  now: number,
): Promise<void> {
  const result = await env.APP_DB.prepare(
    "SELECT job_id, uid, lane FROM cf_sync_jobs WHERE " +
      "(status = 'queued' AND updated_at <= ?) OR (status = 'running' AND lease_until <= ?) " +
      "ORDER BY updated_at LIMIT ?",
  )
    .bind(now - 60, now, SYNC_RECONCILE_BATCH_SIZE)
    .all<{ job_id: string; uid: string; lane: Lane }>();
  for (const row of result.results || []) {
    await queueSyncJob(
      env,
      {
        jobId: row.job_id,
        uid: row.uid,
        kind: "sync_local_files",
        payload: { lane: row.lane },
      },
      row.lane,
    );
  }
}

export async function cleanupExpiredSyncState(
  env: JobsEnv,
  now: number,
): Promise<void> {
  const expired = await env.APP_DB.prepare(
    "SELECT files.job_id, files.uid, files.object_key FROM cf_sync_job_files AS files " +
      "JOIN cf_sync_jobs AS jobs ON jobs.job_id = files.job_id WHERE jobs.expires_at <= ? LIMIT ?",
  )
    .bind(now, SYNC_CLEANUP_BATCH_SIZE)
    .all<{ job_id: string; uid: string; object_key: string }>();
  const jobs = new Set<string>();
  for (const row of expired.results || []) {
    try {
      await env.ASSETS.delete(row.object_key);
    } catch {
      continue;
    }
    jobs.add(row.job_id);
  }
  for (const jobId of jobs) {
    await env.APP_DB.prepare(
      "DELETE FROM cf_sync_jobs WHERE job_id = ? AND expires_at <= ?",
    )
      .bind(jobId, now)
      .run();
  }
  await env.APP_DB.batch([
    env.APP_DB.prepare(
      "DELETE FROM cf_sync_content_ledger WHERE expires_at <= ?",
    ).bind(now),
    env.APP_DB.prepare(
      "DELETE FROM cf_sync_capture_claims WHERE expires_at <= ?",
    ).bind(now),
  ]);
}

export async function cleanupOrphanPlaybackObjects(
  env: JobsEnv,
  now: number,
): Promise<void> {
  const result = await env.APP_DB.prepare(
    "SELECT uid, storage_key, conversation_id FROM cf_sync_playback_objects " +
      "WHERE state <> 'committed' AND updated_at <= ? ORDER BY updated_at LIMIT ?",
  )
    .bind(now - PLAYBACK_INTENT_STALE_SECONDS, SYNC_CLEANUP_BATCH_SIZE)
    .all<{ uid: string; storage_key: string; conversation_id: string }>();
  for (const candidate of result.results || []) {
    const conversation = await env.APP_DB.prepare(
      "SELECT audio_files_json, conversation_audio_json FROM cf_conversations WHERE uid = ? AND id = ?",
    )
      .bind(candidate.uid, candidate.conversation_id)
      .first<{
        audio_files_json: string;
        conversation_audio_json: string | null;
      }>();
    const referenced =
      jsonArray(conversation?.audio_files_json).some(
        (value) => objectValue(value)?.storage_key === candidate.storage_key,
      ) ||
      jsonObject(conversation?.conversation_audio_json).storage_key ===
        candidate.storage_key;
    if (referenced) {
      await env.APP_DB.prepare(
        "UPDATE cf_sync_playback_objects SET state = 'committed', updated_at = ? " +
          "WHERE uid = ? AND storage_key = ?",
      )
        .bind(now, candidate.uid, candidate.storage_key)
        .run();
      continue;
    }
    try {
      await env.ASSETS.delete(candidate.storage_key);
    } catch {
      continue;
    }
    await env.APP_DB.prepare(
      "DELETE FROM cf_sync_playback_objects WHERE uid = ? AND storage_key = ? AND state <> 'committed'",
    )
      .bind(candidate.uid, candidate.storage_key)
      .run();
  }
}

export function registerSyncRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  authContext: (c: JobsContext) => Promise<SyncAuthContext | null>,
): void {
  app.post("/v2/sync-capture-manifest", async (c) => {
    const context = await authContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const device = normalizedDevice(c.req.raw);
    if (!device.id)
      return c.json(
        { error: "Fresh capture provenance could not be verified" },
        403,
      );
    let body: Record<string, unknown> | null = null;
    try {
      body = objectValue(await c.req.json());
    } catch {
      return c.json({ error: "invalid capture manifest request" }, 400);
    }
    const conversationId =
      typeof body?.conversation_id === "string"
        ? body.conversation_id.trim()
        : "";
    const claims = validateClaims(body?.files);
    if (!conversationId || conversationId.length > 128 || !claims)
      return c.json({ error: "invalid capture manifest request" }, 400);
    const trusted = await conversationMatchesCapture(
      c.env.APP_DB,
      context.uid,
      conversationId,
      device.id,
      claims.map((claim) => claim.name),
    );
    if (!trusted)
      return c.json(
        { error: "Fresh capture provenance could not be verified" },
        403,
      );
    const now = Math.floor(Date.now() / 1000);
    const fingerprint = claimFingerprint(claims);
    const inserted = await c.env.APP_DB.prepare(
      "INSERT INTO cf_sync_capture_claims (uid, conversation_id, fingerprint, created_at, expires_at) " +
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(uid, conversation_id) DO UPDATE SET " +
        "fingerprint = excluded.fingerprint, created_at = excluded.created_at, expires_at = excluded.expires_at " +
        "WHERE cf_sync_capture_claims.expires_at <= ?",
    )
      .bind(
        context.uid,
        conversationId,
        fingerprint,
        now,
        now + MANIFEST_CLAIM_TTL_SECONDS,
        now,
      )
      .run();
    if (inserted.meta?.changes !== 1) {
      const existing = await c.env.APP_DB.prepare(
        "SELECT fingerprint FROM cf_sync_capture_claims WHERE uid = ? AND conversation_id = ? AND expires_at > ?",
      )
        .bind(context.uid, conversationId, now)
        .first<{ fingerprint: string }>();
      if (existing?.fingerprint !== fingerprint)
        return c.json(
          { error: "Conversation fresh content was already claimed" },
          409,
        );
    }
    const manifest = await issueManifest(
      c.env,
      context.uid,
      device.id,
      conversationId,
      claims,
      now,
    );
    if (!manifest)
      return c.json({ error: "Fresh capture provenance is unavailable" }, 503);
    return c.json({ manifest });
  });

  const syncLocalFilesHandler = async (
    c: JobsContext,
    options: { deprecatedV1?: boolean } = {},
  ): Promise<Response> => {
    const context = await authContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      if (await recordingDeletionActive(c.env, context.uid)) {
        return c.json(
          {
            code: "recording_deletion_in_progress",
            detail: "Recording cleanup is in progress",
          },
          409,
        );
      }
    } catch {
      return c.json(
        {
          code: "sync_admission_unavailable",
          detail: "Sync admission is temporarily unavailable",
        },
        503,
      );
    }
    const declaredLength = Number(c.req.header("content-length"));
    if (
      Number.isFinite(declaredLength) &&
      declaredLength > MAX_SYNC_REQUEST_BYTES
    )
      return c.json({ error: "Audio upload is too large" }, 413);
    const conversationIdRaw = c.req.query("conversation_id")?.trim() || null;
    const conversationId =
      conversationIdRaw && conversationIdRaw.length <= 128
        ? conversationIdRaw
        : null;
    if (conversationIdRaw && !conversationId)
      return c.json({ error: "invalid conversation id" }, 400);

    const jobId = crypto.randomUUID();
    let files: StagedSyncFile[] = [];
    try {
      files = await stageSyncFiles(c.req.raw, c.env, context.uid, jobId);
      const now = Math.floor(Date.now() / 1000);
      const device = normalizedDevice(c.req.raw);
      const claims = await verifyManifest(
        c.env,
        c.req.header("x-omi-sync-capture-manifest") || null,
        context.uid,
        device.id,
        conversationId,
        now,
      );
      const claimsMatch =
        claims !== null &&
        JSON.stringify(claims) ===
          JSON.stringify(
            files
              .map((file) => ({ name: file.filename, sha256: file.sha256 }))
              .sort((left, right) =>
                `${left.name}:${left.sha256}`.localeCompare(
                  `${right.name}:${right.sha256}`,
                ),
              ),
          );
      if (claims && !claimsMatch) {
        await cleanupObjects(c.env, files);
        return c.json(
          {
            code: "capture_manifest_mismatch",
            detail: "Fresh capture manifest did not match the uploaded audio",
          },
          422,
        );
      }
      const serverProof =
        claimsMatch &&
        (await conversationMatchesCapture(
          c.env.APP_DB,
          context.uid,
          conversationId,
          device.id,
          files.map((file) => file.filename),
        ));
      const lane = classifySyncLane(files, serverProof, now);
      if (!lane.automaticRecoveryAllowed) {
        await cleanupObjects(c.env, files);
        return c.json(
          {
            code: "backfill_lookback_exceeded",
            detail:
              "Recording is older than the automatic recovery window; local audio was not consumed",
            lane: lane.lane,
          },
          422,
        );
      }
      if (options.deprecatedV1 && lane.lane === "backfill") {
        await cleanupObjects(c.env, files);
        return c.json(
          {
            code: "backfill_capacity",
            detail:
              "Historical recovery requires the v2 isolated worker; local audio was not consumed",
          },
          503,
          {
            "Retry-After": "30",
            "X-Omi-Rate-Limit-Reason": "backfill_capacity",
          },
        );
      }
      if (lane.lane === "fresh") {
        const restriction = await readFairUseRestriction(
          c.env.APP_DB,
          context.uid,
          now,
        );
        if (restriction) {
          await cleanupObjects(c.env, files);
          return fairUseRestrictionResponse(restriction);
        }
      } else {
        await enforceBackfillAdmission(c.env.APP_DB, context.uid, now);
      }
      const contentId = await computeContentId(c.env, context.uid, files);
      if (!contentId) {
        await cleanupObjects(c.env, files);
        return c.json({ error: "sync idempotency is unavailable" }, 503);
      }
      return admitSyncJob(
        c,
        context.uid,
        jobId,
        files,
        lane,
        conversationId,
        detectSource(files.map((file) => file.filename)),
        device,
        contentId,
        now,
      );
    } catch (error) {
      if (error instanceof SyncHttpError) {
        await cleanupObjects(c.env, files);
        return c.json(
          { code: error.code, detail: error.message },
          error.status as 400,
          error.headers,
        );
      }
      await cleanupObjects(c.env, files);
      return c.json(
        {
          code: "sync_admission_unavailable",
          detail: "Sync admission is temporarily unavailable",
        },
        503,
      );
    }
  };

  // Keep the historical upload path available while making the Cloudflare
  // queue-backed implementation authoritative. The v1 contract is
  // deprecated and clients should move to the asynchronous v2 endpoint.
  const deprecatedSyncLocalFilesHandler = async (
    c: JobsContext,
  ): Promise<Response> => {
    const response = await syncLocalFilesHandler(c, { deprecatedV1: true });
    const headers = new Headers(response.headers);
    headers.set("Deprecation", "true");
    headers.set("Link", '</v2/sync-local-files>; rel="successor-version"');
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  };

  app.post("/v1/sync-local-files", deprecatedSyncLocalFilesHandler);
  app.post("/v2/sync-local-files", (c) => syncLocalFilesHandler(c));

  // The old Cloud Tasks dispatcher may still call this path during the
  // migration window. Rebind it to the durable D1 job and the existing
  // sync_local_files queues. The request is deliberately not allowed to
  // carry a caller-selected uid or staged blob paths: D1 is authoritative
  // for both ownership and the staged-file ledger.
  app.post("/v2/sync-jobs/run", async (c) => {
    const context = await authContext(c);
    if (!context || context.authority !== "better-auth") {
      return c.json({ error: "unauthorized" }, 401);
    }

    let body: unknown;
    try {
      body = await readBoundedSyncRunBody(c.req.raw);
    } catch (error) {
      if (error instanceof SyncHttpError) {
        return c.json(
          { code: error.code, detail: error.message },
          error.status as 400,
        );
      }
      return c.json(
        { code: "invalid_request", detail: "Invalid sync run request" },
        400,
      );
    }
    const payload = objectValue(body);
    const keys = payload ? Object.keys(payload) : [];
    if (!payload || keys.length !== 1 || keys[0] !== "job_id") {
      return c.json(
        {
          code: "invalid_request",
          detail:
            "Only job_id is accepted; uid and raw_blob_paths are not caller-controlled",
        },
        400,
      );
    }
    const jobId =
      typeof payload.job_id === "string" ? payload.job_id.trim() : "";
    if (!SYNC_RUN_JOB_ID.test(jobId)) {
      return c.json(
        { code: "invalid_job_id", detail: "Invalid sync job id" },
        400,
      );
    }

    let job: {
      job_id: string;
      uid: string;
      status: string;
      lane: string;
      lease_until: number | null;
    } | null;
    try {
      job = await c.env.APP_DB.prepare(
        "SELECT job_id, uid, status, lane, lease_until FROM cf_sync_jobs WHERE job_id = ? AND uid = ?",
      )
        .bind(jobId, context.uid)
        .first();
    } catch {
      return c.json({ status: "retry", reason: "sync_job_unavailable" }, 503);
    }
    if (!job) {
      // Do not reveal whether this id belongs to another account.
      return c.json({ status: "dropped", reason: "unknown_job" });
    }
    if (["completed", "partial_failure", "failed"].includes(job.status)) {
      return c.json({ status: "acked", job_status: job.status });
    }
    if (job.status !== "queued" && job.status !== "running") {
      return c.json({ status: "dropped", reason: "unknown_job" });
    }
    if (job.lane !== "fresh" && job.lane !== "backfill") {
      return c.json({ status: "dropped", reason: "invalid_job" });
    }
    const now = Math.floor(Date.now() / 1_000);
    if (
      job.status === "running" &&
      job.lease_until !== null &&
      Number(job.lease_until) > now
    ) {
      return c.json({ status: "locked" }, 409, { "Retry-After": "10" });
    }
    try {
      await queueSyncJob(
        c.env,
        {
          jobId: job.job_id,
          uid: context.uid,
          kind: "sync_local_files",
          payload: { lane: job.lane },
        },
        job.lane,
      );
    } catch {
      return c.json({ status: "retry", reason: "queue_unavailable" }, 503);
    }
    return c.json({ status: "queued", job_id: job.job_id, lane: job.lane });
  });

  app.get("/v2/sync-local-files/:jobId", async (c) => {
    const context = await authContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const jobId = c.req.param("jobId")?.trim() || "";
    if (!jobId || jobId.length > 128)
      return c.json({ error: "invalid job id" }, 400);
    const row = await c.env.APP_DB.prepare(
      "SELECT job_id, status, total_segments, processed_segments, successful_segments, failed_segments, " +
        "result_json, last_error, lane, reason_code, recording_age_seconds FROM cf_sync_jobs " +
        "WHERE job_id = ? AND uid = ?",
    )
      .bind(jobId, context.uid)
      .first<{
        job_id: string;
        status: string;
        total_segments: number;
        processed_segments: number;
        successful_segments: number;
        failed_segments: number;
        result_json: string | null;
        last_error: string | null;
        lane: Lane;
        reason_code: string | null;
        recording_age_seconds: number | null;
      }>();
    if (!row) return c.json({ error: "Sync job not found or expired" }, 404);
    let result: unknown;
    if (row.result_json) {
      try {
        result = JSON.parse(row.result_json);
      } catch {
        result = undefined;
      }
    }
    const terminal = ["completed", "partial_failure", "failed"].includes(
      row.status,
    );
    return c.json({
      job_id: row.job_id,
      status: row.status,
      total_segments: row.total_segments,
      processed_segments: row.processed_segments,
      successful_segments: row.successful_segments,
      failed_segments: row.failed_segments,
      ...(terminal && result !== undefined ? { result } : {}),
      ...(terminal && row.last_error ? { error: row.last_error } : {}),
      lane: row.lane,
      ...(row.reason_code ? { reason_code: row.reason_code } : {}),
      ...(row.recording_age_seconds === null
        ? {}
        : { recording_age_seconds: row.recording_age_seconds }),
    });
  });
}
