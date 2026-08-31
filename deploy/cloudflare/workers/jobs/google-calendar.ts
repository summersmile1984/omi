import type { Context, Hono } from "hono";
import type { SignedAuthContext } from "../shared/auth-context";
import type { JobsEnv } from "./env";

const GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar";
const GOOGLE_CALENDAR_OAUTH_ALIASES = new Set([
  "google_calendar",
  "gmail",
  "google_mail",
  "email",
  "contacts",
  "google_contacts",
]);
const GOOGLE_CALENDAR_DELETE_ALIASES = new Set(["google_calendar", "gmail"]);
// Only the canonical Calendar grant has an equivalent D1 projection for the
// legacy app-key mutation route. The generic Worker boundary fails closed for
// other providers instead of persisting an unscoped integration record without
// an owning projection.
const GOOGLE_CALENDAR_MUTATION_ALIASES = new Set(["google_calendar"]);
const TOKEN_SENTINEL = "configured";
const OAUTH_STATE_TTL_SECONDS = 10 * 60;
const TOKEN_REFRESH_BUFFER_SECONDS = 5 * 60;
const MAX_PROVIDER_RESPONSE_BYTES = 1_000_000;
const MAX_MUTATION_BODY_BYTES = 32_000;
const MAX_SEARCH_QUERY_LENGTH = 500;
const MAX_CONVERSATION_ID_LENGTH = 256;
const MAX_EVENT_ID_LENGTH = 512;
const MAX_TITLE_LENGTH = 500;
const MAX_DESCRIPTION_LENGTH = 10_000;
const MAX_LOCATION_LENGTH = 2_000;
const MAX_ATTENDEES = 50;
const MIN_OVERLAP_SECONDS = 10;
const MIN_OVERLAP_PERCENTAGE = 0.5;
const DEFAULT_SHARE_BASE_URL = "https://h.omi.me";

type JobsContext = Context<{ Bindings: JobsEnv }>;
type RequestContext = (c: JobsContext) => Promise<SignedAuthContext | null>;
type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type GoogleCalendarDependencies = Readonly<{
  fetchImpl?: FetchLike;
  now?: () => number;
}>;

type CalendarRow = {
  uid: string;
  connected: number;
  access_token_enc: string | null;
  refresh_token_enc: string | null;
  token_expires_at: number | null;
  granted_scopes_json: string;
  created_at: number;
  updated_at: number;
};

type CalendarCredentials = {
  row: CalendarRow;
  accessToken: string;
  refreshToken: string | null;
};

class GoogleCalendarError extends Error {
  constructor(
    readonly status: 400 | 401 | 402 | 404 | 413 | 422 | 502 | 503,
    readonly detail: string,
  ) {
    super(detail);
  }
}

function nowSeconds(dependencies?: GoogleCalendarDependencies) {
  return dependencies?.now?.() ?? Math.floor(Date.now() / 1_000);
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function normalizeCalendarAlias(value: string) {
  return value.trim().toLowerCase().replace(/-/g, "_");
}

function base64Url(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function decodeBase64Url(value: string) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
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

function randomToken(bytes: number) {
  return base64Url(crypto.getRandomValues(new Uint8Array(bytes)));
}

function encryptionSecret(env: JobsEnv) {
  const value = env.GOOGLE_CALENDAR_TOKEN_ENCRYPTION_SECRET;
  if (
    typeof value !== "string" ||
    new TextEncoder().encode(value).byteLength < 32
  ) {
    throw new GoogleCalendarError(
      503,
      "Google Calendar credential encryption is not configured",
    );
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
  return new TextEncoder().encode(`omi:google-calendar:v1\0${uid}\0${field}`);
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
  envelope: string,
) {
  const parts = envelope.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") {
    throw new Error("invalid Google Calendar credential envelope");
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
  const value = new TextDecoder().decode(plaintext);
  if (!value) throw new Error("empty Google Calendar credential");
  return value;
}

function publicApiBase(env: JobsEnv) {
  const value = env.PUBLIC_API_BASE_URL?.trim();
  if (!value) {
    throw new GoogleCalendarError(503, "Public API base URL is not configured");
  }
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      throw new Error("invalid base URL");
    }
    parsed.pathname = parsed.pathname.replace(/\/+$/, "") + "/";
    return parsed;
  } catch {
    throw new GoogleCalendarError(503, "Public API base URL is not configured");
  }
}

function googleConfiguration(env: JobsEnv) {
  // Prefer a Calendar-specific client when one is provisioned. The generic
  // client is the canonical Better Auth/legacy integration credential and can
  // be reused as long as its Google OAuth client has this callback URI
  // registered. Keeping the fallback here avoids duplicating one secret across
  // Workers while preserving an explicit override for deployments that need a
  // separate consent app.
  const clientId = (
    env.GOOGLE_CALENDAR_CLIENT_ID || env.GOOGLE_CLIENT_ID
  )?.trim();
  const clientSecret = (
    env.GOOGLE_CALENDAR_CLIENT_SECRET || env.GOOGLE_CLIENT_SECRET
  )?.trim();
  if (!clientId || !clientSecret) {
    throw new GoogleCalendarError(503, "Google Calendar is not configured");
  }
  encryptionSecret(env);
  return {
    clientId,
    clientSecret,
    redirectUri: new URL(
      "/v2/integrations/google-calendar/callback",
      publicApiBase(env),
    ).toString(),
  };
}

async function readCalendar(env: JobsEnv, uid: string) {
  return env.APP_DB.prepare(
    "SELECT uid, connected, access_token_enc, refresh_token_enc, token_expires_at, granted_scopes_json, created_at, updated_at " +
      "FROM cf_google_calendar_integrations WHERE uid = ?",
  )
    .bind(uid)
    .first<CalendarRow>();
}

type CalendarOnboardingProjection = {
  connected: boolean;
  hasAccessToken: boolean;
  reauthRequired: boolean;
  reauthReason: string | null;
};

async function syncOnboardingProjection(
  env: JobsEnv,
  uid: string,
  projection: CalendarOnboardingProjection,
  now: number,
) {
  const current = await env.APP_DB.prepare(
    "SELECT onboarding_skipped, created_at FROM cf_user_calendar_onboarding WHERE uid = ?",
  )
    .bind(uid)
    .first<{ onboarding_skipped: number; created_at: number }>();
  await env.APP_DB.prepare(
    "INSERT INTO cf_user_calendar_onboarding " +
      "(uid, connected, onboarding_skipped, reauth_required, has_access_token, reauth_reason, created_at, updated_at) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(uid) DO UPDATE SET connected = excluded.connected, " +
      "reauth_required = excluded.reauth_required, has_access_token = excluded.has_access_token, " +
      "reauth_reason = excluded.reauth_reason, updated_at = excluded.updated_at",
  )
    .bind(
      uid,
      projection.connected ? 1 : 0,
      current?.onboarding_skipped ?? 0,
      projection.reauthRequired ? 1 : 0,
      projection.hasAccessToken ? 1 : 0,
      projection.reauthReason,
      current?.created_at ?? now,
      now,
    )
    .run();
}

async function readJsonObject(c: JobsContext) {
  const declaredLength = Number(c.req.header("content-length") || "0");
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_MUTATION_BODY_BYTES
  ) {
    throw new GoogleCalendarError(413, "Request body is too large");
  }
  const bytes = new Uint8Array(await c.req.arrayBuffer());
  if (bytes.byteLength > MAX_MUTATION_BODY_BYTES) {
    throw new GoogleCalendarError(413, "Request body is too large");
  }
  try {
    const value = objectValue(JSON.parse(new TextDecoder().decode(bytes)));
    if (!value) throw new Error("not an object");
    return value;
  } catch (error) {
    if (error instanceof GoogleCalendarError) throw error;
    throw new GoogleCalendarError(400, "Invalid JSON body");
  }
}

