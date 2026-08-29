import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

const MAX_NAME_LENGTH = 256;
const MAX_BODY_BYTES = 4_096;
const RAW_MCP_KEY = /omi_mcp_[0-9a-f]{32}/;
const MCP_APP_ID = "mcp-api";
const MCP_FULL_ACCESS_SCOPES = Object.freeze([
  "action_items.read",
  "action_items.write",
  "chat.read",
  "conversations.read",
  "goals.read",
  "memories.read",
  "memories.write",
  "people.read",
  "screen_activity.read",
] as const);

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
  if (!Number.isSafeInteger(seconds) || seconds < 0)
    throw new Error("invalid MCP API key timestamp");
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

function metadata(row: {
  key_id: string;
  name: string;
  key_prefix: string;
  app_id: string;
  scopes_json: string;
  created_at: number;
  last_used_at: number | null;
}) {
  const scopes = JSON.parse(row.scopes_json) as unknown;
  if (
    !Array.isArray(scopes) ||
    scopes.length !== MCP_FULL_ACCESS_SCOPES.length ||
    scopes.some((scope, index) => scope !== MCP_FULL_ACCESS_SCOPES[index])
  ) {
    throw new Error("invalid MCP API key scopes");
  }
  return {
    id: row.key_id,
    name: row.name,
    key_prefix: row.key_prefix,
    created_at: isoTimestamp(row.created_at),
    last_used_at:
      row.last_used_at === null ? null : isoTimestamp(row.last_used_at),
    app_id: row.app_id,
    scopes,
  };
}

async function createMcpApiKey(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  const declaredLength = Number(c.req.header("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
    return c.json({ detail: "Invalid request body" }, 422);
  }
  let body: unknown;
  try {
    const raw = await c.req.text();
    if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) {
      return c.json({ detail: "Invalid request body" }, 422);
    }
    body = JSON.parse(raw);
  } catch {
    return c.json({ detail: "Invalid request body" }, 422);
  }
  const rawName =
    body && typeof body === "object" && !Array.isArray(body)
      ? (body as Record<string, unknown>).name
      : null;
  const name = typeof rawName === "string" ? rawName.trim() : "";
  if (!name) return c.json({ detail: "Key name cannot be empty" }, 422);
  if (name.length > MAX_NAME_LENGTH || RAW_MCP_KEY.test(name)) {
    return c.json({ detail: "Invalid MCP API key name" }, 422);
  }

  try {
    const secret = randomHex(16);
    const rawKey = `omi_mcp_${secret}`;
    const keyHash = await sha256Hex(secret);
    const id = crypto.randomUUID();
    const prefix = `omi_mcp_${secret.slice(0, 4)}...${secret.slice(-4)}`;
    const now = Math.floor(Date.now() / 1_000);
    await c.env.APP_DB.prepare(
      `INSERT INTO cf_mcp_api_keys
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
        MCP_APP_ID,
        JSON.stringify(MCP_FULL_ACCESS_SCOPES),
        now,
      )
      .run();
    c.header("cache-control", "no-store");
    return c.json({
      ...metadata({
        key_id: id,
        name,
        key_prefix: prefix,
        app_id: MCP_APP_ID,
        scopes_json: JSON.stringify(MCP_FULL_ACCESS_SCOPES),
        created_at: now,
        last_used_at: null,
      }),
      key: rawKey,
    });
  } catch {
    return c.json({ error: "mcp_api_keys_unavailable" }, 503);
  }
}

async function listMcpApiKeys(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  try {
    const result = await c.env.APP_DB.prepare(
      `SELECT key_id, name, key_prefix, app_id, scopes_json, created_at,
              last_used_at
       FROM cf_mcp_api_keys
       WHERE uid = ?
       ORDER BY created_at DESC, key_id DESC
       LIMIT 500`,
    )
      .bind(context.uid)
      .all<{
        key_id: string;
        name: string;
        key_prefix: string;
        app_id: string;
        scopes_json: string;
        created_at: number;
        last_used_at: number | null;
      }>();
    c.header("cache-control", "no-store");
    return c.json((result.results || []).map(metadata));
  } catch {
    return c.json({ error: "mcp_api_keys_unavailable" }, 503);
  }
}

async function deleteMcpApiKey(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  const id = keyId(c.req.param("keyId"));
  if (!id) return c.body(null, 204);
  try {
    await c.env.APP_DB.prepare(
      "DELETE FROM cf_mcp_api_keys WHERE uid = ? AND key_id = ?",
    )
      .bind(context.uid, id)
      .run();
    return c.body(null, 204);
  } catch {
    return c.json({ error: "mcp_api_keys_unavailable" }, 503);
  }
}

export function registerMcpApiKeyRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  app.post("/v1/mcp/keys", (c) => createMcpApiKey(c, requestContext));
  app.get("/v1/mcp/keys", (c) => listMcpApiKeys(c, requestContext));
  app.delete("/v1/mcp/keys/:keyId", (c) =>
    deleteMcpApiKey(c, requestContext),
  );
}
