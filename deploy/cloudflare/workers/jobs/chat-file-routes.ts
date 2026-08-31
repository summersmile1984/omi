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
): Promise<string> {
  const key = String(env.OPENAI_API_KEY || "").trim();
  if (!key)
    throw new ChatFileHttpError(
      503,
      "provider_unavailable",
      "file provider is not configured",
    );
  const form = new FormData();
  form.set("purpose", "assistants");
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

async function uploadOne(
  env: JobsEnv,
  uid: string,
  file: FileUpload,
): Promise<Record<string, unknown>> {
  const name = safeName(file.name || "upload");
  const mime = mimeType(file);
  // Thumbnail generation is part of the legacy public response.  Do not
  // publish an image without its thumbnail until a Worker-safe decoder exists.
  if (mime.startsWith("image/"))
    throw new ChatFileHttpError(
      400,
      "thumbnail_unavailable",
      "image chat files are not enabled on the Cloudflare boundary yet",
    );
  const { bytes, checksum } = await hashFile(file);
  const fingerprint = bytesToHex(
    sha256(new TextEncoder().encode(`${uid}\0${name}\0${mime}\0${checksum}`)),
  );
  const existing = await existingByFingerprint(env, uid, fingerprint);
  if (existing?.status === "ready") return response(existing);
  if (existing?.status === "staging")
    throw new ChatFileHttpError(
      409,
      "upload_in_progress",
      "file upload is already in progress",
    );
  const fileId = existing?.file_id || crypto.randomUUID();
  const storageKey = existing?.storage_key || privateKey(uid, fileId);
  const now = Math.floor(Date.now() / 1000);
  if (!storageKey.startsWith(`${uid}/`))
    throw new ChatFileHttpError(
      503,
      "metadata_unavailable",
      "file metadata is invalid",
    );
  const metadata = await env.APP_DB.prepare(
    "INSERT INTO cf_chat_files (uid, file_id, request_fingerprint, provider, provider_file_id, name, mime_type, size, checksum_sha256, storage_key, status, thumbnail_status, created_at, updated_at) VALUES (?, ?, ?, 'openai', NULL, ?, ?, ?, ?, ?, 'staging', 'unsupported', ?, ?) " +
      "ON CONFLICT(uid, request_fingerprint) DO UPDATE SET provider_file_id = NULL, status = 'staging', thumbnail_status = 'not_applicable', last_error = NULL, updated_at = excluded.updated_at",
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
    providerId = await providerUpload(env, bytes, name, mime);
    await env.APP_DB.prepare(
      "UPDATE cf_chat_files SET provider_file_id = ?, status = 'ready', updated_at = ?, last_error = NULL WHERE uid = ? AND file_id = ? AND status = 'staging'",
    )
      .bind(providerId, now, uid, fileId)
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
    return response(ready);
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
    } catch {
      /* account residual sweep remains authoritative */
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

export function registerChatFileRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: (c: JobsContext) => Promise<AuthContext | null>,
): void {
  app.post("/v1/cf/chat-files", async (c) => {
    const context = await requestContext(c);
    if (!context || !validUid(context.uid))
      return c.json({ error: "unauthorized" }, 401);
    try {
      const uploads = await parseUploads(c.req.raw);
      const results: Record<string, unknown>[] = [];
      for (const upload of uploads)
        results.push(await uploadOne(c.env, context.uid, upload));
      return c.json(results, 201);
    } catch (error) {
      if (error instanceof ChatFileHttpError)
        return c.json(
          { error: error.code, message: error.message },
          error.status as 400,
        );
      return c.json({ error: "upload_unavailable" }, 503);
    }
  });

  app.get("/v1/cf/chat-files", async (c) => {
    const context = await requestContext(c);
    if (!context || !validUid(context.uid))
      return c.json({ error: "unauthorized" }, 401);
    const rows = await c.env.APP_DB.prepare(
      "SELECT * FROM cf_chat_files WHERE uid = ? AND status = 'ready' ORDER BY created_at DESC, file_id DESC LIMIT 100",
    )
      .bind(context.uid)
      .all<ChatFileRow>();
    return c.json((rows.results || []).map(response));
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
}
