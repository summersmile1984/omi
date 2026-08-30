import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import { recordFallback } from "../shared/fallback";
import type { JobsEnv } from "./env";

const AUTHORIZE_URL = "https://x.com/i/oauth2/authorize";
const TOKEN_URL = "https://api.x.com/2/oauth2/token";
const API_BASE = "https://api.x.com/2";
const DEFAULT_DEEP_LINK = "omi://x/callback";
const DEFAULT_SCOPES =
  "tweet.read users.read bookmark.read like.read offline.access";
const DEFAULT_MEMORY_MODEL = "@cf/meta/llama-3.2-3b-instruct";
const OAUTH_STATE_TTL_SECONDS = 600;
const SYNC_INTERVAL_SECONDS = 6 * 60 * 60;
const SYNC_LEASE_SECONDS = 15 * 60;
const MAX_PROVIDER_RESPONSE_BYTES = 2_000_000;
const MAX_D1_JSON_BIND_BYTES = 1_800_000;
const MAX_POST_TEXT_LENGTH = 50_000;
const MAX_TWEET_PAGES = 4;
const MAX_BOOKMARK_PAGES = 2;
const MAX_PENDING_EXTRACTION_POSTS = 200;
const MEMORY_BATCH_CHARS = 8_000;
const MAX_EXTRACTED_MEMORIES = 50;
const X_POST_KINDS = new Set(["tweet", "bookmark", "like"]);

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type XConnectorDependencies = Readonly<{
  fetchImpl?: FetchLike;
  now?: () => number;
}>;

type XConnectionRow = {
  uid: string;
  connected: number;
  access_token_enc: string | null;
  refresh_token_enc: string | null;
  token_expires_at: number | null;
  scope: string | null;
  handle: string | null;
  x_user_id: string | null;
  syncing: number;
  sync_token: string | null;
  sync_started_at: number | null;
  last_synced_at: number | null;
  last_sync_source: string | null;
  post_count: number;
  memory_count: number;
  created_at: number;
  updated_at: number;
};

type OAuthStateRow = {
  uid: string;
  verifier_enc: string;
  success_redirect_url: string;
  expires_at: number;
};

type XPost = {
  id: string;
  text: string;
  kind: "tweet" | "bookmark" | "like";
  lang: string | null;
  metrics_json: string;
  created_at: number;
};

type SyncResult = {
  success: boolean;
  source?: "oauth" | "rapidapi";
  new_posts: number;
  memories_created: number;
  error?: "not_connected" | "sync_in_progress" | "fetch_failed";
};

class ProviderResponseError extends Error {
  constructor(readonly status: number) {
    super(`X provider returned HTTP ${status}`);
  }
}

function nowSeconds(dependencies?: XConnectorDependencies) {
  return dependencies?.now?.() ?? Math.floor(Date.now() / 1_000);
}

function base64Url(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function base64(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function decodeBase64Url(value: string) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function randomToken(bytes: number) {
  return base64Url(crypto.getRandomValues(new Uint8Array(bytes)));
}

async function sha256Bytes(value: string) {
  return new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
  );
}

async function sha256Hex(value: string) {
  return Array.from(await sha256Bytes(value), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function encryptionSecret(env: JobsEnv) {
  const value = env.X_TOKEN_ENCRYPTION_SECRET;
  if (
    typeof value !== "string" ||
    new TextEncoder().encode(value).byteLength < 32
  ) {
    throw new Error("X token encryption is not configured");
  }
  return value;
}

async function credentialKey(env: JobsEnv) {
  return crypto.subtle.importKey(
    "raw",
    await sha256Bytes(encryptionSecret(env)),
    { name: "AES-GCM" },
    false,
    ["encrypt", "decrypt"],
  );
}

function credentialContext(uid: string, field: string) {
  return new TextEncoder().encode(`omi:x:v1\0${uid}\0${field}`);
}

async function encryptCredential(
  env: JobsEnv,
  uid: string,
  field: string,
  value: string,
) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    {
      name: "AES-GCM",
      iv,
      additionalData: credentialContext(uid, field),
    },
    await credentialKey(env),
    new TextEncoder().encode(value),
  );
  return `v1.${base64Url(iv)}.${base64Url(new Uint8Array(ciphertext))}`;
}

async function decryptCredential(
  env: JobsEnv,
  uid: string,
  field: string,
  value: string,
) {
  const parts = value.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") {
    throw new Error("invalid X credential envelope");
  }
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: decodeBase64Url(parts[1]),
      additionalData: credentialContext(uid, field),
    },
    await credentialKey(env),
    decodeBase64Url(parts[2]),
  );
  const decoded = new TextDecoder().decode(plaintext);
  if (!decoded) throw new Error("empty X credential");
  return decoded;
}

function basicAuthorization(clientId: string, clientSecret: string) {
  return `Basic ${base64(new TextEncoder().encode(`${clientId}:${clientSecret}`))}`;
}

function oauthConfiguration(env: JobsEnv) {
  const clientId = env.X_OAUTH_CLIENT_ID?.trim();
  const clientSecret = env.X_OAUTH_CLIENT_SECRET?.trim();
  const redirectUri = env.X_OAUTH_REDIRECT_URI?.trim();
  const scopes = env.X_OAUTH_SCOPES?.trim() || DEFAULT_SCOPES;
  if (!clientId || !clientSecret || !redirectUri || !scopes) return null;
  try {
    const parsed = new URL(redirectUri);
    if (parsed.protocol !== "https:") return null;
    encryptionSecret(env);
  } catch {
    return null;
  }
  return { clientId, clientSecret, redirectUri, scopes };
}

