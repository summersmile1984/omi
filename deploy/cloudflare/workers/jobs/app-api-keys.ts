import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

const MAX_IDENTIFIER_LENGTH = 256;

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;

function identifier(value: unknown, maximum = MAX_IDENTIFIER_LENGTH) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length > 0 &&
    normalized.length <= maximum &&
    !normalized.includes("/") &&
    !normalized.includes("\\")
    ? normalized
    : null;
}

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

function createdAt(value: unknown) {
  const seconds = Number(value);
  if (!Number.isSafeInteger(seconds) || seconds < 0)
    throw new Error("invalid app API key timestamp");
  return new Date(seconds * 1_000).toISOString();
}

async function ownerAuthorization(
  c: JobsContext,
  requestContext: RequestContext,
  appId: string,
): Promise<SignedAuthContext | Response> {
  const context = await requestContext(c);
  if (!context) return c.json({ error: "unauthorized" }, 401);
  const row = await c.env.APP_DB.prepare(
    "SELECT owner_uid FROM cf_app_catalog WHERE id = ? LIMIT 1",
  )
    .bind(appId)
    .first<{ owner_uid?: unknown }>();
  if (!row) return c.json({ detail: "App not found" }, 404);
  if (row.owner_uid !== context.uid) {
    return c.json(
      { detail: "You are not authorized to manage API keys for this app" },
      403,
    );
  }
  return context;
}

async function createAppApiKey(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const appId = identifier(c.req.param("appId"));
  if (!appId) return c.json({ detail: "App not found" }, 404);
  try {
    const authorization = await ownerAuthorization(c, requestContext, appId);
    if (authorization instanceof Response) return authorization;
    const rawKey = randomHex(16);
    const keyHash = await sha256Hex(rawKey);
    const keyId = crypto.randomUUID();
    const label = `sk_${rawKey.slice(0, 4)}...${rawKey.slice(-4)}`;
    const now = Math.floor(Date.now() / 1_000);
    await c.env.APP_DB.prepare(
      "INSERT INTO cf_app_api_keys (app_id, key_id, key_hash, label, created_at) VALUES (?, ?, ?, ?, ?)",
    )
      .bind(appId, keyId, keyHash, label, now)
      .run();
    c.header("cache-control", "no-store");
    return c.json({
      id: keyId,
      secret: `sk_${rawKey}`,
      label,
      created_at: createdAt(now),
    });
  } catch {
    return c.json({ error: "app_api_keys_unavailable" }, 503);
  }
}

async function listAppApiKeys(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const appId = identifier(c.req.param("appId"));
  if (!appId) return c.json({ detail: "App not found" }, 404);
  try {
    const authorization = await ownerAuthorization(c, requestContext, appId);
    if (authorization instanceof Response) return authorization;
    const result = await c.env.APP_DB.prepare(
      "SELECT key_id, label, created_at FROM cf_app_api_keys WHERE app_id = ? " +
        "ORDER BY created_at DESC, key_id DESC LIMIT 500",
    )
      .bind(appId)
      .all<{ key_id: string; label: string; created_at: number }>();
    c.header("cache-control", "no-store");
    return c.json(
      (result.results || []).map((row) => ({
        id: row.key_id,
        label: row.label,
        created_at: createdAt(row.created_at),
      })),
    );
  } catch {
    return c.json({ error: "app_api_keys_unavailable" }, 503);
  }
}

async function deleteAppApiKey(
  c: JobsContext,
  requestContext: RequestContext,
) {
  const appId = identifier(c.req.param("appId"));
  const keyId = identifier(c.req.param("keyId"), 64);
  if (!appId || !keyId) return c.json({ detail: "App not found" }, 404);
  try {
    const authorization = await ownerAuthorization(c, requestContext, appId);
    if (authorization instanceof Response) return authorization;
    await c.env.APP_DB.prepare(
      "DELETE FROM cf_app_api_keys WHERE app_id = ? AND key_id = ?",
    )
      .bind(appId, keyId)
      .run();
    return c.json({ status: "ok", message: "API key deleted" });
  } catch {
    return c.json({ error: "app_api_keys_unavailable" }, 503);
  }
}

export function registerAppApiKeyRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
) {
  app.post("/v1/apps/:appId/keys", (c) =>
    createAppApiKey(c, requestContext),
  );
  app.get("/v1/apps/:appId/keys", (c) => listAppApiKeys(c, requestContext));
  app.delete("/v1/apps/:appId/keys/:keyId", (c) =>
    deleteAppApiKey(c, requestContext),
  );
}