function optionalToken(payload: Record<string, unknown>, field: string) {
  const value = payload[field];
  if (value === undefined || value === TOKEN_SENTINEL) return undefined;
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 16_000
  ) {
    throw new GoogleCalendarError(422, `Invalid ${field}`);
  }
  return value;
}

async function saveCalendar(
  env: JobsEnv,
  uid: string,
  payload: Record<string, unknown>,
  now: number,
) {
  const current = await readCalendar(env, uid);
  const connectedValue = payload.connected;
  if (connectedValue !== undefined && typeof connectedValue !== "boolean") {
    throw new GoogleCalendarError(422, "Invalid connected value");
  }
  const connected = connectedValue === undefined ? true : connectedValue;
  const accessToken = optionalToken(payload, "access_token");
  const refreshToken = optionalToken(payload, "refresh_token");
  let accessTokenEnc = current?.access_token_enc ?? null;
  let refreshTokenEnc = current?.refresh_token_enc ?? null;
  if (typeof accessToken === "string") {
    accessTokenEnc = await encryptCredential(
      env,
      uid,
      "access_token",
      accessToken,
    );
  } else if (accessToken === null) {
    accessTokenEnc = null;
  }
  if (typeof refreshToken === "string") {
    refreshTokenEnc = await encryptCredential(
      env,
      uid,
      "refresh_token",
      refreshToken,
    );
  } else if (refreshToken === null) {
    refreshTokenEnc = null;
  }
  if (connected && !accessTokenEnc) {
    throw new GoogleCalendarError(422, "Access token is required");
  }
  await env.APP_DB.prepare(
    "INSERT INTO cf_google_calendar_integrations " +
      "(uid, connected, access_token_enc, refresh_token_enc, token_expires_at, granted_scopes_json, created_at, updated_at) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(uid) DO UPDATE SET connected = excluded.connected, access_token_enc = excluded.access_token_enc, " +
      "refresh_token_enc = excluded.refresh_token_enc, updated_at = excluded.updated_at",
  )
    .bind(
      uid,
      connected ? 1 : 0,
      accessTokenEnc,
      refreshTokenEnc,
      current?.token_expires_at ?? null,
      current?.granted_scopes_json ?? "[]",
      current?.created_at ?? now,
      now,
    )
    .run();
  await syncOnboardingProjection(
    env,
    uid,
    {
      connected,
      hasAccessToken: connected && Boolean(accessTokenEnc),
      reauthRequired: false,
      reauthReason: null,
    },
    now,
  );
}

async function providerJson(response: Response) {
  const declaredLength = Number(response.headers.get("content-length") || "0");
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_PROVIDER_RESPONSE_BYTES
  ) {
    throw new GoogleCalendarError(502, "Provider response is too large");
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength > MAX_PROVIDER_RESPONSE_BYTES) {
    throw new GoogleCalendarError(502, "Provider response is too large");
  }
  try {
    return objectValue(JSON.parse(new TextDecoder().decode(bytes)));
  } catch {
    throw new GoogleCalendarError(502, "Provider returned invalid JSON");
  }
}

async function providerFetch(
  dependencies: GoogleCalendarDependencies | undefined,
  input: string,
  init: RequestInit,
) {
  try {
    return await (dependencies?.fetchImpl ?? fetch)(input, init);
  } catch {
    throw new GoogleCalendarError(502, "Google Calendar is unavailable");
  }
}

function oauthAuthorizationUrl(
  clientId: string,
  redirectUri: string,
  state: string,
) {
  const url = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  url.searchParams.set("client_id", clientId);
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("state", state);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("access_type", "offline");
  url.searchParams.set("prompt", "consent");
  url.searchParams.set("scope", GOOGLE_CALENDAR_SCOPE);
  return url.toString();
}

