/**
 * Namespaced Cloudflare app-consent OAuth staging seam.
 *
 * The legacy /v1/oauth/* contract verifies Firebase ID tokens and mutates
 * Redis/Firestore. This module uses the Edge-issued Better Auth context and
 * App D1 as its only identity/app authority. It intentionally has no alias
 * from the legacy routes; the explicit gate must be enabled before it serves
 * any HTML or token response.
 */

import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type JsonObject = Record<string, unknown>;
type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type ExternalAppOauthDependencies = Readonly<{
  fetchImpl?: FetchLike;
  now?: () => number;
}>;

class ExternalAppOauthError extends Error {
  constructor(
    readonly status: 400 | 401 | 403 | 404 | 409 | 413 | 422 | 502 | 503,
    readonly code: string,
  ) {
    super(code);
    this.name = "ExternalAppOauthError";
  }
}

const MAX_FORM_BYTES = 16_000;
const MAX_APP_ID_BYTES = 256;
const MAX_APP_PAYLOAD_BYTES = 500_000;
const MAX_STATE_BYTES = 512;
const MAX_CSRF_BYTES = 512;
const MAX_POLICY_BYTES = 65_536;
const MAX_SETUP_RESPONSE_BYTES = 64_000;
const TRANSACTION_TTL_SECONDS = 600;
const COOKIE_NAME = "omi_cf_oauth_csrf";
const OPAQUE_SECRET_RE = /^[A-Za-z0-9._~-]{43,128}$/;
const PRINTABLE_STATE_RE = /^[\x21-\x7e]{1,512}$/;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;

function nowSeconds(dependencies: ExternalAppOauthDependencies): number {
  const now = dependencies.now?.() ?? Math.floor(Date.now() / 1_000);
  return Number.isSafeInteger(now) && now > 0 ? now : 0;
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/g, "");
}

function randomSecret(): string {
  return base64Url(crypto.getRandomValues(new Uint8Array(32)));
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

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let difference = leftBytes.length ^ rightBytes.length;
  const length = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return difference === 0;
}

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

function flag(value: unknown): boolean {
  return value === true || value === 1 ||
    (typeof value === "string" && ["1", "true"].includes(value.trim().toLowerCase()));
}

function validAppId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    utf8Bytes(value) <= MAX_APP_ID_BYTES &&
    !value.includes("/") &&
    !value.includes("\\") &&
    !CONTROL_CHARACTERS.test(value)
  );
}

function publicHttps(value: unknown): value is string {
  if (typeof value !== "string" || !value || utf8Bytes(value) > 2_048)
    return false;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.hash ||
      host === "localhost" ||
      host.endsWith(".localhost") ||
      host.endsWith(".local")
    )
      return false;
    const ipv4 = host.split(".");
    if (ipv4.length === 4 && ipv4.every((part) => /^\d{1,3}$/.test(part))) {
      const octets = ipv4.map(Number);
      if (octets.some((part) => part > 255)) return false;
      const [first, second] = octets;
      return !(
        first === 0 ||
        first === 10 ||
        first === 127 ||
        (first === 100 && second >= 64 && second <= 127) ||
        (first === 169 && second === 254) ||
        (first === 172 && second >= 16 && second <= 31) ||
        (first === 192 && second === 168) ||
        (first === 198 && second >= 18 && second <= 19) ||
        first >= 224
      );
    }
    const mappedIpv4 = host.match(/^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i)?.[1];
    if (mappedIpv4) {
      const octets = mappedIpv4.split(".").map(Number);
      if (octets.some((part) => part > 255)) return false;
      const [first, second] = octets;
      return !(
        first === 0 ||
        first === 10 ||
        first === 127 ||
        (first === 100 && second >= 64 && second <= 127) ||
        (first === 169 && second === 254) ||
        (first === 172 && second >= 16 && second <= 31) ||
        (first === 192 && second === 168) ||
        (first === 198 && second >= 18 && second <= 19) ||
        first >= 224
      );
    }
    if (
      host === "::1" ||
      host === "0:0:0:0:0:0:0:1" ||
      host.startsWith("::ffff:") ||
      host.startsWith("fc") ||
      host.startsWith("fd") ||
      host.startsWith("fe80:")
    )
      return false;
    return true;
  } catch {
    return false;
  }
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
      character
    ] || character,
  );
}

