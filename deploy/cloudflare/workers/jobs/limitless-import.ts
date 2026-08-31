import {
  FormDataParseError,
  MaxFileSizeExceededError,
  MaxFilesExceededError,
  MaxPartsExceededError,
  MaxTotalSizeExceededError,
  parseFormData,
  type FileUpload,
} from "@remix-run/form-data-parser";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import type { Context, Hono } from "hono";
import type { JobMessage, JobsEnv } from "./env";

const MAX_ZIP_BYTES = 100_000_000;
const MAX_ZIP_ENTRIES = 5_000;
const MAX_MARKDOWN_BYTES = 10_000_000;
const MAX_TOTAL_UNCOMPRESSED_BYTES = 200_000_000;
const MAX_COMPRESSION_RATIO = 200;
const MAX_SEGMENTS = 5_000;
const MAX_TEXT_CHARS = 500_000;
const MAX_IMPORT_ATTEMPTS = 3;
const LEASE_SECONDS = 15 * 60;
const RETRY_SECONDS = 15;

type JobsContext = Context<{ Bindings: JobsEnv }>;
type AuthContext = { uid: string; authority?: string };

export class LimitlessImportError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly retryable = false,
  ) {
    super(message);
  }
}

class ImportHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

type ImportJob = {
  uid: string;
  job_id: string;
  source_object_key: string;
  source_filename: string;
  language_code: string;
  request_fingerprint: string;
  account_generation: number;
  status: string;
  total_files: number;
  processed_files: number;
  conversations_created: number;
  attempts: number;
  lease_token: string | null;
};

function safeUid(uid: string): boolean {
  return uid.length > 0 && uid.length <= 256 && !/[\\/\0]/.test(uid);
}

function objectKey(uid: string, jobId: string): string {
  return `imports/${uid}/${jobId}.zip`;
}

function safeImportObject(uid: string, key: string): boolean {
  return (
    key.startsWith(`imports/${uid}/`) &&
    key.length <= 512 &&
    !key.includes("..") &&
    !/[\\\0]/.test(key)
  );
}

async function hashBytes(value: Uint8Array): Promise<string> {
  return bytesToHex(sha256(value));
}

async function hashFile(file: Blob): Promise<string> {
  const digest = sha256.create();
  const reader = file.stream().getReader();
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      digest.update(part.value);
    }
  } finally {
    reader.releaseLock();
  }
  return bytesToHex(digest.digest());
}

function responseForJob(row: Partial<ImportJob>): Record<string, unknown> {
  return {
    job_id: row.job_id,
    status: row.status,
    total_files: Number(row.total_files || 0),
    processed_files: Number(row.processed_files || 0),
    conversations_created: Number(row.conversations_created || 0),
    source_filename: row.source_filename,
  };
}

async function accountState(
  env: JobsEnv,
  uid: string,
): Promise<{ generation: number; fenced: boolean }> {
  const cutover = await env.APP_DB.prepare(
    "SELECT account_generation FROM cf_account_cutover WHERE uid = ?",
  )
    .bind(uid)
    .first<{ account_generation?: number }>();
  const fence = await env.APP_DB.prepare(
    "SELECT uid FROM cf_account_deletion_intents WHERE uid = ? " +
      "UNION ALL SELECT uid FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ? LIMIT 1",
  )
    .bind(uid, uid, Math.floor(Date.now() / 1000))
    .first();
  return {
    generation: Number(cutover?.account_generation || 0),
    fenced: !!fence,
  };
}

async function jobByFingerprint(
  env: JobsEnv,
  uid: string,
  fingerprint: string,
): Promise<ImportJob | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_import_jobs WHERE uid = ? AND request_fingerprint = ?",
  )
    .bind(uid, fingerprint)
    .first<ImportJob>();
}

async function jobById(
  env: JobsEnv,
  uid: string,
  jobId: string,
): Promise<ImportJob | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_import_jobs WHERE uid = ? AND job_id = ?",
  )
    .bind(uid, jobId)
    .first<ImportJob>();
}

async function enqueueLimitless(c: JobsContext, job: ImportJob): Promise<void> {
  await c.env.JOBS.send({
    jobId: job.job_id,
    uid: job.uid,
    kind: "limitless_import",
    payload: { sourceObjectKey: job.source_object_key },
  });
}