async function exchangeGoogleCode(
  env: JobsEnv,
  code: string,
  dependencies?: GoogleCalendarDependencies,
) {
  const configuration = googleConfiguration(env);
  const response = await providerFetch(
    dependencies,
    "https://oauth2.googleapis.com/token",
    {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        code,
        client_id: configuration.clientId,
        client_secret: configuration.clientSecret,
        redirect_uri: configuration.redirectUri,
        grant_type: "authorization_code",
      }).toString(),
    },
  );
  const payload = await providerJson(response);
  if (
    !response.ok ||
    !payload ||
    typeof payload.access_token !== "string" ||
    !payload.access_token
  ) {
    throw new GoogleCalendarError(400, "Token exchange failed");
  }
  return payload;
}

async function storeGoogleConnection(
  env: JobsEnv,
  uid: string,
  tokenData: Record<string, unknown>,
  dependencies?: GoogleCalendarDependencies,
) {
  const now = nowSeconds(dependencies);
  const current = await readCalendar(env, uid);
  const accessToken = tokenData.access_token as string;
  const refreshToken =
    typeof tokenData.refresh_token === "string" && tokenData.refresh_token
      ? tokenData.refresh_token
      : null;
  const refreshTokenEnc = refreshToken
    ? await encryptCredential(env, uid, "refresh_token", refreshToken)
    : (current?.refresh_token_enc ?? null);
  const expiresIn = Number(tokenData.expires_in);
  const expiresAt =
    Number.isFinite(expiresIn) && expiresIn > 0
      ? now + Math.floor(expiresIn)
      : null;
  const scopes =
    typeof tokenData.scope === "string"
      ? Array.from(
          new Set(tokenData.scope.split(/\s+/).filter((scope) => scope)),
        )
      : [GOOGLE_CALENDAR_SCOPE];
  await env.APP_DB.prepare(
    "INSERT INTO cf_google_calendar_integrations " +
      "(uid, connected, access_token_enc, refresh_token_enc, token_expires_at, granted_scopes_json, created_at, updated_at) " +
      "VALUES (?, 1, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(uid) DO UPDATE SET connected = 1, access_token_enc = excluded.access_token_enc, " +
      "refresh_token_enc = excluded.refresh_token_enc, token_expires_at = excluded.token_expires_at, " +
      "granted_scopes_json = excluded.granted_scopes_json, updated_at = excluded.updated_at",
  )
    .bind(
      uid,
      await encryptCredential(env, uid, "access_token", accessToken),
      refreshTokenEnc,
      expiresAt,
      JSON.stringify(scopes),
      current?.created_at ?? now,
      now,
    )
    .run();
  await syncOnboardingProjection(
    env,
    uid,
    {
      connected: true,
      hasAccessToken: true,
      reauthRequired: false,
      reauthReason: null,
    },
    now,
  );
}

async function refreshCredentials(
  env: JobsEnv,
  credentials: CalendarCredentials,
  dependencies?: GoogleCalendarDependencies,
) {
  if (!credentials.refreshToken) {
    const now = nowSeconds(dependencies);
    await env.APP_DB.prepare(
      "UPDATE cf_google_calendar_integrations SET connected = 0, access_token_enc = NULL, token_expires_at = NULL, updated_at = ? WHERE uid = ?",
    )
      .bind(now, credentials.row.uid)
      .run();
    await syncOnboardingProjection(
      env,
      credentials.row.uid,
      {
        connected: false,
        hasAccessToken: false,
        reauthRequired: true,
        reauthReason: "token_expired",
      },
      now,
    );
    throw new GoogleCalendarError(
      401,
      "Google Calendar authentication expired. Please reconnect.",
    );
  }
  const configuration = googleConfiguration(env);
  const response = await providerFetch(
    dependencies,
    "https://oauth2.googleapis.com/token",
    {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        client_id: configuration.clientId,
        client_secret: configuration.clientSecret,
        refresh_token: credentials.refreshToken,
      }).toString(),
    },
  );
  const payload = await providerJson(response);
  const accessToken = payload?.access_token;
  if (!response.ok || typeof accessToken !== "string" || !accessToken) {
    if (response.status === 400 || response.status === 401) {
      await env.APP_DB.prepare(
        "UPDATE cf_google_calendar_integrations SET connected = 0, access_token_enc = NULL, token_expires_at = NULL, updated_at = ? WHERE uid = ?",
      )
        .bind(nowSeconds(dependencies), credentials.row.uid)
        .run();
      await syncOnboardingProjection(
        env,
        credentials.row.uid,
        {
          connected: false,
          hasAccessToken: false,
          reauthRequired: true,
          reauthReason: "token_expired",
        },
        nowSeconds(dependencies),
      );
    }
    throw new GoogleCalendarError(
      401,
      "Google Calendar authentication expired. Please reconnect.",
    );
  }
  const expiresIn = Number(payload.expires_in);
  const expiresAt =
    Number.isFinite(expiresIn) && expiresIn > 0
      ? nowSeconds(dependencies) + Math.floor(expiresIn)
      : null;
  const accessTokenEnc = await encryptCredential(
    env,
    credentials.row.uid,
    "access_token",
    accessToken,
  );
  await env.APP_DB.prepare(
    "UPDATE cf_google_calendar_integrations SET connected = 1, access_token_enc = ?, token_expires_at = ?, updated_at = ? WHERE uid = ?",
  )
    .bind(
      accessTokenEnc,
      expiresAt,
      nowSeconds(dependencies),
      credentials.row.uid,
    )
    .run();
  await syncOnboardingProjection(
    env,
    credentials.row.uid,
    {
      connected: true,
      hasAccessToken: true,
      reauthRequired: false,
      reauthReason: null,
    },
    nowSeconds(dependencies),
  );
  return {
    row: {
      ...credentials.row,
      connected: 1,
      access_token_enc: accessTokenEnc,
      token_expires_at: expiresAt,
    },
    accessToken,
    refreshToken: credentials.refreshToken,
  } satisfies CalendarCredentials;
}

