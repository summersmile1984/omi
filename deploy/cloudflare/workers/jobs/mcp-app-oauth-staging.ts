/**
 * Namespaced external-MCP OAuth staging boundary.
 *
 * This is deliberately separate from Better Auth's MCP OAuth (which
 * authenticates a client to Omi's MCP server) and from the legacy
 * /v1/apps/mcp routes. It only proves the provider authorization-code
 * transaction: metadata/registration -> PKCE redirect -> one-time callback
 * exchange -> encrypted credential connection. Tool discovery and app install
 * remain explicit follow-up gates.
 */

import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type McpAppOauthDependencies = Readonly<{
  fetchImpl?: FetchLike;
  now?: () => number;
}>;

type JsonObject = Record<string, unknown>;

class McpAppOauthError extends Error {
  constructor(
    readonly status: 400 | 401 | 404 | 409 | 413 | 422 | 502 | 503,
    readonly code: string,
  ) {
    super(code);
    this.name = "McpAppOauthError";
  }
}

const MAX_BODY_BYTES = 24_000;
const MAX_PROVIDER_RESPONSE_BYTES = 256_000;
const MAX_METADATA_BYTES = 100_000;
const MAX_CREDENTIAL_BYTES = 8_192;
const MAX_SCOPES = 64;
const STATE_TTL_SECONDS = 600;
const MAX_ENDPOINT_LENGTH = 2_048;
const OPAQUE_RE = /^[A-Za-z0-9._~-]{16,512}$/;
const CLIENT_ID_RE = /^[\x21-\x7e]{1,2048}$/;

// Workers' DOM typings model Uint8Array's backing buffer as ArrayBufferLike,
// while the Web Crypto declarations require BufferSource. These values are
// freshly allocated and safe to pass to Web Crypto.
function cryptoBytes(value: Uint8Array): BufferSource {
  return value as unknown as BufferSource;
}

function nowSeconds(dependencies: McpAppOauthDependencies): number {
  const now = dependencies.now?.() ?? Math.floor(Date.now() / 1_000);
  return Number.isSafeInteger(now) && now > 0 ? now : 0;
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function objectValue(value: unknown): JsonObject | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/g, "");
}

function decodeBase64Url(value: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("invalid envelope");
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function sha256(value: string): Promise<Uint8Array> {
  return new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
  );
}

async function sha256Hex(value: string): Promise<string> {
  return Array.from(await sha256(value), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function randomOpaque(bytes = 32): string {
  return base64Url(crypto.getRandomValues(new Uint8Array(bytes)));
}

function encryptionSecret(env: JobsEnv): string {
  const value = env.MCP_APP_TOKEN_ENCRYPTION_SECRET?.trim();
  if (!value || utf8Bytes(value) < 32)
    throw new McpAppOauthError(503, "mcp_app_oauth_unavailable");
  return value;
}

async function key(env: JobsEnv): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    cryptoBytes(await sha256(encryptionSecret(env))),
    { name: "AES-GCM" },
    false,
    ["encrypt", "decrypt"],
  );
}

function aad(
  field: "verifier" | "client_credentials" | "connection",
  ownerUid: string,
  appId: string,
  transactionId?: string,
): Uint8Array {
  return new TextEncoder().encode(
    `omi:mcp-app-oauth:v1\0${field}\0${ownerUid}\0${appId}\0${transactionId || ""}`,
  );
}

async function encrypt(
  env: JobsEnv,
  field: "verifier" | "client_credentials" | "connection",
  ownerUid: string,
  appId: string,
  value: JsonObject,
  transactionId?: string,
): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    {
      name: "AES-GCM",
      iv: cryptoBytes(iv),
      additionalData: cryptoBytes(aad(field, ownerUid, appId, transactionId)),
    },
    await key(env),
    cryptoBytes(new TextEncoder().encode(JSON.stringify(value))),
  );
  return `v1.${base64Url(iv)}.${base64Url(new Uint8Array(ciphertext))}`;
}