function successRedirect(value: string | undefined) {
  if (!value) return DEFAULT_DEEP_LINK;
  if (value.length > 1_024) return null;
  try {
    const parsed = new URL(value);
    const scheme = parsed.protocol.slice(0, -1);
    if (
      !/^omi(?:-[a-z0-9-]+)?$/.test(scheme) ||
      parsed.hostname !== "x" ||
      parsed.pathname !== "/callback" ||
      parsed.username ||
      parsed.password ||
      parsed.port ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }
    return parsed.toString();
  } catch {
    return null;
  }
}

function deepLink(
  deepLinkValue: string,
  key: "status" | "error",
  value: string,
) {
  const parsed = new URL(deepLinkValue);
  parsed.searchParams.set(key, value);
  return parsed.toString();
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function redirectHtml(target: string, ok: boolean, message: string) {
  const safeTarget = escapeHtml(target);
  const scriptTarget = JSON.stringify(target).replace(/</g, "\\u003c");
  return (
    `<!doctype html><html><head><meta charset="utf-8"><title>X · Omi</title>` +
    `<meta http-equiv="refresh" content="0;url=${safeTarget}">` +
    `<style>body{font-family:-apple-system,system-ui,sans-serif;background:#0b0b0f;color:#eaeaea;` +
    `display:flex;height:100vh;margin:0;align-items:center;justify-content:center;text-align:center}` +
    `.c{max-width:360px}.i{font-size:42px}</style></head><body><div class="c">` +
    `<div class="i">${ok ? "✓" : "⚠️"}</div><h2>${escapeHtml(message)}</h2>` +
    `<p>Returning to Omi…</p></div><script>setTimeout(function(){window.location.href=${scriptTarget};},150);` +
    `</script></body></html>`
  );
}

function htmlResponse(target: string, ok: boolean, message: string) {
  return new Response(redirectHtml(target, ok, message), {
    status: 200,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "content-security-policy":
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
      "referrer-policy": "no-referrer",
      "x-content-type-options": "nosniff",
    },
  });
}

async function providerJson(response: Response) {
  const declared = Number(response.headers.get("content-length") || 0);
  if (Number.isFinite(declared) && declared > MAX_PROVIDER_RESPONSE_BYTES) {
    throw new Error("X provider response too large");
  }
  const body = await response.arrayBuffer();
  if (body.byteLength > MAX_PROVIDER_RESPONSE_BYTES) {
    throw new Error("X provider response too large");
  }
  if (!response.ok) throw new ProviderResponseError(response.status);
  try {
    return JSON.parse(new TextDecoder().decode(body)) as unknown;
  } catch {
    throw new Error("X provider returned invalid JSON");
  }
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function boundedString(value: unknown, maximum: number) {
  return typeof value === "string" &&
    value.length > 0 &&
    value.length <= maximum
    ? value
    : null;
}

async function tokenRequest(
  fetchImpl: FetchLike,
  configuration: NonNullable<ReturnType<typeof oauthConfiguration>>,
  values: Record<string, string>,
) {
  const response = await fetchImpl(TOKEN_URL, {
    method: "POST",
    headers: {
      authorization: basicAuthorization(
        configuration.clientId,
        configuration.clientSecret,
      ),
      "content-type": "application/x-www-form-urlencoded",
      accept: "application/json",
    },
    body: new URLSearchParams(values).toString(),
    signal: AbortSignal.timeout(20_000),
  });
  const payload = objectValue(await providerJson(response));
  const accessToken = boundedString(payload?.access_token, 16_384);
  const refreshToken =
    payload?.refresh_token === undefined
      ? null
      : boundedString(payload.refresh_token, 16_384);
  const expiresIn = Number(payload?.expires_in ?? 7_200);
  if (
    !accessToken ||
    (payload?.refresh_token !== undefined && !refreshToken) ||
    !Number.isSafeInteger(expiresIn) ||
    expiresIn < 60 ||
    expiresIn > 365 * 24 * 60 * 60
  ) {
    throw new Error("X provider returned invalid tokens");
  }
  return {
    accessToken,
    refreshToken,
    expiresIn,
    scope:
      typeof payload?.scope === "string" && payload.scope.length <= 2_000
        ? payload.scope
        : configuration.scopes,
  };
}

async function apiGet(
  fetchImpl: FetchLike,
  accessToken: string,
  path: string,
  parameters: Record<string, string> = {},
) {
  if (!path.startsWith("/")) throw new Error("invalid X API path");
  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(parameters)) {
    url.searchParams.set(key, value);
  }
  return providerJson(
    await fetchImpl(url, {
      headers: {
        authorization: `Bearer ${accessToken}`,
        accept: "application/json",
      },
      signal: AbortSignal.timeout(20_000),
    }),
  );
}

async function fetchMe(fetchImpl: FetchLike, accessToken: string) {
  const payload = objectValue(
    await apiGet(fetchImpl, accessToken, "/users/me"),
  );
  const data = objectValue(payload?.data);
  return {
    id: boundedString(data?.id, 100),
    username: boundedString(data?.username, 100),
  };
}

function epoch(value: unknown) {
  if (typeof value !== "string" || value.length > 100) return null;
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) && milliseconds >= 0
    ? Math.floor(milliseconds / 1_000)
    : null;
}

function normalizePost(value: unknown, kind: XPost["kind"]): XPost | null {
  const post = objectValue(value);
  const id = boundedString(post?.id ?? post?.tweet_id, 100);
  const text = boundedString(post?.text, MAX_POST_TEXT_LENGTH);
  const createdAt = epoch(post?.created_at);
  if (!id || !text || createdAt === null) return null;
  const lang =
    post?.lang === undefined || post.lang === null
      ? null
      : boundedString(post.lang, 32);
  const metrics = objectValue(post?.public_metrics ?? post?.metrics) || {};
  const metricsJson = JSON.stringify(metrics);
  return {
    id,
    text,
    kind,
    lang,
    metrics_json:
      new TextEncoder().encode(metricsJson).byteLength <= 100_000
        ? metricsJson
        : "{}",
    created_at: createdAt,
  };
}