async function calendarCredentials(
  env: JobsEnv,
  uid: string,
  dependencies?: GoogleCalendarDependencies,
) {
  const row = await readCalendar(env, uid);
  if (!row || row.connected !== 1 || !row.access_token_enc) {
    throw new GoogleCalendarError(400, "Google Calendar not connected");
  }
  let credentials: CalendarCredentials;
  try {
    credentials = {
      row,
      accessToken: await decryptCredential(
        env,
        uid,
        "access_token",
        row.access_token_enc,
      ),
      refreshToken: row.refresh_token_enc
        ? await decryptCredential(
            env,
            uid,
            "refresh_token",
            row.refresh_token_enc,
          )
        : null,
    };
  } catch (error) {
    if (error instanceof GoogleCalendarError) throw error;
    throw new GoogleCalendarError(
      503,
      "Google Calendar credentials are unavailable",
    );
  }
  if (
    credentials.refreshToken &&
    (row.token_expires_at === null ||
      row.token_expires_at <=
        nowSeconds(dependencies) + TOKEN_REFRESH_BUFFER_SECONDS)
  ) {
    credentials = await refreshCredentials(env, credentials, dependencies);
  }
  return credentials;
}

async function requestCalendar(
  env: JobsEnv,
  uid: string,
  url: string,
  init: RequestInit = {},
  dependencies?: GoogleCalendarDependencies,
) {
  let credentials = await calendarCredentials(env, uid, dependencies);
  const makeRequest = (accessToken: string) => {
    const headers = new Headers(init.headers);
    headers.set("authorization", `Bearer ${accessToken}`);
    return providerFetch(dependencies, url, { ...init, headers });
  };
  let response = await makeRequest(credentials.accessToken);
  if (response.status !== 401) return response;
  await response.arrayBuffer();
  credentials = await refreshCredentials(env, credentials, dependencies);
  response = await makeRequest(credentials.accessToken);
  if (response.status !== 401) return response;
  await response.arrayBuffer();
  await env.APP_DB.prepare(
    "UPDATE cf_google_calendar_integrations SET connected = 0, access_token_enc = NULL, token_expires_at = NULL, updated_at = ? WHERE uid = ?",
  )
    .bind(nowSeconds(dependencies), uid)
    .run();
  await syncOnboardingProjection(
    env,
    uid,
    {
      connected: false,
      hasAccessToken: false,
      reauthRequired: true,
      reauthReason: "token_expired",
    },
    nowSeconds(dependencies),
  );
  throw new GoogleCalendarError(
    401,
    "Google Calendar authentication expired. Please reconnect.",
  );
}

function deepLink(success: boolean, error?: string) {
  const url = new URL("omi://google_calendar/callback");
  if (success) url.searchParams.set("success", "true");
  else url.searchParams.set("error", error || "server_error");
  return url.toString();
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function oauthResponse(success: boolean, error?: string) {
  const destination = deepLink(success, error);
  const title = success ? "Authentication successful" : "Authentication error";
  const detail = success
    ? "Returning to Omi…"
    : error === "invalid_state"
      ? "This authentication request is invalid or expired."
      : error === "missing_code"
        ? "No authorization code was received."
        : error === "config_error"
          ? "Google Calendar is not configured."
          : "Google could not complete authentication.";
  const redirectScript = success
    ? `<script>setTimeout(function(){location.replace(${JSON.stringify(destination)})},400)</script>`
    : "";
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><style>body{margin:0;background:#0a0a0a;color:#f5f5f5;font:16px system-ui;display:grid;min-height:100vh;place-items:center}.card{max-width:420px;padding:32px;border:1px solid #303030;border-radius:18px;background:#171717;text-align:center}h1{font-size:22px;margin:0 0 12px}p{color:#b8b8b8;margin:0 0 24px}a{color:#fff}</style></head><body><main class="card"><h1>${title}</h1><p>${detail}</p><a href="${escapeHtml(destination)}">Return to Omi</a></main>${redirectScript}</body></html>`;
  return new Response(html, {
    status: 200,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "content-security-policy":
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
      "x-content-type-options": "nosniff",
    },
  });
}

async function oauthCallback(
  c: JobsContext,
  dependencies?: GoogleCalendarDependencies,
) {
  const code = c.req.query("code");
  const state = c.req.query("state");
  if (!code || !state || code.length > 8_000 || state.length > 256) {
    return oauthResponse(false, "missing_code");
  }
  try {
    googleConfiguration(c.env);
  } catch {
    return oauthResponse(false, "config_error");
  }
  const stateRow = await c.env.APP_DB.prepare(
    "DELETE FROM cf_google_calendar_oauth_states WHERE state_hash = ? RETURNING uid, expires_at",
  )
    .bind(await sha256Hex(state))
    .first<{ uid: string; expires_at: number }>();
  if (!stateRow || stateRow.expires_at <= nowSeconds(dependencies)) {
    return oauthResponse(false, "invalid_state");
  }
  try {
    await storeGoogleConnection(
      c.env,
      stateRow.uid,
      await exchangeGoogleCode(c.env, code, dependencies),
      dependencies,
    );
    return oauthResponse(true);
  } catch {
    return oauthResponse(false, "token_exchange_failed");
  }
}

function dateQuery(value: string | undefined, fallback: number, name: string) {
  if (value === undefined) return fallback;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    throw new GoogleCalendarError(400, `Invalid ${name}`);
  }
  return timestamp;
}