async function decrypt(
  env: JobsEnv,
  field: "verifier" | "client_credentials" | "connection",
  ownerUid: string,
  appId: string,
  envelope: string,
  transactionId?: string,
): Promise<JsonObject> {
  const parts = envelope.split(".");
  if (parts.length !== 3 || parts[0] !== "v1" || utf8Bytes(envelope) > 400_000)
    throw new Error("invalid envelope");
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: cryptoBytes(decodeBase64Url(parts[1])),
      additionalData: cryptoBytes(aad(field, ownerUid, appId, transactionId)),
    },
    await key(env),
    cryptoBytes(decodeBase64Url(parts[2])),
  );
  const value = objectValue(JSON.parse(new TextDecoder().decode(plaintext)));
  if (!value) throw new Error("invalid envelope payload");
  return value;
}

function publicHttps(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    !value ||
    utf8Bytes(value) > MAX_ENDPOINT_LENGTH
  )
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
      host.endsWith(".local") ||
      isPrivateIpLiteral(host)
    )
      return false;
    return true;
  } catch {
    return false;
  }
}

function isPrivateIpLiteral(host: string): boolean {
  const ipv4 = host.split(".");
  if (ipv4.length === 4 && ipv4.every((part) => /^\d{1,3}$/.test(part))) {
    const octets = ipv4.map(Number);
    if (octets.some((value) => value > 255)) return true;
    const [a, b] = octets;
    return (
      a === 0 ||
      a === 10 ||
      (a === 100 && b >= 64 && b <= 127) ||
      a === 127 ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 0) ||
      (a === 192 && b === 168) ||
      (a === 198 && b >= 18 && b <= 19) ||
      a >= 224
    );
  }
  if (!host.includes(":")) return false;
  const normalized = host.toLowerCase();
  const mapped = normalized.match(/^(?:::ffff:|::)(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped && isPrivateIpLiteral(mapped[1])) return true;
  const halves = normalized.split("::");
  if (halves.length > 2) return true;
  const left = halves[0] ? halves[0].split(":").filter(Boolean) : [];
  const right = halves[1] ? halves[1].split(":").filter(Boolean) : [];
  if (
    left.some((part) => !/^[0-9a-f]{1,4}$/.test(part)) ||
    right.some((part) => !/^[0-9a-f]{1,4}$/.test(part))
  )
    return true;
  const groups = [
    ...left,
    ...Array(8 - left.length - right.length).fill("0"),
    ...right,
  ];
  if (groups.length !== 8) return true;
  const first = Number.parseInt(groups[0], 16);
  const second = Number.parseInt(groups[1], 16);
  const isIpv4Compatible =
    first === 0 && groups.slice(1, 6).every((group) => group === "0");
  const isIpv4Mapped =
    first === 0 &&
    groups.slice(1, 5).every((group) => group === "0") &&
    groups[5].toLowerCase() === "ffff";
  const mappedIpv4 = isIpv4Mapped
    ? [
        Number.parseInt(groups[6], 16) >> 8,
        Number.parseInt(groups[6], 16) & 0xff,
        Number.parseInt(groups[7], 16) >> 8,
        Number.parseInt(groups[7], 16) & 0xff,
      ].join(".")
    : null;
  return (
    (first === 0 && groups.slice(1).every((group) => group === "0")) ||
    (isIpv4Compatible && (groups[7] === "0" || groups[7] === "1")) ||
    (mappedIpv4 !== null && isPrivateIpLiteral(mappedIpv4)) ||
    (first & 0xfe00) === 0xfc00 ||
    (first & 0xffc0) === 0xfe80 ||
    (first & 0xff00) === 0xff00 ||
    (first === 0x2001 && second === 0x0db8)
  );
}

function normalizeScopes(value: unknown): string[] {
  if (value === undefined || value === null || value === "") return [];
  if (!Array.isArray(value) && typeof value !== "string")
    throw new McpAppOauthError(422, "invalid_scope");
  const values = Array.isArray(value) ? value : value.split(/[\s,]+/);
  if (values.some((scope) => typeof scope !== "string"))
    throw new McpAppOauthError(422, "invalid_scope");
  const scopes = values
    .map((scope) => String(scope))
    .map((scope) => scope.trim())
    .filter(Boolean);
  if (
    scopes.length > MAX_SCOPES ||
    scopes.some(
      (scope) => utf8Bytes(scope) > 128 || !/^[A-Za-z0-9._:-]+$/.test(scope),
    )
  )
    throw new McpAppOauthError(422, "invalid_scope");
  return [...new Set(scopes)];
}