async function fetchPaged(
  fetchImpl: FetchLike,
  accessToken: string,
  path: string,
  kind: XPost["kind"],
  maximumPages: number,
  extra: Record<string, string> = {},
) {
  const posts: XPost[] = [];
  let paginationToken: string | null = null;
  for (let page = 0; page < maximumPages; page += 1) {
    const parameters: Record<string, string> = {
      max_results: "100",
      "tweet.fields": "created_at,lang,public_metrics",
      ...extra,
    };
    if (paginationToken) parameters.pagination_token = paginationToken;
    const payload = objectValue(
      await apiGet(fetchImpl, accessToken, path, parameters),
    );
    const data = Array.isArray(payload?.data) ? payload.data : [];
    for (const value of data) {
      const post = normalizePost(value, kind);
      if (post) posts.push(post);
    }
    const meta = objectValue(payload?.meta);
    paginationToken = boundedString(meta?.next_token, 1_024);
    if (!paginationToken) break;
  }
  return posts;
}

function rapidApiConfiguration(env: JobsEnv) {
  const host = env.RAPID_API_HOST?.trim().toLowerCase();
  const key = env.RAPID_API_KEY?.trim();
  if (!host || !key || !/^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/.test(host)) {
    return null;
  }
  return { host, key };
}

async function fetchRapidApiTimeline(
  env: JobsEnv,
  fetchImpl: FetchLike,
  handle: string,
) {
  const configuration = rapidApiConfiguration(env);
  if (!configuration) throw new Error("RapidAPI is not configured");
  const url = new URL(`https://${configuration.host}/timeline.php`);
  url.searchParams.set("screenname", handle);
  const payload = objectValue(
    await providerJson(
      await fetchImpl(url, {
        headers: {
          "x-rapidapi-key": configuration.key,
          "x-rapidapi-host": configuration.host,
          accept: "application/json",
        },
        signal: AbortSignal.timeout(20_000),
      }),
    ),
  );
  if (payload?.status === "error") throw new Error("RapidAPI timeline failed");
  const timeline = Array.isArray(payload?.timeline) ? payload.timeline : [];
  return timeline
    .map((value) => normalizePost(value, "tweet"))
    .filter((value): value is XPost => value !== null);
}

function jsonChunks(rows: Array<Record<string, unknown>>) {
  const chunks: string[] = [];
  let current: string[] = [];
  let size = 2;
  for (const row of rows) {
    const encoded = JSON.stringify(row);
    const encodedSize = new TextEncoder().encode(encoded).byteLength;
    if (encodedSize + 2 > MAX_D1_JSON_BIND_BYTES) {
      throw new Error("X row exceeds D1 bind limit");
    }
    const additional = encodedSize + (current.length ? 1 : 0);
    if (current.length && size + additional > MAX_D1_JSON_BIND_BYTES) {
      chunks.push(`[${current.join(",")}]`);
      current = [];
      size = 2;
    }
    current.push(encoded);
    size += encodedSize + (current.length > 1 ? 1 : 0);
  }
  if (current.length) chunks.push(`[${current.join(",")}]`);
  return chunks;
}

async function newestTweetId(env: JobsEnv, uid: string) {
  const row = await env.APP_DB.prepare(
    "SELECT id FROM cf_x_posts WHERE uid = ? AND kind = 'tweet' " +
      "ORDER BY length(id) DESC, id DESC LIMIT 1",
  )
    .bind(uid)
    .first<{ id?: unknown }>();
  return boundedString(row?.id, 100);
}

async function savePosts(
  env: JobsEnv,
  uid: string,
  syncToken: string,
  posts: XPost[],
  now: number,
) {
  const unique = [...new Map(posts.map((post) => [post.id, post])).values()];
  if (!unique.length) return 0;
  const idsJson = JSON.stringify(unique.map((post) => post.id));
  const existing = await env.APP_DB.prepare(
    "SELECT id FROM cf_x_posts WHERE uid = ? " +
      "AND id IN (SELECT CAST(value AS TEXT) FROM json_each(?))",
  )
    .bind(uid, idsJson)
    .all<{ id: string }>();
  const existingIds = new Set((existing.results || []).map((row) => row.id));
  const rows = unique.map((post) => ({
    uid,
    id: post.id,
    text: post.text,
    kind: post.kind,
    lang: post.lang,
    metrics_json: post.metrics_json,
    created_at: post.created_at,
    ingested_at: now,
    updated_at: now,
  }));
  const statements: D1PreparedStatement[] = [];
  for (const rowsJson of jsonChunks(rows)) {
    statements.push(
      env.APP_DB.prepare(
        "INSERT INTO cf_x_posts " +
          "(uid, id, text, kind, lang, metrics_json, created_at, ingested_at, updated_at) " +
          "SELECT json_extract(value, '$.uid'), json_extract(value, '$.id'), " +
          "json_extract(value, '$.text'), json_extract(value, '$.kind'), json_extract(value, '$.lang'), " +
          "json_extract(value, '$.metrics_json'), CAST(json_extract(value, '$.created_at') AS INTEGER), " +
          "CAST(json_extract(value, '$.ingested_at') AS INTEGER), " +
          "CAST(json_extract(value, '$.updated_at') AS INTEGER) FROM json_each(?) " +
          "WHERE EXISTS (SELECT 1 FROM cf_x_connections WHERE uid = ? AND connected = 1 AND sync_token = ?) " +
          "ON CONFLICT(uid, id) DO UPDATE SET text = excluded.text, kind = excluded.kind, lang = excluded.lang, " +
          "metrics_json = excluded.metrics_json, created_at = excluded.created_at, updated_at = excluded.updated_at",
      ).bind(rowsJson, uid, syncToken),
    );
    statements.push(
      env.APP_DB.prepare(
        "INSERT INTO cf_vector_projection_outbox " +
          "(uid, source_kind, source_id, desired_version, operation, attempts, next_attempt_at, last_error, " +
          "created_at, updated_at) " +
          "SELECT uid, 'x_post', id, updated_at, 'upsert', 0, ?, NULL, ?, ? FROM cf_x_posts " +
          "WHERE uid = ? AND id IN (SELECT CAST(json_extract(value, '$.id') AS TEXT) FROM json_each(?)) " +
          "AND EXISTS (SELECT 1 FROM cf_x_connections WHERE uid = ? AND connected = 1 AND sync_token = ?) " +
          "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET desired_version = excluded.desired_version, " +
          "operation = 'upsert', attempts = 0, next_attempt_at = excluded.next_attempt_at, last_error = NULL, " +
          "updated_at = excluded.updated_at " +
          "WHERE excluded.desired_version >= cf_vector_projection_outbox.desired_version",
      ).bind(now, now, now, uid, rowsJson, uid, syncToken),
    );
  }
  await env.APP_DB.batch(statements);
  return unique.filter((post) => !existingIds.has(post.id)).length;
}