function eventTime(value: unknown, allDayEnd: boolean) {
  const object = objectValue(value);
  const raw =
    typeof object?.dateTime === "string"
      ? object.dateTime
      : typeof object?.date === "string"
        ? `${object.date}T00:00:00.000Z`
        : null;
  if (!raw) return null;
  const timestamp = Date.parse(raw);
  if (!Number.isFinite(timestamp)) return null;
  const adjusted =
    allDayEnd && typeof object?.date === "string"
      ? timestamp - 1_000
      : timestamp;
  return new Date(adjusted).toISOString();
}

function eventResponse(value: unknown) {
  const event = objectValue(value);
  if (!event) return null;
  const startTime = eventTime(event.start, false);
  const endTime = eventTime(event.end, true);
  if (!startTime || !endTime) return null;
  const attendees = Array.isArray(event.attendees)
    ? event.attendees
        .map(objectValue)
        .filter(
          (item): item is Record<string, unknown> =>
            item !== null && item.self !== true,
        )
    : [];
  const attendeeNames: string[] = [];
  const attendeeEmails: string[] = [];
  for (const attendee of attendees) {
    const email = typeof attendee.email === "string" ? attendee.email : "";
    const name =
      typeof attendee.displayName === "string" && attendee.displayName
        ? attendee.displayName
        : email;
    if (name) attendeeNames.push(name);
    if (email) attendeeEmails.push(email);
  }
  return {
    event_id: typeof event.id === "string" ? event.id : "",
    title:
      typeof event.summary === "string" && event.summary
        ? event.summary
        : "Untitled Event",
    attendees: attendeeNames,
    attendee_emails: attendeeEmails,
    start_time: startTime,
    end_time: endTime,
    html_link: typeof event.htmlLink === "string" ? event.htmlLink : null,
  };
}

async function listEvents(
  env: JobsEnv,
  uid: string,
  c: JobsContext,
  dependencies?: GoogleCalendarDependencies,
) {
  const now = nowSeconds(dependencies) * 1_000;
  const timeMin = dateQuery(c.req.query("time_min"), now, "time_min");
  const timeMax = dateQuery(
    c.req.query("time_max"),
    timeMin + 7 * 24 * 60 * 60 * 1_000,
    "time_max",
  );
  if (timeMax <= timeMin) {
    throw new GoogleCalendarError(400, "time_max must be after time_min");
  }
  const maxResultsRaw = c.req.query("max_results") ?? "20";
  const maxResults = Number(maxResultsRaw);
  if (!Number.isInteger(maxResults) || maxResults < 1 || maxResults > 100) {
    throw new GoogleCalendarError(400, "Invalid max_results");
  }
  const searchQuery = c.req.query("q");
  if (searchQuery && searchQuery.length > MAX_SEARCH_QUERY_LENGTH) {
    throw new GoogleCalendarError(400, "Search query is too long");
  }
  const url = new URL(
    "https://www.googleapis.com/calendar/v3/calendars/primary/events",
  );
  url.searchParams.set("timeMin", new Date(timeMin).toISOString());
  url.searchParams.set("timeMax", new Date(timeMax).toISOString());
  url.searchParams.set("singleEvents", "true");
  url.searchParams.set("orderBy", "startTime");
  url.searchParams.set("maxResults", String(maxResults));
  url.searchParams.set(
    "fields",
    "items(id,summary,start,end,attendees(email,displayName,self),htmlLink)",
  );
  if (searchQuery) url.searchParams.set("q", searchQuery);
  const response = await requestCalendar(
    env,
    uid,
    url.toString(),
    {},
    dependencies,
  );
  const payload = await providerJson(response);
  if (!response.ok || !payload) {
    throw new GoogleCalendarError(502, "Failed to fetch calendar events");
  }
  return (Array.isArray(payload.items) ? payload.items : [])
    .map(eventResponse)
    .filter((event) => event !== null)
    .slice(0, maxResults);
}

type ConversationCalendarRow = {
  id: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  is_locked: number;
};

async function readConversationCalendarRow(
  env: JobsEnv,
  uid: string,
  conversationId: string,
) {
  return env.APP_DB.prepare(
    "SELECT id, created_at, started_at, finished_at, is_locked FROM cf_conversations WHERE uid = ? AND id = ?",
  )
    .bind(uid, conversationId)
    .first<ConversationCalendarRow>();
}

async function requireConversationForCalendar(
  env: JobsEnv,
  uid: string,
  conversationId: string,
) {
  if (
    !conversationId ||
    conversationId.length > MAX_CONVERSATION_ID_LENGTH ||
    conversationId.includes("/")
  ) {
    throw new GoogleCalendarError(400, "Invalid conversation id");
  }
  const conversation = await readConversationCalendarRow(
    env,
    uid,
    conversationId,
  );
  if (!conversation) {
    throw new GoogleCalendarError(404, "Conversation not found");
  }
  if (conversation.is_locked === 1) {
    throw new GoogleCalendarError(
      402,
      "A paid plan is required to access this conversation.",
    );
  }
  return conversation;
}

function validEventId(value: unknown) {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > MAX_EVENT_ID_LENGTH ||
    value.includes("\n") ||
    value.includes("\r")
  ) {
    throw new GoogleCalendarError(422, "Invalid event_id");
  }
  return value;
}

async function getCalendarEvent(
  env: JobsEnv,
  uid: string,
  eventId: string,
  dependencies?: GoogleCalendarDependencies,
) {
  const url = new URL(
    `https://www.googleapis.com/calendar/v3/calendars/primary/events/${encodeURIComponent(eventId)}`,
  );
  url.searchParams.set(
    "fields",
    "id,summary,start,end,attendees(email,displayName,self),htmlLink,description",
  );
  const response = await requestCalendar(
    env,
    uid,
    url.toString(),
    {},
    dependencies,
  );
  const payload = await providerJson(response);
  if (response.status === 404) {
    throw new GoogleCalendarError(404, "Calendar event not found");
  }
  if (!response.ok || !payload) {
    throw new GoogleCalendarError(502, "Failed to fetch calendar event");
  }
  return payload;
}