function validatedCredential(
  value: unknown,
  code: string,
  status: 422 | 502,
  required = false,
): string | null {
  if (value === undefined || value === null) {
    if (required) throw new McpAppOauthError(status, code);
    return null;
  }
  if (
    typeof value !== "string" ||
    utf8Bytes(value) === 0 ||
    utf8Bytes(value) > MAX_CREDENTIAL_BYTES ||
    /[\u0000-\u001f\u007f]/.test(value)
  )
    throw new McpAppOauthError(status, code);
  return value;
}

function parseBody(raw: string): JsonObject {
  if (utf8Bytes(raw) > MAX_BODY_BYTES)
    throw new McpAppOauthError(413, "request_too_large");
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new McpAppOauthError(400, "invalid_request");
  }
  const object = objectValue(value);
  if (!object) throw new McpAppOauthError(400, "invalid_request");
  return object;
}

async function boundedJson(
  response: Response,
  unavailableCode: string,
): Promise<JsonObject> {
  const contentLength = Number(response.headers.get("content-length") || "0");
  if (
    Number.isFinite(contentLength) &&
    contentLength > MAX_PROVIDER_RESPONSE_BYTES
  )
    throw new McpAppOauthError(502, unavailableCode);
  const raw = await readBoundedText(
    response.body,
    MAX_PROVIDER_RESPONSE_BYTES,
    unavailableCode,
  );
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw);
  } catch {
    throw new McpAppOauthError(502, unavailableCode);
  }
  const parsed = objectValue(decoded);
  if (!parsed) throw new McpAppOauthError(502, unavailableCode);
  return parsed;
}