function errorResponse(c: JobsContext, error: unknown): Response {
  if (error instanceof ExternalAppOauthError)
    return c.json({ error: error.code }, error.status);
  return c.json({ error: "external_app_oauth_unavailable" }, 503);
}

async function readBoundedText(
  body: ReadableStream<Uint8Array> | null,
  limit: number,
): Promise<string> {
  if (!body) return "";
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      total += next.value.byteLength;
      if (total > limit) {
        await reader.cancel();
        throw new ExternalAppOauthError(413, "request_too_large");
      }
      chunks.push(next.value);
    }
  } catch (error) {
    if (error instanceof ExternalAppOauthError) throw error;
    throw new ExternalAppOauthError(503, "oauth_request_unavailable");
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new ExternalAppOauthError(422, "invalid_request");
  }
}

async function requestForm(c: JobsContext): Promise<URLSearchParams> {
  const contentLength = Number(c.req.header("content-length") || "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_FORM_BYTES)
    throw new ExternalAppOauthError(413, "request_too_large");
  const raw = await readBoundedText(c.req.raw.body, MAX_FORM_BYTES);
  const contentType = c.req.header("content-type")?.toLowerCase() || "";
  if (!contentType.includes("application/x-www-form-urlencoded"))
    throw new ExternalAppOauthError(422, "invalid_request");
  return new URLSearchParams(raw);
}

function cookieValue(request: Request): string | null {
  const cookie = request.headers.get("cookie") || "";
  for (const part of cookie.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0) continue;
    const name = part.slice(0, separator).trim();
    if (name === COOKIE_NAME) return part.slice(separator + 1).trim() || null;
  }
  return null;
}

type AppAdmission = {
  id: string;
  ownerUid: string | null;
  approved: boolean;
  disabled: boolean;
  updatedAt: number;
  tester: boolean;
  payload: JsonObject;
  appHomeUrl: string;
  setupCompletedUrl: string | null;
  privateApp: boolean;
  paid: boolean;
};

async function loadApp(
  env: JobsEnv,
  appId: string,
  uid: string,
): Promise<AppAdmission> {
  const row = await env.APP_DB.prepare(
    `SELECT a.id, a.owner_uid, a.approved, a.disabled, a.updated_at, a.data_json,
            CASE WHEN t.uid IS NULL THEN 0 ELSE 1 END AS tester
       FROM cf_app_catalog a
       LEFT JOIN cf_app_testers t ON t.uid = ?
      WHERE a.id = ? LIMIT 1`,
  )
    .bind(uid, appId)
    .first<{
      id?: string;
      owner_uid?: string | null;
      approved?: number;
      disabled?: number;
      updated_at?: number;
      data_json?: string;
      tester?: number;
    }>();
  if (!row?.id || typeof row.data_json !== "string")
    throw new ExternalAppOauthError(404, "app_not_found");
  if (Number(row.disabled) === 1)
    throw new ExternalAppOauthError(400, "app_unavailable");
  if (row.data_json.length > MAX_APP_PAYLOAD_BYTES)
    throw new ExternalAppOauthError(503, "app_catalog_unavailable");
  let payload: JsonObject;
  try {
    payload = objectValue(JSON.parse(row.data_json)) || {};
  } catch {
    throw new ExternalAppOauthError(503, "app_catalog_unavailable");
  }
  const external = objectValue(payload.external_integration);
  const appHomeUrl = external?.app_home_url;
  if (!external || !publicHttps(appHomeUrl))
    throw new ExternalAppOauthError(400, "external_integration_missing");
  const setupUrl = external.setup_completed_url;
  if (setupUrl !== undefined && setupUrl !== null && !publicHttps(setupUrl))
    throw new ExternalAppOauthError(400, "external_setup_target_unsafe");
  const privateApp = flag(payload.private);
  const paid = flag(payload.is_paid);
  const ownerUid = row.owner_uid === null || row.owner_uid === undefined
    ? null
    : String(row.owner_uid);
  const owner = ownerUid === uid;
  const tester = Number(row.tester) === 1;
  if (!owner && !tester && (!flag(row.approved) || privateApp))
    throw new ExternalAppOauthError(404, "app_not_found");
  const updatedAt = Number(row.updated_at);
  if (!Number.isSafeInteger(updatedAt) || updatedAt < 0)
    throw new ExternalAppOauthError(503, "app_catalog_unavailable");
  return {
    id: appId,
    ownerUid,
    approved: flag(row.approved),
    disabled: false,
    updatedAt,
    tester,
    payload,
    appHomeUrl,
    setupCompletedUrl: setupUrl == null ? null : String(setupUrl),
    privateApp,
    paid,
  };
}