async function pendingPosts(env: JobsEnv, uid: string) {
  const result = await env.APP_DB.prepare(
    "SELECT id, text, kind, created_at FROM cf_x_posts WHERE uid = ? " +
      "AND memory_extraction_status != 'completed' " +
      "ORDER BY COALESCE(ingested_at, created_at), id LIMIT ?",
  )
    .bind(uid, MAX_PENDING_EXTRACTION_POSTS)
    .all<{ id: string; text: string; kind: string; created_at: number }>();
  return result.results || [];
}

function extractionChunks(
  posts: Array<{ id: string; text: string; created_at: number }>,
) {
  const chunks: Array<{
    text: string;
    ids: string[];
  }> = [];
  let lines: string[] = [];
  let ids: string[] = [];
  let size = 0;
  for (const post of posts) {
    const line = `${post.text} (Posted: ${new Date(post.created_at * 1_000).toISOString()})`;
    if (lines.length && size + line.length > MEMORY_BATCH_CHARS) {
      chunks.push({ text: lines.join("\n"), ids });
      lines = [];
      ids = [];
      size = 0;
    }
    lines.push(line);
    ids.push(post.id);
    size += line.length;
  }
  if (lines.length) chunks.push({ text: lines.join("\n"), ids });
  return chunks;
}

function memorySchema() {
  return {
    type: "json_schema",
    json_schema: {
      name: "omi_x_memories",
      strict: true,
      schema: {
        type: "object",
        properties: {
          memories: { type: "array", items: { type: "string" } },
        },
        required: ["memories"],
        additionalProperties: false,
      },
    },
  };
}

function modelObject(value: unknown) {
  const mapping = objectValue(value);
  const response = mapping?.response;
  if (response && typeof response === "object" && !Array.isArray(response)) {
    return response as Record<string, unknown>;
  }
  if (typeof response !== "string") return mapping;
  const fenced = response.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
  try {
    return objectValue(JSON.parse((fenced || response).trim()));
  } catch {
    return null;
  }
}

async function extractMemories(env: JobsEnv, text: string) {
  let result: unknown;
  try {
    result = await env.AI.run(
      env.WORKERS_AI_X_MEMORY_MODEL || DEFAULT_MEMORY_MODEL,
      {
        messages: [
          {
            role: "system",
            content:
              "Extract durable, user-specific facts from X posts. Exclude commands, transient details, guesses, " +
              "and facts about unrelated third parties. Return only the requested JSON.",
          },
          {
            role: "user",
            content:
              "Return an object with a memories array of concise strings, at most 50 items.\n\n" +
              text,
          },
        ],
        response_format: memorySchema(),
        max_tokens: 1_024,
        temperature: 0,
      },
    );
  } catch {
    return null;
  }
  const values = modelObject(result)?.memories;
  if (!Array.isArray(values)) return null;
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const value of values.slice(0, MAX_EXTRACTED_MEMORIES)) {
    const content = typeof value === "string" ? value.trim() : "";
    if (
      !content ||
      content.length > MAX_POST_TEXT_LENGTH ||
      seen.has(content)
    ) {
      continue;
    }
    seen.add(content);
    unique.push(content);
  }
  return unique;
}