async function readBoundedText(
  body: ReadableStream<Uint8Array> | null,
  limit: number,
  unavailableCode: string,
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
        throw new McpAppOauthError(502, unavailableCode);
      }
      chunks.push(next.value);
    }
  } catch (error) {
    if (error instanceof McpAppOauthError) throw error;
    throw new McpAppOauthError(502, unavailableCode);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

async function requestText(c: JobsContext): Promise<string> {
  const contentLength = Number(c.req.header("content-length") || "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES)
    throw new McpAppOauthError(413, "request_too_large");
  return readBoundedText(c.req.raw.body, MAX_BODY_BYTES, "request_too_large");
}

function errorResponse(c: JobsContext, error: unknown): Response {
  if (error instanceof McpAppOauthError)
    return c.json({ error: error.code }, error.status);
  return c.json({ error: "mcp_app_oauth_unavailable" }, 503);
}

function callbackHtml(title: string, message: string): string {
  const escape = (value: string) =>
    value.replace(
      /[&<>"']/g,
      (character) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[character] || character,
    );
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline'"><title>${escape(title)}</title></head><body><h1>${escape(title)}</h1><p>${escape(message)}</p></body></html>`;
}

function callbackResponse(
  c: JobsContext,
  status: 200 | 400 | 404 | 409 | 502 | 503,
  title: string,
  message: string,
): Response {
  c.header("cache-control", "no-store");
  c.header(
    "content-security-policy",
    "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
  );
  return c.html(callbackHtml(title, message), status);
}

function callbackUri(env: JobsEnv): string {
  const base = env.PUBLIC_API_BASE_URL?.trim();
  if (!publicHttps(base))
    throw new McpAppOauthError(503, "mcp_app_oauth_unavailable");
  return new URL(
    "/v2/cf/apps/mcp/callback",
    `${base.replace(/\/$/, "")}/`,
  ).toString();
}

async function providerFetch(
  dependencies: McpAppOauthDependencies,
  input: RequestInfo | URL,
  init: RequestInit,
): Promise<Response> {
  try {
    return await (dependencies.fetchImpl || fetch)(input, {
      ...init,
      redirect: "error",
      signal: AbortSignal.timeout(20_000),
    });
  } catch {
    throw new McpAppOauthError(503, "provider_unavailable");
  }
}

async function start(
  c: JobsContext,
  context: SignedAuthContext,
  dependencies: McpAppOauthDependencies,
): Promise<Response> {
  if (c.env.MCP_APP_OAUTH_STAGING_ENABLED !== "true")
    throw new McpAppOauthError(404, "not_found");
  let body: JsonObject;
  try {
    body = parseBody(await requestText(c));
  } catch (error) {
    throw error;
  }
  const appId = typeof body.app_id === "string" ? body.app_id.trim() : "";
  const serverUrl =
    typeof body.server_url === "string"
      ? body.server_url.trim().replace(/\/$/, "")
      : "";
  const authorizationEndpoint = body.authorization_endpoint;
  const tokenEndpoint = body.token_endpoint;
  const registrationEndpoint = body.registration_endpoint;
  if (
    !appId ||
    appId.length > 256 ||
    !publicHttps(serverUrl) ||
    !publicHttps(authorizationEndpoint) ||
    !publicHttps(tokenEndpoint)
  )
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  if (registrationEndpoint !== undefined && !publicHttps(registrationEndpoint))
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  const scopes = normalizeScopes(body.scopes);
  const app = await c.env.APP_DB.prepare(
    "SELECT id, owner_uid, disabled FROM cf_app_catalog WHERE id = ? AND owner_uid = ? LIMIT 1",
  )
    .bind(appId, context.uid)
    .first<{ id?: unknown; owner_uid?: unknown; disabled?: unknown }>();
  if (!app || Number(app.disabled) === 1)
    throw new McpAppOauthError(404, "app_not_found");
  const existingConnection = await c.env.APP_DB.prepare(
    "SELECT owner_uid FROM cf_mcp_app_connections WHERE app_id = ? LIMIT 1",
  )
    .bind(appId)
    .first<{ owner_uid?: unknown }>();
  if (existingConnection && existingConnection.owner_uid !== context.uid)
    throw new McpAppOauthError(409, "app_connection_owner_mismatch");
  const redirectUri = callbackUri(c.env);
  let clientId =
    typeof body.client_id === "string" ? body.client_id.trim() : "";
  let clientSecret =
    typeof body.client_secret === "string" ? body.client_secret : null;
  if (clientId && (!CLIENT_ID_RE.test(clientId) || utf8Bytes(clientId) > 2_048))
    throw new McpAppOauthError(422, "invalid_client");
  clientSecret = validatedCredential(clientSecret, "invalid_client", 422);
  if (registrationEndpoint) {
    const registration = await providerFetch(
      dependencies,
      registrationEndpoint,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify({
          client_name: "Omi",
          redirect_uris: [redirectUri],
          token_endpoint_auth_method: "none",
          grant_types: ["authorization_code"],
          response_types: ["code"],
          ...(scopes.length ? { scope: scopes.join(" ") } : {}),
        }),
      },
    );
    if (!registration.ok)
      throw new McpAppOauthError(502, "registration_failed");
    const payload = await boundedJson(registration, "registration_invalid");
    clientId = typeof payload.client_id === "string" ? payload.client_id : "";
    clientSecret = validatedCredential(
      payload.client_secret,
      "registration_invalid",
      502,
    );
    if (!clientId || !CLIENT_ID_RE.test(clientId))
      throw new McpAppOauthError(502, "registration_invalid");
  }
  if (!clientId)
    throw new McpAppOauthError(422, "client_registration_required");
  const now = nowSeconds(dependencies);
  if (!now) throw new McpAppOauthError(503, "clock_unavailable");
  const transactionId = crypto.randomUUID();
  const state = randomOpaque();
  const verifier = randomOpaque();
  const challenge = base64Url(await sha256(verifier));
  const metadata = JSON.stringify({
    authorization_endpoint: authorizationEndpoint,
    token_endpoint: tokenEndpoint,
    registration_endpoint: registrationEndpoint || null,
    scopes,
  });
  if (utf8Bytes(metadata) > MAX_METADATA_BYTES)
    throw new McpAppOauthError(422, "invalid_provider_metadata");
  const verifierEnvelope = await encrypt(
    c.env,
    "verifier",
    context.uid,
    appId,
    { verifier },
    transactionId,
  );
  const credentialsEnvelope = clientSecret
    ? await encrypt(
        c.env,
        "client_credentials",
        context.uid,
        appId,
        { client_secret: clientSecret },
        transactionId,
      )
    : null;
  await c.env.APP_DB.batch([
    c.env.APP_DB.prepare(
      `UPDATE cf_mcp_app_oauth_transactions
          SET status = 'expired', last_error = 'superseded', updated_at = ?
        WHERE app_id = ? AND owner_uid = ? AND status = 'pending'`,
    ).bind(now, appId, context.uid),
    c.env.APP_DB.prepare(
      `INSERT INTO cf_mcp_app_connections
         (app_id, owner_uid, server_url, status, oauth_metadata_json, credential_envelope_enc, oauth_transaction_id, revision, created_at, updated_at)
       VALUES (?, ?, ?, 'pending', ?, NULL, ?, 0, ?, ?)
       ON CONFLICT(app_id) DO UPDATE SET server_url = excluded.server_url,
         status = 'pending', oauth_metadata_json = excluded.oauth_metadata_json,
         credential_envelope_enc = NULL, last_error = NULL,
         oauth_transaction_id = excluded.oauth_transaction_id,
         updated_at = excluded.updated_at
       WHERE cf_mcp_app_connections.owner_uid = excluded.owner_uid`,
    ).bind(appId, context.uid, serverUrl, metadata, transactionId, now, now),
    c.env.APP_DB.prepare(
      `INSERT INTO cf_mcp_app_oauth_transactions
         (transaction_id, app_id, owner_uid, state_hash, code_verifier_enc, client_credentials_enc,
          authorization_endpoint, token_endpoint, registration_endpoint, client_id, redirect_uri,
          status, attempts, expires_at, consumed_at, last_error, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)`,
    ).bind(
      transactionId,
      appId,
      context.uid,
      await sha256Hex(state),
      verifierEnvelope,
      credentialsEnvelope,
      authorizationEndpoint,
      tokenEndpoint,
      registrationEndpoint || null,
      clientId,
      redirectUri,
      now + STATE_TTL_SECONDS,
      now,
      now,
    ),
  ]);
  const url = new URL(authorizationEndpoint);
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
    ...(scopes.length ? { scope: scopes.join(" ") } : {}),
  }).toString();
  return c.json(
    { app_id: appId, requires_oauth: true, auth_url: url.toString() },
    200,
    { "cache-control": "no-store" },
  );
}