async function updateCalendarEventDescription(
  env: JobsEnv,
  uid: string,
  eventId: string,
  conversationId: string,
  currentDescription: string,
  dependencies?: GoogleCalendarDependencies,
) {
  const shareBase = (
    env.PUBLIC_SHARE_BASE_URL || DEFAULT_SHARE_BASE_URL
  ).trim();
  let base = DEFAULT_SHARE_BASE_URL;
  try {
    const parsed = new URL(shareBase || DEFAULT_SHARE_BASE_URL);
    if (
      parsed.protocol === "https:" &&
      !parsed.username &&
      !parsed.password &&
      !parsed.search &&
      !parsed.hash
    ) {
      base = parsed.toString().replace(/\/+$/, "");
    }
  } catch {
    // Keep the stable public share host when an optional override is invalid.
  }
  const conversationLink = `${base}/conversations/${encodeURIComponent(conversationId)}`;
  if (currentDescription.includes(conversationLink)) return;
  const description = currentDescription
    ? `${currentDescription}\n\n${conversationLink}`
    : conversationLink;
  await requestCalendar(
    env,
    uid,
    `https://www.googleapis.com/calendar/v3/calendars/primary/events/${encodeURIComponent(eventId)}`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description }),
    },
    dependencies,
  );
}

async function persistCalendarLink(
  env: JobsEnv,
  uid: string,
  conversationId: string,
  link: Record<string, unknown>,
  now: number,
) {
  await env.APP_DB.prepare(
    "UPDATE cf_conversations SET calendar_event_json = ?, updated_at = ? WHERE uid = ? AND id = ?",
  )
    .bind(JSON.stringify(link), now, uid, conversationId)
    .run();
}

async function linkCalendarEvent(
  env: JobsEnv,
  uid: string,
  conversationId: string,
  eventId: string,
  dependencies?: GoogleCalendarDependencies,
) {
  await requireConversationForCalendar(env, uid, conversationId);
  const event = await getCalendarEvent(env, uid, eventId, dependencies);
  const link = eventResponse(event);
  if (!link || typeof link.event_id !== "string" || !link.event_id) {
    throw new GoogleCalendarError(400, "Could not parse calendar event times");
  }
  await persistCalendarLink(
    env,
    uid,
    conversationId,
    link,
    nowSeconds(dependencies),
  );
  try {
    const description =
      typeof event.description === "string" ? event.description : "";
    await updateCalendarEventDescription(
      env,
      uid,
      link.event_id,
      conversationId,
      description,
      dependencies,
    );
  } catch {
    // The local link is authoritative. A provider description update is best effort.
  }
  return link;
}

async function findOverlappingCalendarEvent(
  env: JobsEnv,
  uid: string,
  startedAt: number,
  finishedAt: number,
  dependencies?: GoogleCalendarDependencies,
) {
  const searchStart = startedAt - 30 * 60;
  const searchEnd = finishedAt + 30 * 60;
  const url = new URL(
    "https://www.googleapis.com/calendar/v3/calendars/primary/events",
  );
  url.searchParams.set("timeMin", new Date(searchStart * 1_000).toISOString());
  url.searchParams.set("timeMax", new Date(searchEnd * 1_000).toISOString());
  url.searchParams.set("singleEvents", "true");
  url.searchParams.set("orderBy", "startTime");
  url.searchParams.set("maxResults", "20");
  url.searchParams.set(
    "fields",
    "items(id,summary,start,end,attendees(email,displayName,self),htmlLink,description)",
  );
  const response = await requestCalendar(
    env,
    uid,
    url.toString(),
    {},
    dependencies,
  );
  const payload = await providerJson(response);
  if (!response.ok || !payload) {
    throw new GoogleCalendarError(502, "Failed to fetch calendar events");
  }
  const conversationStart = startedAt * 1_000;
  const conversationEnd = Math.max(finishedAt, startedAt) * 1_000;
  let best: Record<string, unknown> | null = null;
  let bestOverlap = 0;
  for (const raw of Array.isArray(payload.items) ? payload.items : []) {
    const event = objectValue(raw);
    const link = eventResponse(event);
    if (!event || !link) continue;
    const eventStart = Date.parse(link.start_time);
    const eventEnd = Date.parse(link.end_time);
    const overlap =
      Math.min(eventEnd, conversationEnd) -
      Math.max(eventStart, conversationStart);
    if (overlap <= 0) continue;
    const eventDuration = eventEnd - eventStart;
    const conversationDuration = Math.max(
      conversationEnd - conversationStart,
      1,
    );
    const meetsPercentage =
      (eventDuration > 0 &&
        overlap / eventDuration >= MIN_OVERLAP_PERCENTAGE) ||
      overlap / conversationDuration >= MIN_OVERLAP_PERCENTAGE;
    if (
      overlap >= MIN_OVERLAP_SECONDS * 1_000 &&
      meetsPercentage &&
      overlap > bestOverlap
    ) {
      best = event;
      bestOverlap = overlap;
    }
  }
  return best;
}