async function storeExtractedMemories(
  env: JobsEnv,
  uid: string,
  syncToken: string,
  postIds: string[],
  memories: string[],
  now: number,
) {
  const sourceId = `x:${(await sha256Hex(postIds.join("|"))).slice(0, 24)}`;
  const rows = memories.map((content) => {
    const id = crypto.randomUUID().replace(/-/g, "");
    return {
      uid,
      id,
      content,
      tags_json: JSON.stringify(["x"]),
      qualifiers_json: JSON.stringify({
        integration: {
          kind: "integration_text",
          text_source: "twitter_tweets",
          source_id: sourceId,
          post_ids: postIds,
        },
      }),
      valid_at: now,
      created_at: now,
      updated_at: now,
    };
  });
  const statements: D1PreparedStatement[] = [];
  if (rows.length) {
    const rowsJson = JSON.stringify(rows);
    const idsJson = JSON.stringify(rows.map((row) => row.id));
    statements.push(
      env.APP_DB.prepare(
        "INSERT INTO cf_memories " +
          "(uid, id, content, category, visibility, tags_json, qualifiers_json, manually_added, app_id, " +
          "memory_tier, valid_at, created_at, updated_at) " +
          "SELECT json_extract(value, '$.uid'), json_extract(value, '$.id'), json_extract(value, '$.content'), " +
          "'system', 'private', json_extract(value, '$.tags_json'), json_extract(value, '$.qualifiers_json'), " +
          "0, 'x', 'short_term', CAST(json_extract(value, '$.valid_at') AS INTEGER), " +
          "CAST(json_extract(value, '$.created_at') AS INTEGER), " +
          "CAST(json_extract(value, '$.updated_at') AS INTEGER) FROM json_each(?) " +
          "WHERE EXISTS (SELECT 1 FROM cf_x_connections WHERE uid = ? AND connected = 1 AND sync_token = ?)",
      ).bind(rowsJson, uid, syncToken),
      env.APP_DB.prepare(
        "INSERT INTO cf_usage_sources " +
          "(uid, source_kind, source_id, occurred_at, transcription_seconds, words_transcribed, " +
          "insights_gained, memories_created, updated_at) " +
          "SELECT uid, 'memory', id, created_at, 0, 0, 0, 1, updated_at FROM cf_memories " +
          "WHERE uid = ? AND id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) " +
          "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET occurred_at = excluded.occurred_at, " +
          "memories_created = 1, updated_at = excluded.updated_at",
      ).bind(uid, idsJson),
      env.APP_DB.prepare(
        "INSERT INTO cf_vector_projection_outbox " +
          "(uid, source_kind, source_id, desired_version, operation, attempts, next_attempt_at, last_error, " +
          "created_at, updated_at) SELECT uid, 'memory', id, updated_at, 'upsert', 0, ?, NULL, ?, ? " +
          "FROM cf_memories WHERE uid = ? AND id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) " +
          "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET desired_version = excluded.desired_version, " +
          "operation = 'upsert', attempts = 0, next_attempt_at = excluded.next_attempt_at, last_error = NULL, " +
          "updated_at = excluded.updated_at " +
          "WHERE excluded.desired_version >= cf_vector_projection_outbox.desired_version",
      ).bind(now, now, now, uid, idsJson),
    );
  }
  statements.push(
    env.APP_DB.prepare(
      "UPDATE cf_x_posts SET memory_extraction_status = 'completed', memory_extracted_at = ?, updated_at = ? " +
        "WHERE uid = ? AND id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) " +
        "AND EXISTS (SELECT 1 FROM cf_x_connections WHERE uid = ? AND connected = 1 AND sync_token = ?)",
    ).bind(now, now, uid, JSON.stringify(postIds), uid, syncToken),
  );
  const results = await env.APP_DB.batch(statements);
  const acknowledgement = results[results.length - 1]?.meta?.changes;
  if (acknowledgement !== postIds.length) throw new Error("X sync lease lost");
  return memories.length;
}

async function extractPendingMemories(
  env: JobsEnv,
  uid: string,
  syncToken: string,
  now: number,
) {
  let created = 0;
  for (const chunk of extractionChunks(await pendingPosts(env, uid))) {
    const memories = await extractMemories(env, chunk.text);
    if (memories === null) {
      recordFallback({
        component: "other",
        from: "x_memory_extraction",
        to: "deferred_extraction",
        reason: "dependency_unavailable",
        outcome: "degraded",
      });
      continue;
    }
    created += await storeExtractedMemories(
      env,
      uid,
      syncToken,
      chunk.ids,
      memories,
      now,
    );
  }
  return created;
}

async function connection(env: JobsEnv, uid: string) {
  return env.APP_DB.prepare(
    "SELECT uid, connected, access_token_enc, refresh_token_enc, token_expires_at, scope, handle, x_user_id, " +
      "syncing, sync_token, sync_started_at, last_synced_at, last_sync_source, post_count, memory_count, " +
      "created_at, updated_at FROM cf_x_connections WHERE uid = ?",
  )
    .bind(uid)
    .first<XConnectionRow>();
}

async function claimSync(
  env: JobsEnv,
  uid: string,
  now: number,
  preclaimedToken?: string,
) {
  if (preclaimedToken) {
    const row = await connection(env, uid);
    return row?.connected === 1 &&
      row.syncing === 1 &&
      row.sync_token === preclaimedToken
      ? row
      : null;
  }
  const syncToken = crypto.randomUUID();
  return env.APP_DB.prepare(
    "UPDATE cf_x_connections SET syncing = 1, sync_token = ?, sync_started_at = ?, updated_at = ? " +
      "WHERE uid = ? AND connected = 1 AND (syncing = 0 OR sync_started_at IS NULL OR sync_started_at <= ?) " +
      "RETURNING uid, connected, access_token_enc, refresh_token_enc, token_expires_at, scope, handle, x_user_id, " +
      "syncing, sync_token, sync_started_at, last_synced_at, last_sync_source, post_count, memory_count, " +
      "created_at, updated_at",
  )
    .bind(syncToken, now, now, uid, now - SYNC_LEASE_SECONDS)
    .first<XConnectionRow>();
}

async function clearSync(
  env: JobsEnv,
  uid: string,
  syncToken: string,
  now: number,
) {
  await env.APP_DB.prepare(
    "UPDATE cf_x_connections SET syncing = 0, sync_token = NULL, sync_started_at = NULL, updated_at = ? " +
      "WHERE uid = ? AND sync_token = ?",
  )
    .bind(now, uid, syncToken)
    .run();
}

async function validAccessToken(
  env: JobsEnv,
  row: XConnectionRow,
  fetchImpl: FetchLike,
  now: number,
) {
  try {
    if (!row.access_token_enc) return null;
    const accessToken = await decryptCredential(
      env,
      row.uid,
      "access-token",
      row.access_token_enc,
    );
    if ((row.token_expires_at || 0) > now + 60) return accessToken;
    if (!row.refresh_token_enc) return accessToken;
    const configuration = oauthConfiguration(env);
    if (!configuration) return null;
    const refreshToken = await decryptCredential(
      env,
      row.uid,
      "refresh-token",
      row.refresh_token_enc,
    );
    const tokens = await tokenRequest(fetchImpl, configuration, {
      grant_type: "refresh_token",
      refresh_token: refreshToken,
      client_id: configuration.clientId,
    });
    const accessTokenEnc = await encryptCredential(
      env,
      row.uid,
      "access-token",
      tokens.accessToken,
    );
    const refreshTokenEnc = tokens.refreshToken
      ? await encryptCredential(
          env,
          row.uid,
          "refresh-token",
          tokens.refreshToken,
        )
      : row.refresh_token_enc;
    await env.APP_DB.prepare(
      "UPDATE cf_x_connections SET access_token_enc = ?, refresh_token_enc = ?, token_expires_at = ?, " +
        "scope = ?, updated_at = ? WHERE uid = ? AND connected = 1 AND sync_token = ?",
    )
      .bind(
        accessTokenEnc,
        refreshTokenEnc,
        now + tokens.expiresIn,
        tokens.scope,
        now,
        row.uid,
        row.sync_token,
      )
      .run();
    return tokens.accessToken;
  } catch {
    return null;
  }
}