async function callback(
  c: JobsContext,
  dependencies: McpAppOauthDependencies,
): Promise<Response> {
  if (c.env.MCP_APP_OAUTH_STAGING_ENABLED !== "true")
    return callbackResponse(
      c,
      404,
      "Not found",
      "This staging seam is disabled.",
    );
  const state = c.req.query("state") || "";
  const code = c.req.query("code") || "";
  if (!OPAQUE_RE.test(state))
    return callbackResponse(
      c,
      400,
      "Authorization failed",
      "Invalid or expired state.",
    );
  const now = nowSeconds(dependencies);
  if (!now)
    return callbackResponse(
      c,
      503,
      "Authorization unavailable",
      "The authorization service is unavailable.",
    );
  const stateHash = await sha256Hex(state);
  const consumed = await c.env.APP_DB.prepare(
    `UPDATE cf_mcp_app_oauth_transactions
        SET status = 'exchanged', attempts = attempts + 1, consumed_at = ?, updated_at = ?
      WHERE state_hash = ? AND status = 'pending' AND expires_at > ?
      RETURNING transaction_id, app_id, owner_uid, code_verifier_enc, client_credentials_enc,
                token_endpoint, client_id, redirect_uri`,
  )
    .bind(now, now, stateHash, now)
    .first<{
      transaction_id?: string;
      app_id?: string;
      owner_uid?: string;
      code_verifier_enc?: string;
      client_credentials_enc?: string | null;
      token_endpoint?: string;
      client_id?: string;
      redirect_uri?: string;
    }>();
  if (
    !consumed?.transaction_id ||
    !consumed.app_id ||
    !consumed.owner_uid ||
    !consumed.code_verifier_enc ||
    !consumed.token_endpoint ||
    !consumed.client_id ||
    !consumed.redirect_uri
  )
    return callbackResponse(
      c,
      400,
      "Authorization failed",
      "Invalid or expired state.",
    );
  if (
    !code ||
    utf8Bytes(code) > MAX_CREDENTIAL_BYTES ||
    /[\u0000-\u001f\u007f]/.test(code)
  ) {
    await c.env.APP_DB.prepare(
      "UPDATE cf_mcp_app_oauth_transactions SET status = 'failed', last_error = ?, updated_at = ? WHERE transaction_id = ? AND status = 'exchanged'",
    )
      .bind("missing provider code", now, consumed.transaction_id)
      .run();
    return callbackResponse(
      c,
      400,
      "Authorization failed",
      "The provider callback did not include a valid code.",
    );
  }
  try {
    const verifierPayload = await decrypt(
      c.env,
      "verifier",
      consumed.owner_uid,
      consumed.app_id,
      consumed.code_verifier_enc,
      consumed.transaction_id,
    );
    const verifier = verifierPayload.verifier;
    if (typeof verifier !== "string" || !OPAQUE_RE.test(verifier))
      throw new Error("invalid verifier");
    let clientSecret: string | null = null;
    if (consumed.client_credentials_enc) {
      const credentials = await decrypt(
        c.env,
        "client_credentials",
        consumed.owner_uid,
        consumed.app_id,
        consumed.client_credentials_enc,
        consumed.transaction_id,
      );
      clientSecret =
        typeof credentials.client_secret === "string"
          ? credentials.client_secret
          : null;
    }
    const form = new URLSearchParams({
      grant_type: "authorization_code",
      code,
      client_id: consumed.client_id,
      redirect_uri: consumed.redirect_uri,
      code_verifier: verifier,
      ...(clientSecret ? { client_secret: clientSecret } : {}),
    });
    const response = await providerFetch(
      dependencies,
      consumed.token_endpoint,
      {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          accept: "application/json",
        },
        body: form.toString(),
      },
    );
    if (!response.ok) throw new McpAppOauthError(502, "token_exchange_failed");
    const payload = await boundedJson(response, "token_response_invalid");
    const accessToken = validatedCredential(
      payload.access_token,
      "token_response_invalid",
      502,
      true,
    ) as string;
    const refreshToken = validatedCredential(
      payload.refresh_token,
      "token_response_invalid",
      502,
    );
    const expiresIn = Number(payload.expires_in || 3_600);
    if (!Number.isSafeInteger(expiresIn) || expiresIn < 1 || expiresIn > 86_400)
      throw new McpAppOauthError(502, "token_response_invalid");
    const connectionEnvelope = await encrypt(
      c.env,
      "connection",
      consumed.owner_uid,
      consumed.app_id,
      {
        access_token: accessToken,
        refresh_token: refreshToken,
        expires_in: expiresIn,
        issued_at: now,
      },
    );
    const updated = await c.env.APP_DB.prepare(
      `UPDATE cf_mcp_app_connections
          SET status = 'authorized', credential_envelope_enc = ?, oauth_transaction_id = NULL, revision = revision + 1,
              last_error = NULL, updated_at = ?
        WHERE app_id = ? AND owner_uid = ? AND status = 'pending' AND oauth_transaction_id = ?`,
    )
      .bind(
        connectionEnvelope,
        now,
        consumed.app_id,
        consumed.owner_uid,
        consumed.transaction_id,
      )
      .run();
    if (Number(updated.meta?.changes) !== 1)
      throw new McpAppOauthError(409, "app_connection_changed");
    return callbackResponse(
      c,
      200,
      "Authorization complete",
      "The MCP server is authorized. Tool discovery is pending.",
    );
  } catch (error) {
    const codeValue =
      error instanceof McpAppOauthError ? error.code : "token_exchange_failed";
    try {
      await c.env.APP_DB.prepare(
        "UPDATE cf_mcp_app_oauth_transactions SET status = 'failed', last_error = ?, updated_at = ? WHERE transaction_id = ? AND status = 'exchanged'",
      )
        .bind(codeValue.slice(0, 2_000), now, consumed.transaction_id)
        .run();
    } catch {
      // Account-deletion fences intentionally reject every further mutation.
    }
    const status =
      error instanceof McpAppOauthError &&
      (error.status === 409 || error.status === 503 || error.status === 502)
        ? error.status
        : 502;
    return callbackResponse(
      c,
      status,
      "Authorization unavailable",
      "The provider authorization could not be completed.",
    );
  }
}

export function registerMcpAppOauthRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
  dependencies: McpAppOauthDependencies = {},
): void {
  app.post("/v2/cf/apps/mcp/authorize", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return await start(c, context, dependencies);
    } catch (error) {
      return errorResponse(c, error);
    }
  });
  app.get("/v2/cf/apps/mcp/callback", (c) => callback(c, dependencies));
}
