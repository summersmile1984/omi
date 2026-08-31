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
import type { JobsEnv } from "./env";

const MAX_FILE_BYTES = 50 * 1024 * 1024;
const MAX_TOTAL_BYTES = 100 * 1024 * 1024;
const MAX_FILES = 10;
const MAX_NAME_BYTES = 512;
const PROVIDER_URL = "https://api.openai.com/v1/files";

type JobsContext = Context<{ Bindings: JobsEnv }>;
type AuthContext = { uid: string; authority?: string };

class ChatFileHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

type ChatFileRow = {
  uid: string;
  file_id: string;
  request_fingerprint: string;
  provider: string;
  provider_file_id: string | null;
  name: string;
  mime_type: string;
  size: number;
  checksum_sha256: string;
  storage_key: string;
  thumbnail_key: string | null;
  status: string;
  thumbnail_status: string;
  created_at: number;
  updated_at: number;
  last_error: string | null;
};

function validUid(uid: string): boolean {
  return uid.length > 0 && uid.length <= 256 && !/[\\/\0]/.test(uid);
}

function safeName(name: string): string {
  const basename = name.split(/[\\/]/).pop()?.trim() || "upload";
  if (
    !basename ||
    basename.length > MAX_NAME_BYTES ||
    basename.includes("\0")
  ) {
    throw new ChatFileHttpError(
      400,
      "invalid_filename",
      "file name is invalid",
    );
  }
  return basename;
}

function mimeType(file: FileUpload): string {
  const value = String(file.type || "application/octet-stream")
    .trim()
    .toLowerCase();
  if (
    !/^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(value) ||
    value.length > 200
  ) {
    throw new ChatFileHttpError(
      400,
      "invalid_mime_type",
      "file MIME type is invalid",
    );
  }
  return value;
}

function response(row: Partial<ChatFileRow>): Record<string, unknown> {
  return {
    id: row.file_id,
    name: row.name,
    thumbnail: "",
    mime_type: row.mime_type,
    openai_file_id: row.provider_file_id,
    created_at: new Date(Number(row.created_at || 0) * 1000).toISOString(),
    thumb_name: "",
  };
}

const THUMBNAIL_TTL_SECONDS = 15 * 60;
const MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024;

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function constantTimeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1)
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

async function thumbnailSignature(
  secret: string,
  uid: string,
  fileId: string,
  expires: number,
): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`${uid}\0${fileId}\0${expires}`),
  );
  return base64Url(new Uint8Array(digest));
}

async function thumbnailUrl(
  env: JobsEnv,
  row: Partial<ChatFileRow>,
): Promise<string> {
  if (
    row.thumbnail_status !== "ready" ||
    !row.thumbnail_key ||
    !row.uid ||
    !row.file_id
  )
    return "";
  const secret = String(env.CHAT_FILE_THUMBNAIL_SECRET || "").trim();
  const base = String(env.PUBLIC_API_BASE_URL || "").replace(/\/$/, "");
  if (!secret || !base) return "";
  const expires = Math.floor(Date.now() / 1000) + THUMBNAIL_TTL_SECONDS;
  const signature = await thumbnailSignature(
    secret,
    row.uid,
    row.file_id,
    expires,
  );
  return `${base}/v1/cf/chat-files/${encodeURIComponent(row.file_id)}/thumbnail?uid=${encodeURIComponent(row.uid)}&exp=${expires}&sig=${encodeURIComponent(signature)}`;
}

async function responseWithThumbnail(
  env: JobsEnv,
  row: Partial<ChatFileRow>,
): Promise<Record<string, unknown>> {
  const value = response(row);
  value.thumbnail = await thumbnailUrl(env, row);
  return value;
}

function bytesStream(bytes: Uint8Array): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  });
}