async function autoLinkCalendarEvent(
  env: JobsEnv,
  uid: string,
  conversationId: string,
  dependencies?: GoogleCalendarDependencies,
) {
  const conversation = await requireConversationForCalendar(
    env,
    uid,
    conversationId,
  );
  const startedAt = conversation.started_at ?? conversation.created_at;
  const finishedAt = conversation.finished_at ?? startedAt;
  if (!startedAt) {
    throw new GoogleCalendarError(
      400,
      "Conversation has no timestamp information",
    );
  }
  const event = await findOverlappingCalendarEvent(
    env,
    uid,
    startedAt,
    finishedAt,
    dependencies,
  );
  if (!event) {
    throw new GoogleCalendarError(404, "No overlapping calendar event found");
  }
  const link = eventResponse(event);
  if (!link || typeof link.event_id !== "string" || !link.event_id) {
    throw new GoogleCalendarError(400, "Could not parse calendar event times");
  }
  await persistCalendarLink(
    env,
    uid,
    conversationId,
    link,
    nowSeconds(dependencies),
  );
  try {
    await updateCalendarEventDescription(
      env,
      uid,
      link.event_id,
      conversationId,
      typeof event.description === "string" ? event.description : "",
      dependencies,
    );
  } catch {
    // Keep the successful local auto-link even when the optional provider patch fails.
  }
  return link;
}

function requiredTimezoneDate(payload: Record<string, unknown>, key: string) {
  const value = payload[key];
  if (typeof value !== "string" || !value) {
    throw new GoogleCalendarError(422, `Invalid ${key}`);
  }
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(value)) {
    throw new GoogleCalendarError(422, `${key} must include timezone`);
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    throw new GoogleCalendarError(422, `Invalid ${key}`);
  }
  return { value, timestamp };
}

function boundedString(
  payload: Record<string, unknown>,
  key: string,
  maxLength: number,
) {
  const value = payload[key];
  if (value === undefined || value === null) return null;
  if (typeof value !== "string" || value.length > maxLength) {
    throw new GoogleCalendarError(422, `Invalid ${key}`);
  }
  return value;
}

function attendeeEmails(payload: Record<string, unknown>) {
  const raw = boundedString(payload, "attendees", MAX_DESCRIPTION_LENGTH);
  if (!raw) return [];
  const values = raw
    .split(",")
    .map((value) => value.trim())
    .filter((value) => value);
  if (values.length > MAX_ATTENDEES) {
    throw new GoogleCalendarError(422, "Too many attendees");
  }
  if (values.some((value) => !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value))) {
    throw new GoogleCalendarError(
      422,
      "Attendees must be email addresses when Calendar-only scope is enabled",
    );
  }
  return values;
}

async function createCalendarEventTool(
  env: JobsEnv,
  uid: string,
  payload: Record<string, unknown>,
  dependencies?: GoogleCalendarDependencies,
) {
  const title = boundedString(payload, "title", MAX_TITLE_LENGTH)?.trim();
  if (!title) throw new GoogleCalendarError(422, "Invalid title");
  const start = requiredTimezoneDate(payload, "start_time");
  const end = requiredTimezoneDate(payload, "end_time");
  if (end.timestamp <= start.timestamp) {
    throw new GoogleCalendarError(422, "end_time must be after start_time");
  }
  const description = boundedString(
    payload,
    "description",
    MAX_DESCRIPTION_LENGTH,
  );
  const location = boundedString(payload, "location", MAX_LOCATION_LENGTH);
  const emails = attendeeEmails(payload);
  const body: Record<string, unknown> = {
    summary: title,
    start: {
      dateTime: new Date(start.timestamp).toISOString(),
      timeZone: "UTC",
    },
    end: {
      dateTime: new Date(end.timestamp).toISOString(),
      timeZone: "UTC",
    },
  };
  if (description) body.description = description;
  if (location) body.location = location;
  if (emails.length) body.attendees = emails.map((email) => ({ email }));

  try {
    const response = await requestCalendar(
      env,
      uid,
      "https://www.googleapis.com/calendar/v3/calendars/primary/events",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      },
      dependencies,
    );
    const result = await providerJson(response);
    if (!response.ok || !result) {
      return `Error creating calendar event: Google Calendar returned HTTP ${response.status}`;
    }
    const eventLink =
      typeof result.htmlLink === "string" ? result.htmlLink : "";
    const lines = [
      `✅ Successfully created calendar event: ${title}`,
      `   Start: ${new Date(start.timestamp).toISOString()}`,
      `   End: ${new Date(end.timestamp).toISOString()}`,
    ];
    if (location) lines.push(`   Location: ${location}`);
    if (emails.length) lines.push(`   Attendees: ${emails.join(", ")}`);
    if (eventLink) lines.push(`   View event: ${eventLink}`);
    return lines.join("\n");
  } catch (error) {
    if (error instanceof GoogleCalendarError) {
      return `Error creating calendar event: ${error.detail}`;
    }
    return "Error creating calendar event: Google Calendar is unavailable";
  }
}

function errorResponse(c: JobsContext, error: unknown) {
  if (error instanceof GoogleCalendarError) {
    return c.json({ detail: error.detail }, error.status);
  }
  return c.json({ error: "google_calendar_unavailable" }, 503);
}

export async function cleanupExpiredGoogleCalendarOAuthStates(
  env: Pick<JobsEnv, "APP_DB">,
  now: number,
) {
  await env.APP_DB.prepare(
    "DELETE FROM cf_google_calendar_oauth_states WHERE expires_at <= ?",
  )
    .bind(now)
    .run();
}