async function persistIdentity(
  env: JobsEnv,
  uid: string,
  syncToken: string,
  id: string,
  username: string | null,
  now: number,
) {
  await env.APP_DB.prepare(
    "UPDATE cf_x_connections SET x_user_id = ?, handle = COALESCE(?, handle), updated_at = ? " +
      "WHERE uid = ? AND connected = 1 AND sync_token = ?",
  )
    .bind(id, username, now, uid, syncToken)
    .run();
}

async function finishSync(
  env: JobsEnv,
  uid: string,
  syncToken: string,
  source: "oauth" | "rapidapi",
  now: number,
) {
  const counts = await env.APP_DB.prepare(
    "SELECT (SELECT COUNT(*) FROM cf_x_posts WHERE uid = ?) AS post_count, " +
      "(SELECT COUNT(*) FROM cf_memories WHERE uid = ? AND app_id = 'x' " +
      "AND deleted_at IS NULL AND invalid_at IS NULL) AS memory_count",
  )
    .bind(uid, uid)
    .first<{ post_count?: unknown; memory_count?: unknown }>();
  const postCount = Number(counts?.post_count || 0);
  const memoryCount = Number(counts?.memory_count || 0);
  const updated = await env.APP_DB.prepare(
    "UPDATE cf_x_connections SET syncing = 0, sync_token = NULL, sync_started_at = NULL, last_synced_at = ?, " +
      "last_sync_source = ?, post_count = ?, memory_count = ?, updated_at = ? " +
      "WHERE uid = ? AND connected = 1 AND sync_token = ?",
  )
    .bind(now, source, postCount, memoryCount, now, uid, syncToken)
    .run();
  if (updated.meta?.changes !== 1) throw new Error("X sync lease lost");
}

export async function syncXForUser(
  env: JobsEnv,
  uid: string,
  dependencies: XConnectorDependencies = {},
  preclaimedToken?: string,
): Promise<SyncResult> {
  const fetchImpl = dependencies.fetchImpl || fetch;
  const now = nowSeconds(dependencies);
  const row = await claimSync(env, uid, now, preclaimedToken);
  if (!row || !row.sync_token) {
    const existing = await connection(env, uid);
    return existing?.connected === 1
      ? {
          success: false,
          error: "sync_in_progress",
          new_posts: 0,
          memories_created: 0,
        }
      : {
          success: false,
          error: "not_connected",
          new_posts: 0,
          memories_created: 0,
        };
  }
  const syncToken = row.sync_token;
  try {
    let posts: XPost[] = [];
    let source: "oauth" | "rapidapi" | null = null;
    const accessToken = await validAccessToken(env, row, fetchImpl, now);
    let handle = row.handle;
    if (accessToken) {
      try {
        let xUserId = row.x_user_id;
        if (!xUserId) {
          const me = await fetchMe(fetchImpl, accessToken);
          xUserId = me.id;
          handle = me.username || handle;
          if (xUserId) {
            await persistIdentity(
              env,
              uid,
              syncToken,
              xUserId,
              me.username,
              now,
            );
          }
        }
        if (xUserId) {
          const sinceId = await newestTweetId(env, uid);
          const tweets = await fetchPaged(
            fetchImpl,
            accessToken,
            `/users/${encodeURIComponent(xUserId)}/tweets`,
            "tweet",
            MAX_TWEET_PAGES,
            {
              exclude: "retweets",
              ...(sinceId ? { since_id: sinceId } : {}),
            },
          );
          let bookmarks: XPost[] = [];
          try {
            bookmarks = await fetchPaged(
              fetchImpl,
              accessToken,
              `/users/${encodeURIComponent(xUserId)}/bookmarks`,
              "bookmark",
              MAX_BOOKMARK_PAGES,
            );
          } catch {
            bookmarks = [];
          }
          posts = [...tweets, ...bookmarks];
          source = "oauth";
        }
      } catch {
        source = null;
      }
    }
    if (!source) {
      if (!handle) {
        await clearSync(env, uid, syncToken, now);
        return {
          success: false,
          error: "not_connected",
          new_posts: 0,
          memories_created: 0,
        };
      }
      try {
        posts = await fetchRapidApiTimeline(env, fetchImpl, handle);
        source = "rapidapi";
        recordFallback({
          component: "other",
          from: "x_oauth_api",
          to: "rapidapi_timeline",
          reason: "dependency_unavailable",
          outcome: "recovered",
        });
      } catch {
        recordFallback({
          component: "other",
          from: "x_oauth_api",
          to: "rapidapi_timeline",
          reason: "dependency_unavailable",
          outcome: "exhausted",
        });
        await clearSync(env, uid, syncToken, now);
        return {
          success: false,
          error: "fetch_failed",
          new_posts: 0,
          memories_created: 0,
        };
      }
    }
    const newPosts = await savePosts(env, uid, syncToken, posts, now);
    const memoriesCreated = await extractPendingMemories(
      env,
      uid,
      syncToken,
      now,
    );
    await finishSync(env, uid, syncToken, source, now);
    return {
      success: true,
      source,
      new_posts: newPosts,
      memories_created: memoriesCreated,
    };
  } catch {
    try {
      await clearSync(env, uid, syncToken, now);
    } catch {
      // The mutation fence may already own this principal.
    }
    return {
      success: false,
      error: "fetch_failed",
      new_posts: 0,
      memories_created: 0,
    };
  }
}