async function renderThumbnail(
  env: JobsEnv,
  bytes: Uint8Array,
): Promise<{ bytes: Uint8Array; contentType: string }> {
  if (!env.IMAGES || !String(env.CHAT_FILE_THUMBNAIL_SECRET || "").trim())
    throw new ChatFileHttpError(
      503,
      "thumbnail_unavailable",
      "image thumbnails are not configured on the Cloudflare boundary",
    );
  try {
    const transformed = await env.IMAGES.input(bytesStream(bytes)).transform({
      width: 128,
      height: 128,
      fit: "scale-down",
    }).output({ format: "jpeg", quality: 85 });
    const output = new Uint8Array(await transformed.response().arrayBuffer());
    if (!output.length || output.length > MAX_THUMBNAIL_BYTES)
      throw new ChatFileHttpError(
        503,
        "thumbnail_unavailable",
        "image thumbnail output is invalid",
      );
    return {
      bytes: output,
      contentType: transformed.contentType() || "image/jpeg",
    };
  } catch (error) {
    if (error instanceof ChatFileHttpError) throw error;
    // Cloudflare Images uses code 9412 for a non-image input.  Treat that as
    // invalid client input; other transformation failures are capability or
    // provider failures and must not result in a successful upload without a
    // thumbnail URL.
    if ((error as { code?: unknown })?.code === 9412)
      throw new ChatFileHttpError(
        400,
        "unsupported_file",
        "file contents are not a supported image",
      );
    throw new ChatFileHttpError(
      503,
      "thumbnail_unavailable",
      "image thumbnail generation is unavailable",
    );
  }
}

async function hashFile(
  file: Blob,
): Promise<{ bytes: Uint8Array; checksum: string }> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  return { bytes, checksum: bytesToHex(sha256(bytes)) };
}

async function providerUpload(
  env: JobsEnv,
  bytes: Uint8Array,
  name: string,
  mime: string,
  purpose: "assistants" | "vision",
): Promise<string> {
  const key = String(env.OPENAI_API_KEY || "").trim();
  if (!key)
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider is not configured",
    );
  const form = new FormData();
  form.set("purpose", purpose);
  form.set(
    "file",
    new File([bytes.buffer as ArrayBuffer], name, { type: mime }),
  );
  let result: Response;
  try {
    result = await fetch(PROVIDER_URL, {
      method: "POST",
      headers: { authorization: `Bearer ${key}` },
      body: form,
    });
  } catch {
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider is unavailable",
    );
  }
  if (!result.ok) {
    if (result.status === 400 || result.status === 415) {
      throw new ChatFileHttpError(
        400,
        "unsupported_file",
        "file provider rejected this file type",
      );
    }
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider rejected the upload",
    );
  }
  let payload: unknown;
  try {
    payload = await result.json();
  } catch {
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider returned invalid data",
    );
  }
  const providerId =
    typeof (payload as { id?: unknown })?.id === "string"
      ? (payload as { id: string }).id
      : "";
  if (!/^file-[A-Za-z0-9_-]{1,256}$/.test(providerId))
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider returned no file id",
    );
  return providerId;
}

async function providerDelete(env: JobsEnv, providerId: string): Promise<void> {
  const key = String(env.OPENAI_API_KEY || "").trim();
  if (!key)
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider is not configured",
    );
  let result: Response;
  try {
    result = await fetch(`${PROVIDER_URL}/${encodeURIComponent(providerId)}`, {
      method: "DELETE",
      headers: { authorization: `Bearer ${key}` },
    });
  } catch {
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider is unavailable",
    );
  }
  if (!result.ok && result.status !== 404)
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider rejected deletion",
    );
}

async function existingByFingerprint(
  env: JobsEnv,
  uid: string,
  fingerprint: string,
): Promise<ChatFileRow | null> {
  return env.APP_DB.prepare(
    "SELECT * FROM cf_chat_files WHERE uid = ? AND request_fingerprint = ?",
  )
    .bind(uid, fingerprint)
    .first<ChatFileRow>();
}

function privateKey(uid: string, fileId: string): string {
  return `${uid}/${fileId}`;
}

async function parseUploads(request: Request): Promise<FileUpload[]> {
  const uploads: FileUpload[] = [];
  try {
    await parseFormData(
      request,
      {
        maxFiles: MAX_FILES,
        maxFileSize: MAX_FILE_BYTES,
        maxParts: MAX_FILES + 2,
        maxTotalSize: MAX_TOTAL_BYTES,
      },
      async (file: FileUpload) => {
        if (file.fieldName !== "files" && file.fieldName !== "file")
          throw new ChatFileHttpError(
            400,
            "invalid_multipart",
            "expected files fields",
          );
        if (!file.size)
          throw new ChatFileHttpError(400, "empty_file", "file is empty");
        uploads.push(file);
        return null;
      },
    );
  } catch (error) {
    if (error instanceof ChatFileHttpError) throw error;
    if (
      error instanceof MaxFileSizeExceededError ||
      error instanceof MaxTotalSizeExceededError ||
      error instanceof MaxFilesExceededError
    )
      throw new ChatFileHttpError(
        413,
        "upload_too_large",
        "chat file upload is too large",
      );
    if (
      error instanceof FormDataParseError ||
      error instanceof MaxPartsExceededError
    )
      throw new ChatFileHttpError(
        400,
        "invalid_multipart",
        "chat file upload is not valid multipart data",
      );
    throw new ChatFileHttpError(
      503,
      "upload_unavailable",
      "chat file upload is unavailable",
    );
  }
  if (!uploads.length)
    throw new ChatFileHttpError(
      400,
      "missing_file",
      "at least one file is required",
    );
  return uploads;
}