export function registerGoogleCalendarRoutes(
  app: Hono<{ Bindings: JobsEnv }>,
  requestContext: RequestContext,
  dependencies?: GoogleCalendarDependencies,
) {
  app.get("/v2/integrations/google-calendar/callback", (c) =>
    oauthCallback(c, dependencies),
  );
  // Older clients used the provider key with an underscore. Keep that
  // callback on the same Calendar-owned D1 state/token path while the
  // hyphenated URI remains the canonical OAuth redirect.
  app.get("/v2/integrations/google_calendar/callback", (c) =>
    oauthCallback(c, dependencies),
  );
  app.get("/v2/integrations/:appKey/callback", (c) => {
    const requested = normalizeCalendarAlias(c.req.param("appKey"));
    if (!GOOGLE_CALENDAR_OAUTH_ALIASES.has(requested)) {
      return oauthResponse(false, "config_error");
    }
    return oauthCallback(c, dependencies);
  });

  const issueOAuthUrl = async (
    c: JobsContext,
    requestedAppKey = "google_calendar",
  ) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    const normalizedAppKey = normalizeCalendarAlias(requestedAppKey);
    if (!GOOGLE_CALENDAR_OAUTH_ALIASES.has(normalizedAppKey)) {
      return c.json(
        { detail: `Unsupported integration: ${requestedAppKey}` },
        400,
      );
    }
    try {
      const configuration = googleConfiguration(c.env);
      const state = randomToken(32);
      const now = nowSeconds(dependencies);
      await c.env.APP_DB.batch([
        c.env.APP_DB.prepare(
          "DELETE FROM cf_google_calendar_oauth_states WHERE expires_at <= ?",
        ).bind(now),
        c.env.APP_DB.prepare(
          "INSERT INTO cf_google_calendar_oauth_states (state_hash, uid, expires_at, created_at) VALUES (?, ?, ?, ?)",
        ).bind(
          await sha256Hex(state),
          context.uid,
          now + OAUTH_STATE_TTL_SECONDS,
          now,
        ),
      ]);
      return c.json({
        auth_url: oauthAuthorizationUrl(
          configuration.clientId,
          configuration.redirectUri,
          state,
        ),
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  };

  app.get("/v1/integrations/google_calendar/oauth-url", (c) =>
    issueOAuthUrl(c),
  );

  // The legacy integration router exposed one provider-capability URL. Keep
  // that contract for Google-derived aliases while storing the state in the
  // Worker-owned D1 OAuth table. The exact Calendar route above remains the
  // canonical match for google_calendar.
  app.get("/v1/integrations/:appKey/oauth-url", (c) =>
    issueOAuthUrl(c, c.req.param("appKey")),
  );

  app.get("/v1/integrations/google_calendar", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const calendar = await readCalendar(c.env, context.uid);
      return c.json({
        connected: calendar?.connected === 1,
        app_key: "google_calendar",
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  const saveCalendarIntegration = async (c: JobsContext, appKey: string) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      await saveCalendar(
        c.env,
        context.uid,
        await readJsonObject(c),
        nowSeconds(dependencies),
      );
      return c.json({ status: "ok", app_key: appKey });
    } catch (error) {
      return errorResponse(c, error);
    }
  };

  app.put("/v1/integrations/google_calendar", (c) =>
    saveCalendarIntegration(c, "google_calendar"),
  );

  app.put("/v1/integrations/:appKey", async (c) => {
    const requested = normalizeCalendarAlias(c.req.param("appKey"));
    if (!GOOGLE_CALENDAR_MUTATION_ALIASES.has(requested)) {
      return c.json({ detail: "Integration not found" }, 404);
    }
    return saveCalendarIntegration(c, requested);
  });

  const disconnectCalendar = async (c: JobsContext) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const result = await c.env.APP_DB.prepare(
        "DELETE FROM cf_google_calendar_integrations WHERE uid = ?",
      )
        .bind(context.uid)
        .run();
      if (result.meta?.changes !== 1) {
        return c.json({ detail: "Integration not found" }, 404);
      }
      await syncOnboardingProjection(
        c.env,
        context.uid,
        {
          connected: false,
          hasAccessToken: false,
          reauthRequired: false,
          reauthReason: null,
        },
        nowSeconds(dependencies),
      );
      return new Response(null, { status: 204 });
    } catch (error) {
      return errorResponse(c, error);
    }
  };

  app.delete("/v1/integrations/google_calendar", disconnectCalendar);
  app.delete("/v1/integrations/:appKey", async (c) => {
    const requested = normalizeCalendarAlias(c.req.param("appKey"));
    if (!GOOGLE_CALENDAR_DELETE_ALIASES.has(requested)) {
      return c.json({ detail: "Integration not found" }, 404);
    }
    return disconnectCalendar(c);
  });

  app.get("/v1/calendar/google/events", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      return c.json(await listEvents(c.env, context.uid, c, dependencies));
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.post("/v1/conversations/:conversationId/calendar-event", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const payload = await readJsonObject(c);
      const eventId = validEventId(payload.event_id);
      return c.json(
        await linkCalendarEvent(
          c.env,
          context.uid,
          c.req.param("conversationId"),
          eventId,
          dependencies,
        ),
      );
    } catch (error) {
      return errorResponse(c, error);
    }
  });

  app.post(
    "/v1/conversations/:conversationId/calendar-event/auto-link",
    async (c) => {
      const context = await requestContext(c);
      if (!context) return c.json({ error: "unauthorized" }, 401);
      try {
        return c.json(
          await autoLinkCalendarEvent(
            c.env,
            context.uid,
            c.req.param("conversationId"),
            dependencies,
          ),
        );
      } catch (error) {
        return errorResponse(c, error);
      }
    },
  );

  app.post("/v1/tools/calendar-events", async (c) => {
    const context = await requestContext(c);
    if (!context) return c.json({ error: "unauthorized" }, 401);
    try {
      const resultText = await createCalendarEventTool(
        c.env,
        context.uid,
        await readJsonObject(c),
        dependencies,
      );
      return c.json({
        tool_name: "create_calendar_event",
        result_text: resultText,
        is_error: !resultText.startsWith(
          "✅ Successfully created calendar event:",
        ),
      });
    } catch (error) {
      return errorResponse(c, error);
    }
  });
}