function policyFor(app: AppAdmission): JsonObject {
  return {
    app_id: app.id,
    app_home_url: app.appHomeUrl,
    setup_completed_url: app.setupCompletedUrl,
    private: app.privateApp,
    paid: app.paid,
    owner_uid: app.ownerUid,
    catalog_revision: app.updatedAt,
  };
}

function permissionSummary(app: AppAdmission): string[] {
  const capabilities = app.payload.capabilities;
  if (!Array.isArray(capabilities)) return ["Access your basic Omi profile information."];
  const permissions: string[] = [];
  if (capabilities.includes("chat")) permissions.push("Engage in chat conversations with Omi.");
  if (capabilities.includes("memories")) permissions.push("Access and manage your conversations.");
  if (capabilities.includes("external_integration")) permissions.push("Run the app's configured Omi integration.");
  return permissions.length ? permissions : ["Access your basic Omi profile information."];
}

async function authorize(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: ExternalAppOauthDependencies,
): Promise<Response> {
  if (c.env.EXTERNAL_APP_OAUTH_STAGING_ENABLED !== "true")
    throw new ExternalAppOauthError(404, "not_found");
  const appId = c.req.query("app_id") || "";
  if (!validAppId(appId)) throw new ExternalAppOauthError(422, "invalid_app_id");
  const clientState = c.req.query("state") || null;
  if (clientState !== null &&
      (utf8Bytes(clientState) > MAX_STATE_BYTES || !PRINTABLE_STATE_RE.test(clientState)))
    throw new ExternalAppOauthError(422, "invalid_state");
  const app = await loadApp(c.env, appId, context.uid);
  const csrfSecret = randomSecret();
  const transactionState = randomSecret();
  const now = nowSeconds(dependencies);
  if (!now) throw new ExternalAppOauthError(503, "clock_unavailable");
  const policyJson = JSON.stringify(policyFor(app));
  if (utf8Bytes(policyJson) > MAX_POLICY_BYTES)
    throw new ExternalAppOauthError(503, "app_catalog_unavailable");
  await c.env.APP_DB.prepare(
    `INSERT INTO cf_external_app_oauth_transactions
       (transaction_id, app_id, uid, state_hash, csrf_hash, client_state,
        redirect_url, app_catalog_revision, app_policy_json, setup_target_hash,
        status, expires_at, created_at, consumed_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL)`,
  )
    .bind(
      crypto.randomUUID(),
      app.id,
      context.uid,
      await sha256Hex(transactionState),
      await sha256Hex(csrfSecret),
      clientState,
      app.appHomeUrl,
      app.updatedAt,
      policyJson,
      app.setupCompletedUrl ? await sha256Hex(app.setupCompletedUrl) : null,
      now + TRANSACTION_TTL_SECONDS,
      now,
    )
    .run();
  const permissions = permissionSummary(app)
    .map((permission) => `<li>${escapeHtml(permission)}</li>`)
    .join("");
  const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline';form-action 'self'"><title>Authorize ${escapeHtml(String(app.payload.name || app.id))}</title></head><body><h1>Authorize ${escapeHtml(String(app.payload.name || app.id))}</h1><ul>${permissions}</ul><form method="post" action="/v2/cf/oauth/token"><input type="hidden" name="app_id" value="${escapeHtml(app.id)}"><input type="hidden" name="state" value="${escapeHtml(transactionState)}"><input type="hidden" name="csrf_token" value="${escapeHtml(csrfSecret)}"><button type="submit">Authorize</button></form></body></html>`;
  return new Response(html, {
    status: 200,
    headers: {
      "content-type": "text/html; charset=UTF-8",
      "cache-control": "no-store",
      "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
      "set-cookie": `${COOKIE_NAME}=${csrfSecret}; Max-Age=${TRANSACTION_TTL_SECONDS}; Path=/v2/cf/oauth; HttpOnly; Secure; SameSite=Strict`,
    },
  });
}