type UploadedChatFile = {
  row: ChatFileRow;
  newlyReady: boolean;
};

async function uploadOne(
  env: JobsEnv,
  uid: string,
  file: FileUpload,
): Promise<UploadedChatFile> {
  const name = safeName(file.name || "upload");
  const mime = mimeType(file);
  const image = mime.startsWith("image/");
  const { bytes, checksum } = await hashFile(file);
  // Render before writing any authority.  A successful image upload without a
  // corresponding thumbnail would be a false success for the legacy clients,
  // whose response and persisted message model both expose `thumbnail`.
  const thumbnail = image ? await renderThumbnail(env, bytes) : null;
  const fingerprint = bytesToHex(
    sha256(new TextEncoder().encode(`${uid}\0${name}\0${mime}\0${checksum}`)),
  );
  const existing = await existingByFingerprint(env, uid, fingerprint);
  if (existing?.status === "ready") return { row: existing, newlyReady: false };
  if (existing?.status === "staging")
    throw new ChatFileHttpError(
      409,
      "upload_in_progress",
      "file upload is already in progress",
    );
  const fileId = existing?.file_id || crypto.randomUUID();
  const storageKey = existing?.storage_key || privateKey(uid, fileId);
  const thumbnailKey = image ? `${storageKey}/thumbnail.jpg` : null;
  const now = Math.floor(Date.now() / 1000);
  if (!storageKey.startsWith(`${uid}/`))
    throw new ChatFileHttpError(
      503,
      "metadata_unavailable",
      "file metadata is invalid",
    );
  const metadata = await env.APP_DB.prepare(
    "INSERT INTO cf_chat_files (uid, file_id, request_fingerprint, provider, provider_file_id, name, mime_type, size, checksum_sha256, storage_key, thumbnail_key, status, thumbnail_status, created_at, updated_at) VALUES (?, ?, ?, 'openai', NULL, ?, ?, ?, ?, ?, ?, 'staging', ?, ?, ?) " +
      "ON CONFLICT(uid, request_fingerprint) DO UPDATE SET provider_file_id = NULL, name = excluded.name, mime_type = excluded.mime_type, size = excluded.size, checksum_sha256 = excluded.checksum_sha256, storage_key = excluded.storage_key, thumbnail_key = excluded.thumbnail_key, status = 'staging', thumbnail_status = excluded.thumbnail_status, last_error = NULL, updated_at = excluded.updated_at",
  )
    .bind(
      uid,
      fileId,
      fingerprint,
      name,
      mime,
      bytes.length,
      checksum,
      storageKey,
      thumbnailKey,
      image ? "unsupported" : "not_applicable",
      now,
      now,
    )
    .run();
  if (metadata.meta?.changes !== 1 && !existing)
    throw new ChatFileHttpError(
      503,
      "metadata_unavailable",
      "file metadata is unavailable",
    );
  let providerId: string | null = null;
  try {
    if (!env.CHAT_FILES)
      throw new ChatFileHttpError(
        503,
        "storage_unavailable",
        "chat file storage is not configured",
      );
    await env.CHAT_FILES.put(storageKey, bytes, {
      httpMetadata: { contentType: mime },
      customMetadata: { uid, fileId, checksum },
    });
    if (thumbnail && thumbnailKey) {
      await env.CHAT_FILES.put(thumbnailKey, thumbnail.bytes, {
        httpMetadata: { contentType: thumbnail.contentType },
        customMetadata: { uid, fileId, kind: "thumbnail" },
      });
    }
    providerId = await providerUpload(
      env,
      bytes,
      name,
      mime,
      image ? "vision" : "assistants",
    );
    await env.APP_DB.prepare(
      "UPDATE cf_chat_files SET provider_file_id = ?, status = 'ready', thumbnail_status = ?, updated_at = ?, last_error = NULL WHERE uid = ? AND file_id = ? AND status = 'staging'",
    )
      .bind(providerId, image ? "ready" : "not_applicable", now, uid, fileId)
      .run();
    const ready = await env.APP_DB.prepare(
      "SELECT * FROM cf_chat_files WHERE uid = ? AND file_id = ? AND status = 'ready'",
    )
      .bind(uid, fileId)
      .first<ChatFileRow>();
    if (!ready)
      throw new ChatFileHttpError(
        503,
        "metadata_unavailable",
        "file metadata commit failed",
      );
    return { row: ready, newlyReady: true };
  } catch (error) {
    const message =
      error instanceof ChatFileHttpError
        ? error.message
        : "file upload unavailable";
    await env.APP_DB.prepare(
      "UPDATE cf_chat_files SET status = 'failed', last_error = ?, updated_at = ? WHERE uid = ? AND file_id = ?",
    )
      .bind(message.slice(0, 2048), now, uid, fileId)
      .run();
    try {
      await env.CHAT_FILES?.delete(storageKey);
      if (thumbnailKey) await env.CHAT_FILES?.delete(thumbnailKey);
    } catch {
      /* account residual sweep remains authoritative for both objects */
    }
    if (providerId) {
      try {
        await providerDelete(env, providerId);
      } catch {
        /* The failed D1 row and provider cleanup can be reconciled separately. */
      }
    }
    throw error instanceof ChatFileHttpError
      ? error
      : new ChatFileHttpError(503, "upload_unavailable", message);
  }
}