function languageFromValue(value: unknown): string {
  const language =
    typeof value === "string" && value.trim() ? value.trim() : "en";
  if (!/^[A-Za-z]{2,16}(?:[-_][A-Za-z0-9]{1,16})?$/.test(language)) {
    throw new ImportHttpError(400, "invalid_language", "language is invalid");
  }
  return language;
}

export function registerLimitlessImportRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (c: JobsContext) => Promise<AuthContext | null>,
): void {
  app.post("/v1/import/limitless", async (c) => {
    const context = await requestContext(c);
    if (!context || !safeUid(context.uid))
      return c.json({ error: "unauthorized" }, 401);
    const contentLength = Number(c.req.header("content-length") || 0);
    if (contentLength > MAX_ZIP_BYTES)
      return c.json({ error: "upload_too_large" }, 413);
    let upload: FileUpload | null = null;
    let language = "en";
    try {
      const form = await parseFormData(
        c.req.raw,
        {
          maxFiles: 1,
          maxFileSize: MAX_ZIP_BYTES,
          maxParts: 3,
          maxTotalSize: MAX_ZIP_BYTES,
        },
        async (file: FileUpload) => {
          if (file.fieldName !== "file" && file.fieldName !== "files") {
            throw new ImportHttpError(
              400,
              "invalid_multipart",
              "expected a ZIP file",
            );
          }
          if (upload)
            throw new ImportHttpError(
              400,
              "invalid_multipart",
              "only one ZIP is accepted",
            );
          upload = file;
          return null;
        },
      );
      language = languageFromValue(form.get("language"));
    } catch (error) {
      if (error instanceof ImportHttpError) {
        return c.json(
          { error: error.code, message: error.message },
          error.status as 400,
        );
      }
      if (
        error instanceof MaxFileSizeExceededError ||
        error instanceof MaxTotalSizeExceededError ||
        error instanceof MaxFilesExceededError
      )
        return c.json({ error: "upload_too_large" }, 413);
      if (
        error instanceof FormDataParseError ||
        error instanceof MaxPartsExceededError
      ) {
        return c.json({ error: "invalid_multipart" }, 400);
      }
      return c.json({ error: "upload_unavailable" }, 503);
    }
    const stagedUpload = upload as FileUpload | null;
    if (
      !stagedUpload ||
      !stagedUpload.size ||
      !/\.zip$/i.test(stagedUpload.name || "")
    ) {
      return c.json(
        { error: "invalid_zip", message: "a non-empty .zip file is required" },
        400,
      );
    }
    const contentHash = await hashFile(stagedUpload);
    const fingerprint = await hashBytes(
      new TextEncoder().encode(`${context.uid}\0${language}\0${contentHash}`),
    );
    const state = await accountState(c.env, context.uid);
    if (state.fenced)
      return c.json({ error: "account_deletion_in_progress" }, 409);
    const existing = await jobByFingerprint(c.env, context.uid, fingerprint);
    if (existing) return c.json(responseForJob(existing), 200);
    const jobId = crypto.randomUUID();
    const sourceObjectKey = objectKey(context.uid, jobId);
    const now = Math.floor(Date.now() / 1000);
    const inserted = await c.env.APP_DB.prepare(
      "INSERT INTO cf_import_jobs (uid, job_id, source_type, source_object_key, source_filename, language_code, request_fingerprint, account_generation, status, created_at, updated_at) " +
        "VALUES (?, ?, 'limitless', ?, ?, ?, ?, ?, 'pending', ?, ?) ON CONFLICT DO NOTHING",
    )
      .bind(
        context.uid,
        jobId,
        sourceObjectKey,
        stagedUpload.name.slice(0, 512),
        language,
        fingerprint,
        state.generation,
        now,
        now,
      )
      .run();
    if (inserted.meta?.changes !== 1) {
      const raced = await jobByFingerprint(c.env, context.uid, fingerprint);
      if (!raced) return c.json({ error: "import_unavailable" }, 503);
      return c.json(responseForJob(raced), 200);
    }
    try {
      await c.env.ASSETS.put(sourceObjectKey, stagedUpload, {
        httpMetadata: { contentType: "application/zip" },
        customMetadata: { uid: context.uid, jobId, fingerprint },
      });
      await enqueueLimitless(c, {
        uid: context.uid,
        job_id: jobId,
        source_object_key: sourceObjectKey,
        source_filename: stagedUpload.name,
        language_code: language,
        request_fingerprint: fingerprint,
        account_generation: state.generation,
        status: "pending",
        total_files: 0,
        processed_files: 0,
        conversations_created: 0,
        attempts: 0,
        lease_token: null,
      });
    } catch {
      await c.env.APP_DB.prepare(
        "UPDATE cf_import_jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE uid = ? AND job_id = ?",
      )
        .bind("import staging or queue unavailable", now, context.uid, jobId)
        .run();
      try {
        await c.env.ASSETS.delete(sourceObjectKey);
      } catch {
        /* scheduled residual cleanup */
      }
      return c.json({ error: "import_unavailable" }, 503);
    }
    return c.json(
      responseForJob({
        ...{ job_id: jobId, status: "pending" },
        source_filename: stagedUpload.name,
      }),
      202,
    );
  });
}