async function saveOAuthConnection(
  env: JobsEnv,
  uid: string,
  tokens: Awaited<ReturnType<typeof tokenRequest>>,
  handle: string | null,
  xUserId: string | null,
  syncToken: string,
  now: number,
) {
  const accessTokenEnc = await encryptCredential(
    env,
    uid,
    "access-token",
    tokens.accessToken,
  );
  const refreshTokenEnc = tokens.refreshToken
    ? await encryptCredential(env, uid, "refresh-token", tokens.refreshToken)
    : null;
  await env.APP_DB.prepare(
    "INSERT INTO cf_x_connections " +
      "(uid, connected, access_token_enc, refresh_token_enc, token_expires_at, scope, handle, x_user_id, " +
      "syncing, sync_token, sync_started_at, created_at, updated_at) " +
      "VALUES (?, 1, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?) " +
      "ON CONFLICT(uid) DO UPDATE SET connected = 1, access_token_enc = excluded.access_token_enc, " +
      "refresh_token_enc = COALESCE(excluded.refresh_token_enc, cf_x_connections.refresh_token_enc), " +
      "token_expires_at = excluded.token_expires_at, " +
      "scope = excluded.scope, handle = COALESCE(excluded.handle, cf_x_connections.handle), " +
      "x_user_id = COALESCE(excluded.x_user_id, cf_x_connections.x_user_id), syncing = 1, " +
      "sync_token = excluded.sync_token, sync_started_at = excluded.sync_started_at, " +
      "updated_at = excluded.updated_at",
  )
    .bind(
      uid,
      accessTokenEnc,
      refreshTokenEnc,
      now + tokens.expiresIn,
      tokens.scope,
      handle,
      xUserId,
      syncToken,
      now,
      now,
      now,
    )
    .run();
}

async function oauthUrl(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: XConnectorDependencies,
) {
  const configuration = oauthConfiguration(c.env);
  if (!configuration) {
    return c.json({ success: false, error: "x_oauth_not_configured" });
  }
  const redirect = successRedirect(c.req.query("success_redirect_url"));
  if (!redirect) {
    return c.json({ success: false, error: "invalid_success_redirect_url" });
  }
  const verifier = randomToken(64);
  const challenge = base64Url(await sha256Bytes(verifier));
  const state = randomToken(32);
  const stateHash = await sha256Hex(state);
  const now = nowSeconds(dependencies);
  const verifierEnc = await encryptCredential(
    c.env,
    context.uid,
    `oauth-verifier:${stateHash}`,
    verifier,
  );
  await c.env.APP_DB.batch([
    c.env.APP_DB.prepare(
      "DELETE FROM cf_x_oauth_states WHERE expires_at <= ?",
    ).bind(now),
    c.env.APP_DB.prepare(
      "INSERT INTO cf_x_oauth_states " +
        "(state_hash, uid, verifier_enc, success_redirect_url, expires_at, created_at) " +
        "VALUES (?, ?, ?, ?, ?, ?)",
    ).bind(
      stateHash,
      context.uid,
      verifierEnc,
      redirect,
      now + OAUTH_STATE_TTL_SECONDS,
      now,
    ),
  ]);
  const url = new URL(AUTHORIZE_URL);
  for (const [key, value] of Object.entries({
    response_type: "code",
    client_id: configuration.clientId,
    redirect_uri: configuration.redirectUri,
    scope: configuration.scopes,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  })) {
    url.searchParams.set(key, value);
  }
  c.header("cache-control", "no-store");
  return c.json({ success: true, auth_url: url.toString() });
}

async function consumeOAuthState(env: JobsEnv, state: string) {
  if (!state || state.length > 256) return null;
  const stateHash = await sha256Hex(state);
  const row = await env.APP_DB.prepare(
    "DELETE FROM cf_x_oauth_states WHERE state_hash = ? " +
      "RETURNING uid, verifier_enc, success_redirect_url, expires_at",
  )
    .bind(stateHash)
    .first<OAuthStateRow>();
  return row ? { ...row, stateHash } : null;
}

async function oauthCallback(
  c: JobsContext,
  dependencies: XConnectorDependencies,
) {
  const error = c.req.query("error");
  const code = c.req.query("code");
  const state = c.req.query("state");
  if (error || !code || !state || code.length > 8_192) {
    return htmlResponse(
      deepLink(DEFAULT_DEEP_LINK, "error", error || "missing_code"),
      false,
      "Connection cancelled",
    );
  }
  const consumed = await consumeOAuthState(c.env, state);
  const now = nowSeconds(dependencies);
  if (!consumed || consumed.expires_at <= now) {
    return htmlResponse(
      deepLink(DEFAULT_DEEP_LINK, "error", "invalid_state"),
      false,
      "Link expired",
    );
  }
  const configuration = oauthConfiguration(c.env);
  const redirect =
    successRedirect(consumed.success_redirect_url) || DEFAULT_DEEP_LINK;
  if (!configuration) {
    return htmlResponse(
      deepLink(redirect, "error", "exchange_failed"),
      false,
      "Connection failed",
    );
  }
  const fetchImpl = dependencies.fetchImpl || fetch;
  try {
    const verifier = await decryptCredential(
      c.env,
      consumed.uid,
      `oauth-verifier:${consumed.stateHash}`,
      consumed.verifier_enc,
    );
    const tokens = await tokenRequest(fetchImpl, configuration, {
      grant_type: "authorization_code",
      code,
      redirect_uri: configuration.redirectUri,
      code_verifier: verifier,
      client_id: configuration.clientId,
    });
    let identity = {
      id: null as string | null,
      username: null as string | null,
    };
    try {
      identity = await fetchMe(fetchImpl, tokens.accessToken);
    } catch {
      // Identity hydration is optional; the first sync retries it.
    }
    const syncToken = crypto.randomUUID();
    await saveOAuthConnection(
      c.env,
      consumed.uid,
      tokens,
      identity.username,
      identity.id,
      syncToken,
      now,
    );
    c.executionCtx.waitUntil(
      syncXForUser(c.env, consumed.uid, dependencies, syncToken).then(
        () => undefined,
        () => undefined,
      ),
    );
    return htmlResponse(
      deepLink(redirect, "status", "success"),
      true,
      "X connected",
    );
  } catch {
    return htmlResponse(
      deepLink(redirect, "error", "exchange_failed"),
      false,
      "Connection failed",
    );
  }
}