async function setupCompleted(
  dependencies: ExternalAppOauthDependencies,
  setupUrl: string,
  uid: string,
): Promise<boolean> {
  if (!publicHttps(setupUrl))
    throw new ExternalAppOauthError(400, "external_setup_target_unsafe");
  const target = new URL(setupUrl);
  target.searchParams.set("uid", uid);
  let response: Response;
  try {
    response = await (dependencies.fetchImpl || fetch)(target, {
      method: "GET",
      headers: { accept: "application/json" },
      redirect: "error",
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    throw new ExternalAppOauthError(503, "external_setup_unavailable");
  }
  if (!response.ok)
    throw new ExternalAppOauthError(503, "external_setup_unavailable");
  const contentLength = Number(response.headers.get("content-length") || "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_SETUP_RESPONSE_BYTES)
    throw new ExternalAppOauthError(503, "external_setup_invalid");
  const raw = await readBoundedText(response.body, MAX_SETUP_RESPONSE_BYTES);
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new ExternalAppOauthError(503, "external_setup_invalid");
  }
  return objectValue(payload)?.is_setup_completed === true;
}

async function isPaid(env: JobsEnv, uid: string, appId: string, now: number): Promise<boolean> {
  const row = await env.APP_DB.prepare(
    "SELECT status, current_period_end FROM cf_app_subscriptions WHERE uid = ? AND app_id = ? LIMIT 1",
  )
    .bind(uid, appId)
    .first<{ status?: string; current_period_end?: number | null }>();
  return (
    (row?.status === "active" || row?.status === "trialing") &&
    typeof row.current_period_end === "number" &&
    row.current_period_end > now
  );
}

async function ensureInstalled(
  env: JobsEnv,
  app: AppAdmission,
  uid: string,
  now: number,
  dependencies: ExternalAppOauthDependencies,
): Promise<void> {
  const existing = await env.APP_DB.prepare(
    "SELECT uid FROM cf_user_enabled_apps WHERE uid = ? AND app_id = ? LIMIT 1",
  )
    .bind(uid, app.id)
    .first<{ uid?: string }>();
  if (existing?.uid === uid) return;
  if (app.paid && !(await isPaid(env, uid, app.id, now)))
    throw new ExternalAppOauthError(403, "external_app_not_entitled");
  if (app.setupCompletedUrl && !(await setupCompleted(dependencies, app.setupCompletedUrl, uid)))
    throw new ExternalAppOauthError(400, "external_setup_incomplete");
  const owner = app.ownerUid === uid;
  const canInstall = owner || app.tester || (app.approved && !app.privateApp);
  if (!canInstall) throw new ExternalAppOauthError(403, "external_app_not_authorized");
  const inserted = await env.APP_DB.prepare(
    `INSERT OR IGNORE INTO cf_user_enabled_apps (uid, app_id, created_at)
       SELECT ?, ?, ?
        WHERE EXISTS (
          SELECT 1 FROM cf_app_catalog a
           WHERE a.id = ? AND a.updated_at = ? AND a.disabled = 0
             AND (
               a.owner_uid = ? OR
               (a.approved = 1 AND COALESCE(json_extract(a.data_json, '$.private'), 0) NOT IN (1, 'true')) OR
               EXISTS (SELECT 1 FROM cf_app_testers t WHERE t.uid = ?)
             )
        )
          AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents i WHERE i.uid = ?)
          AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones t WHERE t.uid = ?)`,
  )
    .bind(uid, app.id, now, app.id, app.updatedAt, uid, uid, uid, uid)
    .run();
  const changes = Number(inserted.meta?.changes || 0);
  if (changes !== 1) {
    const raced = await env.APP_DB.prepare(
      "SELECT uid FROM cf_user_enabled_apps WHERE uid = ? AND app_id = ? LIMIT 1",
    )
      .bind(uid, app.id)
      .first<{ uid?: string }>();
    if (raced?.uid === uid) return;
    throw new ExternalAppOauthError(409, "app_changed_or_deleting");
  }
  if (!app.privateApp && app.approved && !owner && !app.tester) {
    const counter = await env.APP_DB.prepare(
      "UPDATE cf_app_catalog SET installs = MAX(0, installs + 1), updated_at = ? WHERE id = ? AND updated_at = ? AND disabled = 0",
    )
      .bind(now, app.id, app.updatedAt)
      .run();
    if (Number(counter.meta?.changes || 0) !== 1)
      throw new ExternalAppOauthError(409, "app_changed_or_deleting");
  }
}

async function token(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: ExternalAppOauthDependencies,
): Promise<Response> {
  if (c.env.EXTERNAL_APP_OAUTH_STAGING_ENABLED !== "true")
    throw new ExternalAppOauthError(404, "not_found");
  const form = await requestForm(c);
  const allowed = new Set(["app_id", "state", "csrf_token"]);
  for (const key of form.keys()) {
    if (!allowed.has(key) || form.getAll(key).length !== 1)
      throw new ExternalAppOauthError(422, "invalid_request");
  }
  const appId = form.get("app_id") || "";
  const state = form.get("state") || "";
  const csrfToken = form.get("csrf_token") || "";
  const cookie = cookieValue(c.req.raw);
  if (
    !validAppId(appId) ||
    !OPAQUE_SECRET_RE.test(state) ||
    !OPAQUE_SECRET_RE.test(csrfToken) ||
    !cookie ||
    !OPAQUE_SECRET_RE.test(cookie) ||
    utf8Bytes(csrfToken) > MAX_CSRF_BYTES ||
    !constantTimeEqual(csrfToken, cookie)
  )
    throw new ExternalAppOauthError(403, "csrf_invalid");
  const now = nowSeconds(dependencies);
  if (!now) throw new ExternalAppOauthError(503, "clock_unavailable");
  const stateHash = await sha256Hex(state);
  const csrfHash = await sha256Hex(csrfToken);
  const consumed = await c.env.APP_DB.prepare(
    `UPDATE cf_external_app_oauth_transactions
        SET status = 'consumed', consumed_at = ?
      WHERE app_id = ? AND uid = ? AND state_hash = ? AND csrf_hash = ?
        AND status = 'pending' AND expires_at > ?
        AND NOT EXISTS (
          SELECT 1 FROM cf_account_deletion_intents i
           WHERE i.uid = cf_external_app_oauth_transactions.uid
        )
        AND NOT EXISTS (
          SELECT 1 FROM cf_account_deletion_tombstones t
           WHERE t.uid = cf_external_app_oauth_transactions.uid
        )
        AND EXISTS (
          SELECT 1 FROM cf_app_catalog a
           WHERE a.id = cf_external_app_oauth_transactions.app_id
             AND a.updated_at = cf_external_app_oauth_transactions.app_catalog_revision
             AND a.disabled = 0
        )
      RETURNING transaction_id, app_id, uid, client_state, redirect_url,
                app_catalog_revision, app_policy_json`,
  )
    .bind(now, appId, context.uid, stateHash, csrfHash, now)
    .first<{
      transaction_id?: string;
      app_id?: string;
      uid?: string;
      client_state?: string | null;
      redirect_url?: string;
      app_catalog_revision?: number;
      app_policy_json?: string;
    }>();
  if (!consumed?.transaction_id || consumed.uid !== context.uid)
    throw new ExternalAppOauthError(400, "oauth_request_invalid");
  const app = await loadApp(c.env, appId, context.uid);
  if (
    app.updatedAt !== Number(consumed.app_catalog_revision) ||
    app.appHomeUrl !== consumed.redirect_url
  )
    throw new ExternalAppOauthError(409, "app_changed_or_deleting");
  try {
    const policy = objectValue(JSON.parse(consumed.app_policy_json || ""));
    if (!policy || policy.app_id !== app.id || policy.catalog_revision !== app.updatedAt)
      throw new ExternalAppOauthError(409, "app_changed_or_deleting");
    await ensureInstalled(c.env, app, context.uid, now, dependencies);
  } catch (error) {
    if (error instanceof ExternalAppOauthError) throw error;
    throw new ExternalAppOauthError(503, "oauth_install_unavailable");
  }
  return c.json(
    {
      uid: context.uid,
      redirect_url: app.appHomeUrl,
      state: consumed.client_state ?? null,
    },
    200,
    { "cache-control": "no-store" },
  );
}

export function registerExternalAppOauthRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
  dependencies: ExternalAppOauthDependencies = {},
): void {
  app.get("/v2/cf/oauth/authorize", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await authorize(c, context, dependencies);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
  app.post("/v2/cf/oauth/token", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await token(c, context, dependencies);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
}