function u16(bytes: Uint8Array, at: number): number {
  return bytes[at] | (bytes[at + 1] << 8);
}
function u32(bytes: Uint8Array, at: number): number {
  return (
    (bytes[at] |
      (bytes[at + 1] << 8) |
      (bytes[at + 2] << 16) |
      (bytes[at + 3] << 24)) >>>
    0
  );
}
function text(bytes: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new LimitlessImportError(
      "malformed_utf8",
      "ZIP filename or markdown is not valid UTF-8",
    );
  }
}

function validZipPath(name: string): boolean {
  if (
    !name ||
    name.length > 512 ||
    name.includes("\0") ||
    name.startsWith("/") ||
    name.includes("\\")
  )
    return false;
  const normalized = name.endsWith("/") ? name.slice(0, -1) : name;
  return (
    normalized.length > 0 &&
    normalized
      .split("/")
      .every((part) => part !== ".." && part !== "." && part.length > 0)
  );
}

function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (const value of bytes) {
    crc ^= value;
    for (let i = 0; i < 8; i++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

type ZipEntry = {
  name: string;
  compressed: number;
  uncompressed: number;
  method: number;
  crc: number;
  local: number;
};

async function zipEntries(bytes: Uint8Array): Promise<ZipEntry[]> {
  if (bytes.length < 22 || bytes.length > MAX_ZIP_BYTES)
    throw new LimitlessImportError("invalid_zip", "ZIP size is invalid");
  let eocd = -1;
  for (
    let i = bytes.length - 22;
    i >= Math.max(0, bytes.length - 65_557);
    i--
  ) {
    if (u32(bytes, i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0)
    throw new LimitlessImportError("invalid_zip", "ZIP end record is missing");
  const count = u16(bytes, eocd + 10);
  const cdSize = u32(bytes, eocd + 12);
  const cdOffset = u32(bytes, eocd + 16);
  if (
    !count ||
    count > MAX_ZIP_ENTRIES ||
    cdOffset + cdSize > bytes.length ||
    count === 0xffff ||
    cdSize > 5_000_000
  ) {
    throw new LimitlessImportError(
      "zip_limits",
      "ZIP central directory exceeds limits",
    );
  }
  const entries: ZipEntry[] = [];
  const seen = new Set<string>();
  let cursor = cdOffset;
  let total = 0;
  for (let i = 0; i < count; i++) {
    if (cursor + 46 > cdOffset + cdSize || u32(bytes, cursor) !== 0x02014b50)
      throw new LimitlessImportError("invalid_zip", "ZIP entry is malformed");
    const flags = u16(bytes, cursor + 8);
    const method = u16(bytes, cursor + 10);
    const crc = u32(bytes, cursor + 16);
    const compressed = u32(bytes, cursor + 20);
    const uncompressed = u32(bytes, cursor + 24);
    const nameLength = u16(bytes, cursor + 28);
    const extraLength = u16(bytes, cursor + 30);
    const commentLength = u16(bytes, cursor + 32);
    const local = u32(bytes, cursor + 42);
    const name = text(bytes.slice(cursor + 46, cursor + 46 + nameLength));
    if (
      flags & 1 ||
      !validZipPath(name) ||
      seen.has(name) ||
      [compressed, uncompressed, local].some((v) => v === 0xffffffff)
    ) {
      throw new LimitlessImportError(
        "unsafe_zip",
        "ZIP contains an unsafe entry",
      );
    }
    seen.add(name);
    if (method !== 0 && method !== 8)
      throw new LimitlessImportError(
        "unsupported_zip",
        "ZIP compression is unsupported",
      );
    if (
      uncompressed > MAX_MARKDOWN_BYTES ||
      compressed > MAX_ZIP_BYTES ||
      (compressed === 0 && uncompressed > 0) ||
      uncompressed > compressed * MAX_COMPRESSION_RATIO + 1_024 * 1_024
    ) {
      throw new LimitlessImportError(
        "zip_bomb",
        "ZIP entry exceeds expansion limits",
      );
    }
    total += uncompressed;
    if (total > MAX_TOTAL_UNCOMPRESSED_BYTES)
      throw new LimitlessImportError(
        "zip_bomb",
        "ZIP expansion exceeds limits",
      );
    entries.push({ name, compressed, uncompressed, method, crc, local });
    cursor += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

async function unzipEntry(
  bytes: Uint8Array,
  entry: ZipEntry,
): Promise<Uint8Array> {
  if (entry.local + 30 > bytes.length || u32(bytes, entry.local) !== 0x04034b50)
    throw new LimitlessImportError(
      "invalid_zip",
      "ZIP local entry is malformed",
    );
  const nameLength = u16(bytes, entry.local + 26);
  const extraLength = u16(bytes, entry.local + 28);
  const start = entry.local + 30 + nameLength + extraLength;
  const compressed = bytes.slice(start, start + entry.compressed);
  if (compressed.length !== entry.compressed)
    throw new LimitlessImportError("invalid_zip", "ZIP data is truncated");
  let output: Uint8Array;
  try {
    output =
      entry.method === 0
        ? compressed
        : new Uint8Array(
            await new Response(
              new Blob([compressed])
                .stream()
                .pipeThrough(new DecompressionStream("deflate-raw")),
            ).arrayBuffer(),
          );
  } catch {
    throw new LimitlessImportError(
      "invalid_zip",
      "ZIP deflate stream is malformed",
    );
  }
  if (output.length !== entry.uncompressed || crc32(output) !== entry.crc)
    throw new LimitlessImportError(
      "invalid_zip",
      "ZIP checksum or size is invalid",
    );
  return output;
}

type ParsedConversation = {
  id: string;
  title: string;
  startedAt: number;
  finishedAt: number;
  segments: Array<Record<string, unknown>>;
  language: string;
};

export function parseLimitlessMarkdown(
  bytes: Uint8Array,
  filename: string,
  language: string,
  id: string,
): ParsedConversation | null {
  if (bytes.length > MAX_MARKDOWN_BYTES)
    throw new LimitlessImportError(
      "markdown_too_large",
      "markdown file is too large",
    );
  const markdown = text(bytes);
  if (markdown.length > MAX_TEXT_CHARS)
    throw new LimitlessImportError(
      "markdown_too_large",
      "markdown text is too large",
    );
  const basename = filename.split("/").pop() || filename;
  const match = /^(\d{4}-\d{2}-\d{2})_(\d{2})h(\d{2})m(\d{2})s_(.+)\.md$/i.exec(
    basename,
  );
  if (!match) return null;
  const [year, month, day] = match[1].split("-").map(Number);
  const startedAt = Date.UTC(
    year,
    month - 1,
    day,
    Number(match[2]),
    Number(match[3]),
    Number(match[4]),
  );
  const date = new Date(startedAt);
  if (
    !Number.isFinite(startedAt) ||
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day ||
    Number(match[2]) > 23 ||
    Number(match[3]) > 59 ||
    Number(match[4]) > 59
  ) {
    throw new LimitlessImportError(
      "invalid_filename",
      "Limitless filename has an invalid date",
    );
  }
  const titleMatch = /^#\s+(.+)$/m.exec(markdown);
  const title =
    (titleMatch?.[1] || match[5].replace(/[-_]+/g, " "))
      .trim()
      .slice(0, 2_048) || "Imported conversation";
  const segments: Array<Record<string, unknown>> = [];
  let firstMs = Number.POSITIVE_INFINITY;
  let lastMs = 0;
  const pattern = /^>\s*\[(\d+)\]\(#startMs=(\d+)&endMs=(\d+)\):\s*(.+)$/gm;
  for (
    let found = pattern.exec(markdown);
    found;
    found = pattern.exec(markdown)
  ) {
    if (segments.length >= MAX_SEGMENTS)
      throw new LimitlessImportError(
        "too_many_segments",
        "conversation has too many transcript segments",
      );
    const startMs = Number(found[2]);
    const endMs = Number(found[3]);
    if (
      !Number.isSafeInteger(startMs) ||
      !Number.isSafeInteger(endMs) ||
      endMs < startMs
    )
      throw new LimitlessImportError(
        "invalid_transcript",
        "transcript timestamps are invalid",
      );
    firstMs = Math.min(firstMs, startMs);
    lastMs = Math.max(lastMs, endMs);
    const speakerId = Number(found[1]);
    segments.push({
      id: `${id}:${segments.length}`,
      text: found[4].trim().slice(0, 20_000),
      start: startMs / 1000,
      end: endMs / 1000,
      speaker: speakerId === 1 ? "user" : "speaker",
      speaker_id: speakerId,
      is_user: speakerId === 1,
      person_id: null,
    });
  }
  if (!segments.length) return null;
  const originMs = Number.isFinite(firstMs) ? firstMs : 0;
  for (const segment of segments) {
    segment.start = (Number(segment.start) * 1000 - originMs) / 1000;
    segment.end = (Number(segment.end) * 1000 - originMs) / 1000;
  }
  return {
    id,
    title,
    startedAt: Math.floor(startedAt / 1000),
    finishedAt: Math.floor(
      startedAt / 1000 +
        Math.max(0, lastMs - (Number.isFinite(firstMs) ? firstMs : 0)) / 1000,
    ),
    segments,
    language,
  };
}

async function deterministicId(seed: string): Promise<string> {
  const digest = sha256(new TextEncoder().encode(seed)).slice(0, 16);
  digest[6] = (digest[6] & 0x0f) | 0x40;
  digest[8] = (digest[8] & 0x3f) | 0x80;
  const hex = bytesToHex(digest);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function insertConversation(
  env: JobsEnv,
  uid: string,
  conversation: ParsedConversation,
  now: number,
): Promise<boolean> {
  const structured = JSON.stringify({
    title: conversation.title,
    overview: "Imported from Limitless",
    emoji: "📝",
    category: "other",
    action_items: [],
    events: [],
  });
  const inserted = await env.APP_DB.prepare(
    "INSERT OR IGNORE INTO cf_conversations (uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, starred, discarded, is_locked, deferred, private_cloud_sync_enabled, structured_json, transcript_segments_json, photos_json, audio_files_json, conversation_audio_json, apps_results_json, suggested_apps_json) " +
      "VALUES (?, ?, ?, ?, ?, ?, 'limitless', ?, 'completed', 'private', 0, 0, 0, 0, 0, ?, ?, '[]', '[]', NULL, '[]', '[]') " +
      "RETURNING id",
  )
    .bind(
      uid,
      conversation.id,
      conversation.startedAt,
      now,
      conversation.startedAt,
      conversation.finishedAt,
      conversation.language,
      structured,
      JSON.stringify(conversation.segments),
    )
    .first<{ id: string }>();
  return inserted?.id === conversation.id;
}

async function setImportFailed(
  env: JobsEnv,
  job: ImportJob,
  error: string,
  now: number,
): Promise<void> {
  await env.APP_DB.prepare(
    "UPDATE cf_import_jobs SET status = 'failed', last_error = ?, lease_token = NULL, lease_until = NULL, completed_at = ?, updated_at = ? WHERE uid = ? AND job_id = ?",
  )
    .bind(error.slice(0, 2048), now, now, job.uid, job.job_id)
    .run();
}

async function cleanupImportObject(
  env: JobsEnv,
  job: ImportJob,
): Promise<void> {
  if (safeImportObject(job.uid, job.source_object_key))
    await env.ASSETS.delete(job.source_object_key);
}

export async function processLimitlessImportMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const job = await jobById(env, message.body.uid, message.body.jobId);
  if (
    !job ||
    job.status === "cancelled" ||
    job.status === "completed" ||
    job.status === "failed"
  ) {
    if (job) await cleanupImportObject(env, job);
    message.ack();
    return;
  }
  const state = await accountState(env, job.uid);
  if (state.fenced || state.generation !== Number(job.account_generation)) {
    await setImportFailed(
      env,
      job,
      "account deletion or generation fence",
      Math.floor(Date.now() / 1000),
    );
    await cleanupImportObject(env, job);
    message.ack();
    return;
  }
  const now = Math.floor(Date.now() / 1000);
  const token = crypto.randomUUID();
  const claimed = await env.APP_DB.prepare(
    "UPDATE cf_import_jobs SET status = 'processing', attempts = attempts + 1, lease_token = ?, lease_until = ?, started_at = COALESCE(started_at, ?), updated_at = ? WHERE uid = ? AND job_id = ? AND (status = 'pending' OR (status = 'processing' AND lease_until < ?))",
  )
    .bind(token, now + LEASE_SECONDS, now, now, job.uid, job.job_id, now)
    .run();
  if (claimed.meta?.changes !== 1) {
    message.retry({ delaySeconds: RETRY_SECONDS });
    return;
  }
  try {
    const source = await env.ASSETS.get(job.source_object_key);
    if (!source)
      throw new LimitlessImportError("source_missing", "staged ZIP is missing");
    const bytes = new Uint8Array(await source.arrayBuffer());
    const entries = await zipEntries(bytes);
    const lifelogs = entries.filter((entry) =>
      /(^|\/)lifelogs\/.*\.md$/i.test(entry.name),
    );
    await env.APP_DB.prepare(
      "UPDATE cf_import_jobs SET total_files = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND lease_token = ?",
    )
      .bind(lifelogs.length, now, job.uid, job.job_id, token)
      .run();
    let processed = 0;
    let created = 0;
    for (const entry of lifelogs) {
      const current = await jobById(env, job.uid, job.job_id);
      if (!current || current.status === "cancelled") {
        await cleanupImportObject(env, job);
        message.ack();
        return;
      }
      const markdown = await unzipEntry(bytes, entry);
      const id = await deterministicId(
        `limitless\0${job.uid}\0${job.job_id}\0${entry.name}`,
      );
      const conversation = parseLimitlessMarkdown(
        markdown,
        entry.name,
        job.language_code,
        id,
      );
      if (
        conversation &&
        (await insertConversation(env, job.uid, conversation, now))
      )
        created++;
      processed++;
      await env.APP_DB.prepare(
        "UPDATE cf_import_jobs SET processed_files = ?, conversations_created = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND lease_token = ?",
      )
        .bind(
          processed,
          created,
          Math.floor(Date.now() / 1000),
          job.uid,
          job.job_id,
          token,
        )
        .run();
    }
    if (!lifelogs.length)
      throw new LimitlessImportError(
        "no_lifelogs",
        "ZIP contains no Limitless markdown files",
      );
    await env.APP_DB.prepare(
      "UPDATE cf_import_jobs SET status = 'completed', completed_at = ?, lease_token = NULL, lease_until = NULL, updated_at = ? WHERE uid = ? AND job_id = ? AND lease_token = ?",
    )
      .bind(
        Math.floor(Date.now() / 1000),
        Math.floor(Date.now() / 1000),
        job.uid,
        job.job_id,
        token,
      )
      .run();
    await cleanupImportObject(env, job);
    message.ack();
  } catch (error) {
    const messageText =
      error instanceof LimitlessImportError
        ? error.message
        : "import unavailable";
    if (
      error instanceof LimitlessImportError ||
      message.attempts >= MAX_IMPORT_ATTEMPTS
    ) {
      await setImportFailed(
        env,
        job,
        messageText,
        Math.floor(Date.now() / 1000),
      );
      await cleanupImportObject(env, job);
      message.ack();
      return;
    }
    await env.APP_DB.prepare(
      "UPDATE cf_import_jobs SET status = 'pending', last_error = ?, lease_token = NULL, lease_until = NULL, updated_at = ? WHERE uid = ? AND job_id = ? AND lease_token = ?",
    )
      .bind(
        messageText,
        Math.floor(Date.now() / 1000),
        job.uid,
        job.job_id,
        token,
      )
      .run();
    message.retry({ delaySeconds: RETRY_SECONDS });
  }
}

export async function cleanupExpiredLimitlessImports(
  env: JobsEnv,
  now: number,
): Promise<void> {
  const rows = await env.APP_DB.prepare(
    "SELECT * FROM cf_import_jobs WHERE status IN ('completed', 'failed', 'cancelled') AND updated_at < ? LIMIT 50",
  )
    .bind(now - 15 * 60)
    .all<ImportJob>();
  for (const job of rows.results || []) {
    try {
      await cleanupImportObject(env, job);
    } catch {
      /* next scheduled sweep */
    }
  }
}

export { validZipPath };
