import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

const MAX_NAME_LENGTH = 256;
const MAX_BODY_BYTES = 4_096;
const RAW_DEVELOPER_KEY = /omi_dev_[0-9a-f]{32}/;
const DEVELOPER_APP_ID = "developer_api";
const DEVELOPER_SCOPES = Object.freeze([
  "conversations:read",
  "conversations:write",
  "memories:read",
  "memories:write",
  "action_items:read",
  "action_items:write",
  "goals:read",
  "goals:write",
] as const);
const READ_ONLY_SCOPES = Object.freeze([
  "conversations:read",
  "memories:read",
  "action_items:read",
  "goals:read",
] as const);
const INVALID_SCOPES_DETAIL = `Invalid scopes. Available: [${DEVELOPER_SCOPES.map((scope) => `'${scope}'`).join(", ")}]`;

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;

function randomHex(bytes: number) {
  return Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
}

async function sha256Hex(value: string) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function isoTimestamp(value: unknown) {
  const seconds = Number(value);
  if (!Number.isSafeInteger(seconds) || seconds < 0) {
    throw new Error("invalid Developer API key timestamp");
  }
  return new Date(seconds * 1_000).toISOString();
}

function keyId(value: unknown) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length > 0 &&
    normalized.length <= 64 &&
    !normalized.includes("/") &&
    !normalized.includes("\\")
    ? normalized
    : null;
}

function requestedScopes(value: unknown): string[] | null {
  if (value === undefined || value === null) return [...READ_ONLY_SCOPES];
  if (
    !Array.isArray(value) ||
    value.length > DEVELOPER_SCOPES.length ||
    value.some(
      (scope) =>
        typeof scope !== "string" ||
        !DEVELOPER_SCOPES.includes(scope as (typeof DEVELOPER_SCOPES)[number]),
    )
  ) {
    return null;
  }
  return [...value];
}

function metadata(row: {
  key_id: string;
  name: string;
  key_prefix: string;
  scopes_json: string;
  created_at: number;
  last_used_at: number | null;
}) {
  const scopes = JSON.parse(row.scopes_json) as unknown;
  if (
    !Array.isArray(scopes) ||
    scopes.length > DEVELOPER_SCOPES.length ||
    scopes.some(
      (scope) =>
        typeof scope !== "string" ||
        !DEVELOPER_SCOPES.includes(scope as (typeof DEVELOPER_SCOPES)[number]),
    )
  ) {
    throw new Error("invalid Developer API key scopes");
  }
  return {
    id: row.key_id,
    name: row.name,
    key_prefix: row.key_prefix,
    created_at: isoTimestamp(row.created_at),
    last_used_at:
      row.last_used_at === null ? null : isoTimestamp(row.last_used_at),
    scopes,
  };
}

async function requestBody(c: JobsContext): Promise<unknown | Response> {
  const declaredLength = Number(c.req.header("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
    return c.json({ detail: "Invalid request body" }, 422);
  }
  try {
    const raw = await c.req.text();
    if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) {
      return c.json({ detail: "Invalid request body" }, 422);
    }
    return JSON.parse(raw) as unknown;
  } catch {
    return c.json({ detail: "Invalid request body" }, 422);
  }
}

async function createDeveloperApiKey(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  const body = await requestBody(c);
  if (body instanceof Response) return body;
  const object =
    body && typeof body === "object" && !Array.isArray(body)
      ? (body as Record<string, unknown>)
      : null;
  const name = typeof object?.name === "string" ? object.name.trim() : "";
  if (!name) return c.json({ detail: "Key name cannot be empty" }, 422);
  if (name.length > MAX_NAME_LENGTH) {
    return c.json({ detail: "Invalid Developer API key name" }, 422);
  }
  if (RAW_DEVELOPER_KEY.test(name)) {
    return c.json(
      { detail: "API key name must not contain a raw API key" },
      422,
    );
  }
  const scopes = requestedScopes(object?.scopes);
  if (!scopes) return c.json({ detail: INVALID_SCOPES_DETAIL }, 400);

  try {
    const secret = randomHex(16);
    const rawKey = `omi_dev_${secret}`;
    const keyHash = await sha256Hex(secret);
    const id = crypto.randomUUID();
    const prefix = `omi_dev_${secret.slice(0, 4)}...${secret.slice(-4)}`;
    const now = Math.floor(Date.now() / 1_000);
    await c.env.APP_DB.prepare(
      `INSERT INTO cf_developer_api_keys
         (uid, key_id, name, key_hash, key_prefix, app_id, scopes_json,
          created_at, last_used_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)`,
    )
      .bind(
        context.uid,
        id,
        name,
        keyHash,
        prefix,
        DEVELOPER_APP_ID,
        JSON.stringify(scopes),
        now,
      )
      .run();
    c.header("cache-control", "no-store");
    return c.json({
      ...metadata({
        key_id: id,
        name,
        key_prefix: prefix,
        scopes_json: JSON.stringify(scopes),
        created_at: now,
        last_used_at: null,
      }),
      key: rawKey,
    });
  } catch {
    return c.json({ error: "developer_api_keys_unavailable" }, 503);
  }
}

async function listDeveloperApiKeys(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  try {
    const result = await c.env.APP_DB.prepare(
      `SELECT key_id, name, key_prefix, scopes_json, created_at, last_used_at
       FROM cf_developer_api_keys
       WHERE uid = ?
       ORDER BY created_at DESC, key_id ASC
       LIMIT 500`,
    )
      .bind(context.uid)
      .all<{
        key_id: string;
        name: string;
        key_prefix: string;
        scopes_json: string;
        created_at: number;
        last_used_at: number | null;
      }>();
    c.header("cache-control", "no-store");
    return c.json((result.results || []).map(metadata));
  } catch {
    return c.json({ error: "developer_api_keys_unavailable" }, 503);
  }
}

async function deleteDeveloperApiKey(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  const id = keyId(c.req.param("keyId"));
  if (!id) return c.body(null, 204);
  try {
    await c.env.APP_DB.prepare(
      "DELETE FROM cf_developer_api_keys WHERE uid = ? AND key_id = ?",
    )
      .bind(context.uid, id)
      .run();
    return c.body(null, 204);
  } catch {
    return c.json({ error: "developer_api_keys_unavailable" }, 503);
  }
}

export function registerDeveloperApiKeyRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  app.post("/v1/dev/keys", (c) => createDeveloperApiKey(c, requestContext));
  app.get("/v1/dev/keys", (c) => listDeveloperApiKeys(c, requestContext));
  app.delete("/v1/dev/keys/:keyId", (c) =>
    deleteDeveloperApiKey(c, requestContext),
  );
}