function iso(value: unknown) {
  const seconds = Number(value);
  return Number.isSafeInteger(seconds) && seconds >= 0
    ? new Date(seconds * 1_000).toISOString()
    : null;
}

function parsedJson(value: unknown) {
  if (typeof value !== "string" || value.length > 100_000) return {};
  try {
    return objectValue(JSON.parse(value)) || {};
  } catch {
    return {};
  }
}

export async function reconcileXConnections(
  env: JobsEnv,
  now: number,
  dependencies: XConnectorDependencies = {},
) {
  await env.APP_DB.prepare(
    "DELETE FROM cf_x_oauth_states WHERE expires_at <= ?",
  )
    .bind(now)
    .run();
  if (!oauthConfiguration(env)) return 0;
  const result = await env.APP_DB.prepare(
    "SELECT uid FROM cf_x_connections WHERE connected = 1 " +
      "AND (syncing = 0 OR sync_started_at IS NULL OR sync_started_at <= ?) " +
      "AND (last_synced_at IS NULL OR last_synced_at <= ?) ORDER BY COALESCE(last_synced_at, 0), uid LIMIT 5",
  )
    .bind(now - SYNC_LEASE_SECONDS, now - SYNC_INTERVAL_SECONDS)
    .all<{ uid: string }>();
  let synced = 0;
  for (const row of result.results || []) {
    try {
      const response = await syncXForUser(env, row.uid, {
        ...dependencies,
        now: () => now,
      });
      if (response.success) synced += 1;
    } catch {
      // One provider account cannot fail the shared maintenance pass.
    }
  }
  return synced;
}

export function registerXConnectorRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
  dependencies: XConnectorDependencies = {},
) {
  app.get("/v1/x/oauth/callback", (c) => oauthCallback(c, dependencies));

  app.get("/v1/x/oauth-url", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await oauthUrl(c, context, dependencies);
    } catch {
      return c.json({ success: false, error: "internal_error" });
    }
  });

  app.get("/v1/x/connection-status", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const row = await connection(c.env, context.uid);
      if (!row || row.connected !== 1) {
        return c.json({
          success: true,
          connected: false,
          handle: null,
          post_count: 0,
          memory_count: 0,
          syncing: false,
          last_synced_at: null,
          last_sync_source: null,
        });
      }
      return c.json({
        success: true,
        connected: true,
        handle: row.handle,
        post_count: Number(row.post_count || 0),
        memory_count: Number(row.memory_count || 0),
        syncing: row.syncing === 1,
        last_synced_at: iso(row.last_synced_at),
        last_sync_source: row.last_sync_source,
      });
    } catch {
      return c.json({ error: "x_connection_unavailable" }, 503);
    }
  });

  app.get("/v1/x/posts", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const kind = c.req.query("kind");
    if (kind !== undefined && !X_POST_KINDS.has(kind)) {
      return c.json(
        { detail: "kind must be one of: tweet, bookmark, like" },
        400,
      );
    }
    const limit = Number(c.req.query("limit") || "100");
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      return c.json({ detail: "invalid limit" }, 422);
    }
    try {
      const clauses = ["uid = ?"];
      const values: unknown[] = [context.uid];
      if (kind) {
        clauses.push("kind = ?");
        values.push(kind);
      }
      const rows = await c.env.APP_DB.prepare(
        "SELECT id, text, kind, lang, metrics_json, created_at, ingested_at, updated_at, " +
          "memory_extraction_status, memory_extracted_at FROM cf_x_posts WHERE " +
          clauses.join(" AND ") +
          " ORDER BY created_at DESC, id DESC LIMIT ?",
      )
        .bind(...values, limit)
        .all<Record<string, unknown>>();
      return c.json({
        posts: (rows.results || []).map((row) => ({
          id: String(row.id || ""),
          text: String(row.text || ""),
          kind: String(row.kind || "tweet"),
          lang: row.lang ?? null,
          metrics: parsedJson(row.metrics_json),
          created_at: iso(row.created_at),
          ingested_at: iso(row.ingested_at),
          updated_at: iso(row.updated_at),
          memory_extraction_status: String(
            row.memory_extraction_status || "pending",
          ),
          memory_extracted_at: iso(row.memory_extracted_at),
        })),
      });
    } catch {
      return c.json({ error: "x_posts_unavailable" }, 503);
    }
  });

  app.post("/v1/x/sync", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    return c.json(await syncXForUser(c.env, context.uid, dependencies));
  });

  app.post("/v1/x/disconnect", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const now = nowSeconds(dependencies);
      await c.env.APP_DB.prepare(
        "INSERT INTO cf_x_connections (uid, connected, syncing, post_count, memory_count, created_at, updated_at) " +
          "VALUES (?, 0, 0, 0, 0, ?, ?) ON CONFLICT(uid) DO UPDATE SET connected = 0, " +
          "access_token_enc = NULL, refresh_token_enc = NULL, token_expires_at = NULL, syncing = 0, " +
          "sync_token = NULL, sync_started_at = NULL, updated_at = excluded.updated_at",
      )
        .bind(context.uid, now, now)
        .run();
      return c.json({ success: true });
    } catch {
      return c.json({ error: "x_disconnect_unavailable" }, 503);
    }
  });
}