async function rollbackBatch(
  env: JobsEnv,
  uploaded: UploadedChatFile[],
): Promise<void> {
  for (const item of uploaded) {
    if (!item.newlyReady) continue;
    const row = item.row;
    let providerDeleted = !row.provider_file_id;
    if (row.provider_file_id) {
      try {
        await providerDelete(env, row.provider_file_id);
        providerDeleted = true;
      } catch {
        // Keep the provider id in D1 so a later residual reconciliation can
        // retry deletion; still remove local objects and mark the batch
        // failed, so the caller never observes a partial successful batch.
      }
    }
    try {
      await env.CHAT_FILES?.delete(row.storage_key);
      if (row.thumbnail_key) await env.CHAT_FILES?.delete(row.thumbnail_key);
    } catch {
      // The failed row and account residual sweep remain authoritative.
    }
    await env.APP_DB.prepare(
      "UPDATE cf_chat_files SET provider_file_id = ?, status = 'failed', last_error = ?, updated_at = ? WHERE uid = ? AND file_id = ? AND status = 'ready'",
    )
      .bind(
        providerDeleted ? null : row.provider_file_id,
        "batch upload rolled back",
        Math.floor(Date.now() / 1000),
        row.uid,
        row.file_id,
      )
      .run();
  }
}

export function registerChatFileRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (c: JobsContext) => Promise<AuthContext | null>,
): void {
  const upload = async (c: JobsContext, legacy: boolean) => {
    if (
      legacy &&
      c.env.LEGACY_CHAT_FILES_STAGING_ENABLED !== "true"
    )
      return c.json(
        {
          error: "legacy_route_disabled",
          detail:
            "Legacy chat-file compatibility is disabled until the downstream session contract is migrated.",
        },
        404,
      );
    const context = await requestContext(c);
    if (!context || !validUid(context.uid))
      return c.json({ error: "unauthorized" }, 401);
    try {
      const uploads = await parseUploads(c.req.raw);
      const results: Record<string, unknown>[] = [];
      const completed: UploadedChatFile[] = [];
      try {
        for (const upload of uploads) {
          const item = await uploadOne(c.env, context.uid, upload);
          completed.push(item);
          results.push(await responseWithThumbnail(c.env, item.row));
        }
      } catch (error) {
        await rollbackBatch(c.env, completed);
        throw error;
      }
      // FastAPI's legacy handlers return 200 for a successful list.  The
      // explicit /v1/cf staging API keeps its original 201 admission status.
      return c.json(results, legacy ? 200 : 201);
    } catch (error) {
      if (error instanceof ChatFileHttpError)
        return c.json(
          legacy
            ? { detail: error.message }
            : { error: error.code, message: error.message },
          error.status as 400,
        );
      return c.json(
        legacy
          ? { detail: "File upload is temporarily unavailable" }
          : { error: "upload_unavailable" },
        503,
      );
    }
  };

  app.post("/v1/cf/chat-files", (c) => upload(c, false));

  // These aliases are guarded by an explicit switch.  This lets staging
  // prove the same canonical handler and legacy 200/response shape without
  // silently changing the route inventory before downstream chat-session
  // reads and historical backfill are complete.
  app.post("/v1/files", (c) => upload(c, true));
  app.post("/v2/files", (c) => upload(c, true));

  app.get("/v1/cf/chat-files", async (c) => {
    const context = await requestContext(c);
    if (!context || !validUid(context.uid))
      return c.json({ error: "unauthorized" }, 401);
    const rows = await c.env.APP_DB.prepare(
      "SELECT * FROM cf_chat_files WHERE uid = ? AND status = 'ready' ORDER BY created_at DESC, file_id DESC LIMIT 100",
    )
      .bind(context.uid)
      .all<ChatFileRow>();
    return c.json(
      await Promise.all(
        (rows.results || []).map((row) =>
          responseWithThumbnail(c.env, row),
        ),
      ),
    );
  });

  app.delete("/v1/cf/chat-files/:fileId", async (c) => {
    const context = await requestContext(c);
    if (!context || !validUid(context.uid))
      return c.json({ error: "unauthorized" }, 401);
    const fileId = c.req.param("fileId");
    if (!/^[0-9a-f-]{36}$/i.test(fileId))
      return c.json({ error: "not_found" }, 404);
    const row = await c.env.APP_DB.prepare(
      "SELECT * FROM cf_chat_files WHERE uid = ? AND file_id = ? AND status = 'ready'",
    )
      .bind(context.uid, fileId)
      .first<ChatFileRow>();
    if (!row) return c.json({ error: "not_found" }, 404);
    try {
      if (row.provider_file_id)
        await providerDelete(c.env, row.provider_file_id);
      if (!c.env.CHAT_FILES)
        throw new ChatFileHttpError(
          503,
          "storage_unavailable",
          "chat file storage is not configured",
        );
      await c.env.CHAT_FILES.delete(row.storage_key);
      if (row.thumbnail_key) await c.env.CHAT_FILES.delete(row.thumbnail_key);
      await c.env.APP_DB.prepare(
        "DELETE FROM cf_chat_files WHERE uid = ? AND file_id = ? AND status = 'ready'",
      )
        .bind(context.uid, fileId)
        .run();
      return c.json({ status: "ok", id: fileId });
    } catch (error) {
      if (error instanceof ChatFileHttpError)
        return c.json(
          { error: error.code, message: error.message },
          error.status as 400,
        );
      return c.json({ error: "delete_unavailable" }, 503);
    }
  });

  // The source and thumbnail objects are private.  Legacy clients render the
  // returned `thumbnail` URL directly, so this endpoint accepts only a short
  // lived HMAC token and never accepts a caller-supplied R2 key.
  app.get("/v1/cf/chat-files/:fileId/thumbnail", async (c) => {
    const fileId = c.req.param("fileId");
    const uid = String(c.req.query("uid") || "");
    const expires = Number(c.req.query("exp"));
    const signature = String(c.req.query("sig") || "");
    const secret = String(c.env.CHAT_FILE_THUMBNAIL_SECRET || "").trim();
    const now = Math.floor(Date.now() / 1000);
    if (
      !validUid(uid) ||
      !/^[0-9a-f-]{36}$/i.test(fileId) ||
      !secret ||
      !Number.isInteger(expires) ||
      expires < now ||
      expires > now + THUMBNAIL_TTL_SECONDS + 5 ||
      !/^[A-Za-z0-9_-]{43}$/.test(signature)
    )
      return c.body(null, 404);
    const expected = await thumbnailSignature(secret, uid, fileId, expires);
    if (!constantTimeEqual(signature, expected)) return c.body(null, 404);
    const row = await c.env.APP_DB.prepare(
      "SELECT * FROM cf_chat_files WHERE uid = ? AND file_id = ? AND status = 'ready' AND thumbnail_status = 'ready'",
    )
      .bind(uid, fileId)
      .first<ChatFileRow>();
    if (!row?.thumbnail_key || !c.env.CHAT_FILES) return c.body(null, 404);
    const object = await c.env.CHAT_FILES.get(row.thumbnail_key);
    if (!object) return c.body(null, 404);
    return new Response(object.body, {
      headers: {
        "content-type": object.httpMetadata?.contentType || "image/jpeg",
        "cache-control": "private, max-age=300",
        etag: object.httpEtag,
      },
    });
  });
}
