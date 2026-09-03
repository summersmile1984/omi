/**
 * Namespaced native-auth compatibility seam.
 *
 * This module provides a namespaced replay surface and an independently gated
 * exact `/v1/auth/*` surface for the Firebase/native OAuth lifecycle while the
 * provider and identity bridges are being verified. Provider credentials are
 * returned to the native client only after the D1 code transaction is
 * atomically consumed; D1 stores only secret-derived AES-GCM envelopes and
 * SHA-256 lookups.
 */

import type { Context, Hono } from "hono";
import type { AuthEnv } from "./env";
import {
  exchangeFirebaseProviderCredential,
  FirebaseCustomTokenBridgeError,
  issueFirebaseCustomToken,
  resolveFirebaseIdentityByFirebaseUid,
} from "./firebase-custom-token-bridge";
import {
  consumeLegacyAuthTransaction,
  createLegacyAuthTransaction,
  isValidLegacyOpaqueSecret,
  isValidLegacyPkceChallenge,
  isValidLegacyRedirectUri,
  pruneExpiredLegacyAuthTransactions,
  type LegacyAuthProvider,
} from "./legacy-compatibility";

type NativeAuthContext = Context<{ Bindings: AuthEnv }>;
type NativeAuthApp = Hono<{ Bindings: AuthEnv }>;
type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type NativeAuthCompatibilityOptions = Readonly<{
  /**
   * The namespaced route is the default migration seam.  The legacy prefix is
   * registered only when Edge has explicitly enabled its staging owner; it
   * never changes the default gate or production route inventory by itself.
   */
  surface?: "namespaced" | "legacy";
}>;

export type NativeAuthCompatibilityDependencies = Readonly<{
  fetchImpl?: FetchLike;
  now?: () => number;
}>;

type ProviderConfiguration = {
  provider: LegacyAuthProvider;
  clientId: string;
  clientSecret: string | null;
  authorizationEndpoint: string;
  tokenEndpoint: string;
  callbackUri: string;
};

type SessionMetadata = {
  redirectUri: string;
  clientState: string | null;
  provider: LegacyAuthProvider;
};

type ProviderCredentials = {
  provider: LegacyAuthProvider;
  idToken: string;
  accessToken: string | null;
  expiresIn: number;
  fullName?: string;
};

class NativeAuthError extends Error {
  constructor(
    readonly status: 400 | 404 | 409 | 502 | 503,
    readonly code: string,
  ) {
    super(code);
    this.name = "NativeAuthError";
  }
}

const SESSION_TTL_SECONDS = 300;
const CODE_TTL_SECONDS = 300;
// The legacy `/v1/auth/token` contract exposes a fixed one-hour client token
// lifetime, independent of the provider's `expires_in` value. Keep the
// provider value inside the encrypted transaction only; changing this wire
// field would break released native clients that assume 3600 seconds.
const LEGACY_RESPONSE_EXPIRES_IN_SECONDS = 3_600;
const MAX_CALLBACK_BODY_BYTES = 16_384;
const MAX_TOKEN_BODY_BYTES = 8_192;
const MAX_PROVIDER_RESPONSE_BYTES = 256_000;
const MAX_CREDENTIAL_BYTES = 8_192;
const MAX_CLIENT_STATE_BYTES = 512;
const MAX_NAME_BYTES = 256;
const BASE64_URL_RE = /^[A-Za-z0-9_-]+$/;

async function bestEffortPruneTransactions(
  database: Parameters<typeof createLegacyAuthTransaction>[0],
  now: number,
): Promise<void> {
  try {
    await pruneExpiredLegacyAuthTransactions(database, now);
  } catch {
    // Transaction cleanup is maintenance, not an authorization prerequisite.
    // A D1 outage must not cause provider credentials to be issued from a
    // newly-created transaction, but it also must not turn a bounded cleanup
    // failure into a user-visible auth outage.
  }
}

// TypeScript's DOM lib models Uint8Array's backing buffer as ArrayBufferLike,
// while Workers' crypto declarations require BufferSource.  The values here
// are freshly allocated byte arrays and are safe for the Web Crypto calls.
function cryptoBytes(value: Uint8Array): BufferSource {
  return value as unknown as BufferSource;
}

function nowSeconds(dependencies: NativeAuthCompatibilityDependencies): number {
  const now = dependencies.now?.() ?? Math.floor(Date.now() / 1_000);
  return Number.isSafeInteger(now) && now > 0 ? now : 0;
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function base64Url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/g, "");
}

function decodeBase64Url(value: string): Uint8Array {
  if (!value || !BASE64_URL_RE.test(value)) throw new Error("invalid envelope");
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function transactionSecret(env: AuthEnv): string {
  const value = env.LEGACY_AUTH_TRANSACTION_ENCRYPTION_SECRET;
  if (!value || utf8Bytes(value) < 32) {
    throw new NativeAuthError(503, "transaction_authority_unavailable");
  }
  return value;
}

async function encryptionKey(env: AuthEnv): Promise<CryptoKey> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(transactionSecret(env)),
  );
  return crypto.subtle.importKey("raw", digest, { name: "AES-GCM" }, false, [
    "encrypt",
    "decrypt",
  ]);
}

function envelopeContext(
  kind: "session" | "code",
  provider: LegacyAuthProvider,
  transactionId: string,
): Uint8Array {
  return new TextEncoder().encode(
    `omi:legacy-auth:v1\0${kind}\0${provider}\0${transactionId}`,
  );
}

async function encryptEnvelope(
  env: AuthEnv,
  kind: "session" | "code",
  provider: LegacyAuthProvider,
  transactionId: string,
  value: Record<string, unknown>,
): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    {
      name: "AES-GCM",
      iv: cryptoBytes(iv),
      additionalData: cryptoBytes(
        envelopeContext(kind, provider, transactionId),
      ),
    },
    await encryptionKey(env),
    cryptoBytes(new TextEncoder().encode(JSON.stringify(value))),
  );
  return `v1.${base64Url(iv)}.${base64Url(new Uint8Array(ciphertext))}`;
}

async function decryptEnvelope(
  env: AuthEnv,
  envelope: string,
  kind: "session" | "code",
  provider: LegacyAuthProvider,
  transactionId: string,
): Promise<Record<string, unknown>> {
  const parts = envelope.split(".");
  if (
    parts.length !== 3 ||
    parts[0] !== "v1" ||
    utf8Bytes(envelope) > 400_000
  ) {
    throw new Error("invalid envelope");
  }
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: cryptoBytes(decodeBase64Url(parts[1])),
      additionalData: cryptoBytes(
        envelopeContext(kind, provider, transactionId),
      ),
    },
    await encryptionKey(env),
    cryptoBytes(decodeBase64Url(parts[2])),
  );
  const decoded: unknown = JSON.parse(new TextDecoder().decode(plaintext));
  if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) {
    throw new Error("invalid envelope payload");
  }
  return decoded as Record<string, unknown>;
}

function randomOpaqueSecret(bytes = 48): string {
  return base64Url(crypto.getRandomValues(new Uint8Array(bytes)));
}

function validClientState(value: string | null): boolean {
  return (
    value === null ||
    (utf8Bytes(value) <= MAX_CLIENT_STATE_BYTES &&
      !/[\u0000-\u001f\u007f]/.test(value))
  );
}

function validCredential(value: unknown): value is string {
  return (
    typeof value === "string" &&
    utf8Bytes(value) > 0 &&
    utf8Bytes(value) <= MAX_CREDENTIAL_BYTES &&
    !/[\u0000-\u001f\u007f]/.test(value)
  );
}

function publicBaseUrl(env: AuthEnv): URL {
  const value = env.NATIVE_AUTH_PUBLIC_BASE_URL?.trim();
  if (!value || utf8Bytes(value) > 2_048) {
    throw new NativeAuthError(503, "native_auth_unavailable");
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
      throw new Error("unsafe base URL");
    }
    parsed.pathname = parsed.pathname.replace(/\/+$/, "");
    return parsed;
  } catch {
    throw new NativeAuthError(503, "native_auth_unavailable");
  }
}

function providerConfiguration(
  env: AuthEnv,
  provider: LegacyAuthProvider,
  surface: NativeAuthCompatibilityOptions["surface"] = "namespaced",
): ProviderConfiguration {
  const clientId =
    provider === "google" ? env.GOOGLE_CLIENT_ID : env.APPLE_CLIENT_ID;
  const clientSecret =
    provider === "google" ? env.GOOGLE_CLIENT_SECRET : env.APPLE_CLIENT_SECRET;
  const appleDynamicSecretConfigured =
    provider === "apple" &&
    Boolean(
      env.APPLE_TEAM_ID?.trim() &&
      env.APPLE_KEY_ID?.trim() &&
      env.APPLE_PRIVATE_KEY?.trim(),
    );
  if (
    !clientId?.trim() ||
    (provider === "google" && !clientSecret?.trim()) ||
    (provider === "apple" &&
      !clientSecret?.trim() &&
      !appleDynamicSecretConfigured)
  ) {
    throw new NativeAuthError(503, "provider_not_configured");
  }
  const base = publicBaseUrl(env);
  const callbackUri = new URL(
    `${surface === "legacy" ? "/v1/auth/callback" : "/v2/cf/auth/callback"}/${provider}`,
    `${base.toString().replace(/\/$/, "")}/`,
  ).toString();
  return {
    provider,
    clientId: clientId.trim(),
    clientSecret: clientSecret?.trim() || null,
    authorizationEndpoint:
      provider === "google"
        ? "https://accounts.google.com/o/oauth2/v2/auth"
        : "https://appleid.apple.com/auth/authorize",
    tokenEndpoint:
      provider === "google"
        ? "https://oauth2.googleapis.com/token"
        : "https://appleid.apple.com/auth/token",
    callbackUri,
  };
}

function pemBytes(pem: string): Uint8Array {
  const normalized = pem.replaceAll("\\n", "\n");
  const begin = "-----BEGIN PRIVATE KEY-----";
  const end = "-----END PRIVATE KEY-----";
  const start = normalized.indexOf(begin);
  const finish = normalized.indexOf(end);
  if (start < 0 || finish <= start) throw new Error("invalid Apple key");
  const encoded = normalized
    .slice(start + begin.length, finish)
    .replace(/\s+/g, "");
  if (!encoded || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) {
    throw new Error("invalid Apple key");
  }
  const binary = atob(encoded);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (bytes.byteLength < 32) throw new Error("invalid Apple key");
  return bytes;
}

async function appleClientSecret(
  env: AuthEnv,
  configuration: ProviderConfiguration,
): Promise<string> {
  const teamId = env.APPLE_TEAM_ID?.trim();
  const keyId = env.APPLE_KEY_ID?.trim();
  const privateKey = env.APPLE_PRIVATE_KEY?.trim();
  if (!teamId || !keyId || !privateKey) {
    if (configuration.clientSecret) return configuration.clientSecret;
    throw new NativeAuthError(503, "provider_not_configured");
  }
  if (
    !/^[A-Za-z0-9]{1,64}$/.test(teamId) ||
    !/^[A-Za-z0-9]{1,64}$/.test(keyId)
  ) {
    throw new NativeAuthError(503, "provider_not_configured");
  }
  const now = Math.floor(Date.now() / 1_000);
  const header = base64Url(
    new TextEncoder().encode(
      JSON.stringify({ alg: "ES256", kid: keyId, typ: "JWT" }),
    ),
  );
  const payload = base64Url(
    new TextEncoder().encode(
      JSON.stringify({
        iss: teamId,
        iat: now,
        exp: now + 15 * 60,
        aud: "https://appleid.apple.com",
        sub: configuration.clientId,
      }),
    ),
  );
  try {
    const key = await crypto.subtle.importKey(
      "pkcs8",
      cryptoBytes(pemBytes(privateKey)),
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["sign"],
    );
    const signature = await crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" },
      key,
      new TextEncoder().encode(`${header}.${payload}`),
    );
    return `${header}.${payload}.${base64Url(new Uint8Array(signature))}`;
  } catch (error) {
    throw new NativeAuthError(503, "provider_not_configured");
  }
}

function providerFromCode(code: string): LegacyAuthProvider | null {
  const match = /^cf-(google|apple)-([A-Za-z0-9_-]{43,128})$/.exec(code);
  return match ? (match[1] as LegacyAuthProvider) : null;
}

function responseError(
  c: NativeAuthContext,
  error: NativeAuthError | string,
  status?: 400 | 404 | 409 | 502 | 503,
): Response {
  const resolvedStatus =
    error instanceof NativeAuthError ? error.status : status || 503;
  const code = error instanceof NativeAuthError ? error.code : error;
  c.header("cache-control", "no-store");
  // The exact legacy auth contract uses FastAPI's `{detail: ...}` error
  // envelope. Keep the namespaced seam's stable machine-readable `{error}`
  // response while matching that wire shape when this handler is reached via
  // `/v1/auth/*`.
  return c.json(
    isExactLegacyAuthRequest(c)
      ? { detail: legacyErrorDetail(code) }
      : { error: code },
    resolvedStatus,
  );
}

function legacyErrorDetail(code: string): string {
  switch (code) {
    case "unsupported_provider":
      return "Unsupported provider";
    case "invalid_redirect_uri":
      return "Invalid redirect_uri";
    case "invalid_state":
      return "Invalid auth session";
    case "invalid_pkce":
      return "Invalid PKCE parameters";
    case "invalid_code":
    case "invalid_or_expired_code":
      return "Invalid or expired code";
    case "invalid_request":
      return "Invalid request";
    case "firebase_bridge_unavailable":
      return "Failed to generate authentication token";
    default:
      return code;
  }
}

function isExactLegacyAuthRequest(c: NativeAuthContext): boolean {
  return new URL(c.req.url).pathname.startsWith("/v1/auth/");
}

function callbackHtml(
  title: string,
  message: string,
  redirectUrl?: string,
  status: 200 | 400 | 502 | 503 = 200,
  autoRedirect = false,
): string {
  const escapeHtml = (value: string) =>
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
  const link = redirectUrl
    ? `<p><a href="${escapeHtml(redirectUrl)}">Continue in Omi</a></p>`
    : "";
  const redirectScript =
    autoRedirect && redirectUrl
      ? `<script>window.location.assign(${JSON.stringify(redirectUrl)
          .replaceAll("<", "\\u003c")
          .replaceAll(">", "\\u003e")
          .replaceAll("&", "\\u0026")});</script>`
      : "";
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(title)}</title></head><body><h1>${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p>${link}${redirectScript}</body></html>`;
}

function callbackResponse(
  c: NativeAuthContext,
  title: string,
  message: string,
  redirectUrl?: string,
  status: 200 | 400 | 502 | 503 = 200,
): Response {
  c.header("cache-control", "no-store");
  c.header(
    "content-security-policy",
    `default-src 'none'; style-src 'unsafe-inline';${
      isExactLegacyAuthRequest(c) && status === 200
        ? " script-src 'unsafe-inline';"
        : ""
    } base-uri 'none'; frame-ancestors 'none'`,
  );
  if (isExactLegacyAuthRequest(c) && status !== 200) {
    return c.json({ detail: message }, status);
  }
  return c.html(
    callbackHtml(
      title,
      message,
      redirectUrl,
      status,
      isExactLegacyAuthRequest(c) && status === 200,
    ),
    status,
  );
}

function redirectWithCode(
  redirectUri: string,
  code: string,
  state: string | null,
): string {
  const parsed = new URL(redirectUri);
  parsed.searchParams.set("code", code);
  if (state !== null) parsed.searchParams.set("state", state);
  return parsed.toString();
}

function providerAuthorizationUrl(
  configuration: ProviderConfiguration,
  state: string,
): string {
  const url = new URL(configuration.authorizationEndpoint);
  url.search = new URLSearchParams(
    configuration.provider === "google"
      ? {
          client_id: configuration.clientId,
          redirect_uri: configuration.callbackUri,
          response_type: "code",
          scope: "openid email profile",
          state,
          access_type: "offline",
        }
      : {
          client_id: configuration.clientId,
          redirect_uri: configuration.callbackUri,
          response_type: "code",
          scope: "name email",
          response_mode: "form_post",
          state,
        },
  ).toString();
  return url.toString();
}

function providerErrorResponse(
  response: Response,
  provider: LegacyAuthProvider,
): NativeAuthError {
  // Do not read or include provider response text: provider error bodies can
  // contain tokens or unbounded attacker-controlled data.
  return new NativeAuthError(
    response.status >= 500 ? 503 : 502,
    `${provider}_provider_unavailable`,
  );
}

async function providerCredentials(
  env: AuthEnv,
  configuration: ProviderConfiguration,
  code: string,
  fetchImpl: FetchLike,
): Promise<ProviderCredentials> {
  if (!isValidLegacyOpaqueSecret(code) || utf8Bytes(code) > 2_048) {
    throw new NativeAuthError(400, "invalid_provider_code");
  }
  const secret =
    configuration.provider === "apple"
      ? await appleClientSecret(env, configuration)
      : configuration.clientSecret;
  if (!secret) throw new NativeAuthError(503, "provider_not_configured");
  const form = new URLSearchParams({
    code,
    client_id: configuration.clientId,
    client_secret: secret,
    redirect_uri: configuration.callbackUri,
    grant_type: "authorization_code",
  });
  let response: Response;
  try {
    response = await fetchImpl(configuration.tokenEndpoint, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
  } catch {
    throw new NativeAuthError(
      503,
      `${configuration.provider}_provider_unavailable`,
    );
  }
  const contentLength = Number(response.headers.get("content-length") || "0");
  if (
    Number.isFinite(contentLength) &&
    contentLength > MAX_PROVIDER_RESPONSE_BYTES
  ) {
    throw new NativeAuthError(503, "provider_response_too_large");
  }
  if (!response.ok)
    throw providerErrorResponse(response, configuration.provider);
  let body: unknown;
  try {
    const raw = await response.text();
    if (utf8Bytes(raw) > MAX_PROVIDER_RESPONSE_BYTES) {
      throw new Error("provider response too large");
    }
    body = JSON.parse(raw);
  } catch {
    throw new NativeAuthError(503, "provider_response_invalid");
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new NativeAuthError(503, "provider_response_invalid");
  }
  const data = body as Record<string, unknown>;
  const idToken = data.id_token;
  const accessToken = data.access_token;
  const expiresIn = data.expires_in;
  const expires =
    typeof expiresIn === "number" ? expiresIn : Number(expiresIn || 3_600);
  if (!validCredential(idToken)) {
    throw new NativeAuthError(503, "provider_response_invalid");
  }
  if (configuration.provider === "google" && !validCredential(accessToken)) {
    throw new NativeAuthError(503, "provider_response_invalid");
  }
  if (!Number.isSafeInteger(expires) || expires < 1 || expires > 86_400) {
    throw new NativeAuthError(503, "provider_response_invalid");
  }
  return {
    provider: configuration.provider,
    idToken,
    accessToken: validCredential(accessToken) ? accessToken : null,
    expiresIn: expires,
  };
}

function parseAppleName(value: string | null): string | undefined {
  if (!value || utf8Bytes(value) > 4_096) return undefined;
  try {
    const decoded: unknown = JSON.parse(value);
    const name =
      decoded && typeof decoded === "object" && !Array.isArray(decoded)
        ? (decoded as Record<string, unknown>).name
        : null;
    if (!name || typeof name !== "object" || Array.isArray(name))
      return undefined;
    const first = (name as Record<string, unknown>).firstName;
    const last = (name as Record<string, unknown>).lastName;
    const parts = [first, last].filter(
      (part): part is string =>
        typeof part === "string" && part.trim().length > 0,
    );
    const full = parts.join(" ").trim();
    return full && utf8Bytes(full) <= MAX_NAME_BYTES ? full : undefined;
  } catch {
    return undefined;
  }
}

async function readBoundedForm(
  request: Request,
  maxBytes: number,
): Promise<URLSearchParams | null> {
  const contentLength = Number(request.headers.get("content-length") || "0");
  if (
    !Number.isFinite(contentLength) ||
    contentLength < 0 ||
    contentLength > maxBytes
  ) {
    return null;
  }
  let raw: string;
  try {
    raw = await request.text();
  } catch {
    return null;
  }
  if (utf8Bytes(raw) > maxBytes) return null;
  return new URLSearchParams(raw);
}

function queryString(value: string | undefined | null): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

async function authorize(
  c: NativeAuthContext,
  dependencies: NativeAuthCompatibilityDependencies,
  surface: NativeAuthCompatibilityOptions["surface"],
): Promise<Response> {
  const enabled =
    surface === "legacy"
      ? c.env.LEGACY_AUTH_EXACT_STAGING_ENABLED === "true"
      : c.env.LEGACY_AUTH_COMPAT_STAGING_ENABLED === "true";
  if (!enabled) {
    return responseError(c, new NativeAuthError(404, "not_found"));
  }
  const providerValue = c.req.query("provider");
  if (providerValue !== "google" && providerValue !== "apple") {
    return responseError(c, new NativeAuthError(400, "unsupported_provider"));
  }
  const provider = providerValue as LegacyAuthProvider;
  const redirectUri = c.req.query("redirect_uri") || "";
  const clientState = c.req.query("state") || null;
  const codeChallenge = c.req.query("code_challenge") || "";
  const codeChallengeMethod = c.req.query("code_challenge_method") || "";
  if (!isValidLegacyRedirectUri(redirectUri)) {
    return responseError(c, new NativeAuthError(400, "invalid_redirect_uri"));
  }
  if (!validClientState(clientState)) {
    return responseError(c, new NativeAuthError(400, "invalid_state"));
  }
  if (!isValidLegacyPkceChallenge(codeChallenge, codeChallengeMethod)) {
    return responseError(c, new NativeAuthError(400, "invalid_pkce"));
  }
  const now = nowSeconds(dependencies);
  if (!now)
    return responseError(c, new NativeAuthError(503, "clock_unavailable"));

  await bestEffortPruneTransactions(c.env.AUTH_DB, now);

  try {
    const configuration = providerConfiguration(c.env, provider, surface);
    transactionSecret(c.env);
    const transactionId = crypto.randomUUID();
    const stateSecret = randomOpaqueSecret();
    const metadataEnvelope = await encryptEnvelope(
      c.env,
      "session",
      provider,
      transactionId,
      { redirectUri, clientState, provider },
    );
    await createLegacyAuthTransaction(c.env.AUTH_DB, {
      id: transactionId,
      kind: "session",
      provider,
      lookupSecret: stateSecret,
      stateSecret,
      redirectUri,
      codeChallenge,
      codeChallengeMethod,
      metadataEnvelopeEnc: metadataEnvelope,
      createdAt: now,
      expiresAt: now + SESSION_TTL_SECONDS,
    });
    return c.redirect(
      providerAuthorizationUrl(configuration, stateSecret),
      302,
    );
  } catch (error) {
    return responseError(
      c,
      error instanceof NativeAuthError
        ? error
        : new NativeAuthError(503, "transaction_authority_unavailable"),
    );
  }
}

async function callback(
  c: NativeAuthContext,
  provider: LegacyAuthProvider,
  dependencies: NativeAuthCompatibilityDependencies,
  surface: NativeAuthCompatibilityOptions["surface"],
  values: {
    code: string | null;
    state: string | null;
    error: string | null;
    user: string | null;
  },
): Promise<Response> {
  const enabled =
    surface === "legacy"
      ? c.env.LEGACY_AUTH_EXACT_STAGING_ENABLED === "true"
      : c.env.LEGACY_AUTH_COMPAT_STAGING_ENABLED === "true";
  if (!enabled) {
    return responseError(c, new NativeAuthError(404, "not_found"));
  }
  if (!values.state || !isValidLegacyOpaqueSecret(values.state)) {
    return callbackResponse(
      c,
      "Authentication failed",
      "Invalid or expired authentication state.",
      undefined,
      400,
    );
  }
  const now = nowSeconds(dependencies);
  if (!now)
    return callbackResponse(
      c,
      "Authentication failed",
      "Authentication service is unavailable.",
      undefined,
      503,
    );
  await bestEffortPruneTransactions(c.env.AUTH_DB, now);
  let configuration: ProviderConfiguration;
  try {
    configuration = providerConfiguration(c.env, provider, surface);
    transactionSecret(c.env);
  } catch (error) {
    return callbackResponse(
      c,
      "Authentication unavailable",
      "The provider is not configured.",
      undefined,
      503,
    );
  }

  // Consume the provider-facing state before provider exchange.  A provider
  // denial or malformed callback therefore cannot be replayed indefinitely.
  const consumed = await consumeLegacyAuthTransaction(c.env.AUTH_DB, {
    lookupSecret: values.state,
    kind: "session",
    provider,
    now,
  });
  if (!consumed || !consumed.metadataEnvelopeEnc) {
    return callbackResponse(
      c,
      "Authentication failed",
      "Invalid or expired authentication state.",
      undefined,
      400,
    );
  }
  let metadata: SessionMetadata;
  try {
    const decoded = await decryptEnvelope(
      c.env,
      consumed.metadataEnvelopeEnc,
      "session",
      provider,
      consumed.id,
    );
    if (
      typeof decoded.redirectUri !== "string" ||
      !isValidLegacyRedirectUri(decoded.redirectUri) ||
      decoded.redirectUri !== consumed.redirectUri ||
      (decoded.clientState !== null &&
        typeof decoded.clientState !== "string") ||
      !validClientState(
        (decoded.clientState as string | null | undefined) ?? null,
      ) ||
      decoded.provider !== provider
    ) {
      throw new Error("invalid session metadata");
    }
    metadata = {
      redirectUri: decoded.redirectUri,
      clientState: (decoded.clientState as string | null | undefined) ?? null,
      provider,
    };
  } catch {
    return callbackResponse(
      c,
      "Authentication unavailable",
      "The authentication transaction is unavailable.",
      undefined,
      503,
    );
  }
  if (values.error) {
    return callbackResponse(
      c,
      "Authentication cancelled",
      "The provider did not authorize this sign-in.",
      undefined,
      400,
    );
  }
  if (!values.code || !isValidLegacyOpaqueSecret(values.code)) {
    return callbackResponse(
      c,
      "Authentication failed",
      "The provider callback did not include a valid code.",
      undefined,
      400,
    );
  }

  let credentials: ProviderCredentials;
  try {
    credentials = await providerCredentials(
      c.env,
      configuration,
      values.code,
      dependencies.fetchImpl || fetch,
    );
    const fullName =
      provider === "apple" ? parseAppleName(values.user) : undefined;
    if (fullName) credentials.fullName = fullName;
  } catch (error) {
    const failure =
      error instanceof NativeAuthError
        ? error
        : new NativeAuthError(503, "provider_unavailable");
    return callbackResponse(
      c,
      "Authentication unavailable",
      "The identity provider could not be reached.",
      undefined,
      failure.status === 400 ? 400 : failure.status === 502 ? 502 : 503,
    );
  }

  try {
    const transactionId = crypto.randomUUID();
    const authCode = `cf-${provider}-${randomOpaqueSecret()}`;
    const encryptedPayload = await encryptEnvelope(
      c.env,
      "code",
      provider,
      transactionId,
      {
        provider,
        id_token: credentials.idToken,
        access_token: credentials.accessToken,
        expires_in: credentials.expiresIn,
        ...(credentials.fullName ? { full_name: credentials.fullName } : {}),
      },
    );
    await createLegacyAuthTransaction(c.env.AUTH_DB, {
      id: transactionId,
      kind: "code",
      provider,
      lookupSecret: authCode,
      stateSecret: values.state,
      redirectUri: metadata.redirectUri,
      codeChallenge: consumed.codeChallenge,
      codeChallengeMethod: "S256",
      encryptedPayload,
      createdAt: now,
      expiresAt: now + CODE_TTL_SECONDS,
    });
    const redirectUrl = redirectWithCode(
      metadata.redirectUri,
      authCode,
      metadata.clientState,
    );
    return callbackResponse(
      c,
      "Authentication ready",
      "Continue in the Omi app to finish signing in.",
      redirectUrl,
      200,
    );
  } catch {
    return callbackResponse(
      c,
      "Authentication unavailable",
      "The authentication transaction could not be saved.",
      undefined,
      503,
    );
  }
}

async function token(
  c: NativeAuthContext,
  dependencies: NativeAuthCompatibilityDependencies,
  surface: NativeAuthCompatibilityOptions["surface"],
): Promise<Response> {
  const enabled =
    surface === "legacy"
      ? c.env.LEGACY_AUTH_EXACT_STAGING_ENABLED === "true"
      : c.env.LEGACY_AUTH_COMPAT_STAGING_ENABLED === "true";
  if (!enabled) {
    return responseError(c, new NativeAuthError(404, "not_found"));
  }
  const contentType = c.req.header("content-type") || "";
  let body: Record<string, unknown>;
  if (contentType.includes("application/json")) {
    const contentLength = Number(c.req.header("content-length") || "0");
    if (
      !Number.isFinite(contentLength) ||
      contentLength > MAX_TOKEN_BODY_BYTES
    ) {
      return responseError(c, new NativeAuthError(400, "invalid_request"));
    }
    try {
      const raw = await c.req.text();
      if (utf8Bytes(raw) > MAX_TOKEN_BODY_BYTES) throw new Error("too large");
      const parsed: unknown = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
        throw new Error("invalid body");
      body = parsed as Record<string, unknown>;
    } catch {
      return responseError(c, new NativeAuthError(400, "invalid_request"));
    }
  } else {
    const form = await readBoundedForm(c.req.raw, MAX_TOKEN_BODY_BYTES);
    if (!form)
      return responseError(c, new NativeAuthError(400, "invalid_request"));
    body = Object.fromEntries(form.entries());
  }
  const grantType = body.grant_type;
  const code = body.code;
  const redirectUri = body.redirect_uri;
  const codeVerifier = body.code_verifier;
  const useCustomToken =
    body.use_custom_token === true ||
    body.use_custom_token === "true" ||
    body.use_custom_token === "1";
  if (
    grantType !== "authorization_code" ||
    typeof code !== "string" ||
    typeof redirectUri !== "string" ||
    typeof codeVerifier !== "string"
  ) {
    return responseError(c, new NativeAuthError(400, "invalid_request"));
  }
  const provider = providerFromCode(code);
  if (!provider)
    return responseError(c, new NativeAuthError(400, "invalid_code"));
  if (!isValidLegacyRedirectUri(redirectUri)) {
    return responseError(c, new NativeAuthError(400, "invalid_redirect_uri"));
  }
  if (!isValidLegacyOpaqueSecret(codeVerifier)) {
    return responseError(c, new NativeAuthError(400, "invalid_pkce"));
  }
  const now = nowSeconds(dependencies);
  if (!now)
    return responseError(c, new NativeAuthError(503, "clock_unavailable"));
  await bestEffortPruneTransactions(c.env.AUTH_DB, now);
  try {
    const consumed = await consumeLegacyAuthTransaction(c.env.AUTH_DB, {
      lookupSecret: code,
      kind: "code",
      provider,
      redirectUri,
      codeVerifier,
      now,
    });
    if (!consumed?.encryptedPayload) {
      return responseError(
        c,
        new NativeAuthError(400, "invalid_or_expired_code"),
      );
    }
    const decoded = await decryptEnvelope(
      c.env,
      consumed.encryptedPayload,
      "code",
      provider,
      consumed.id,
    );
    if (
      decoded.provider !== provider ||
      !validCredential(decoded.id_token) ||
      (decoded.access_token !== null &&
        decoded.access_token !== undefined &&
        !validCredential(decoded.access_token))
    ) {
      throw new Error("invalid provider payload");
    }
    const expiresIn = Number(decoded.expires_in || 3_600);
    if (
      !Number.isSafeInteger(expiresIn) ||
      expiresIn < 1 ||
      expiresIn > 86_400
    ) {
      throw new Error("invalid expiry");
    }
    const response: Record<string, unknown> = {
      provider,
      id_token: decoded.id_token,
      access_token: decoded.access_token ?? null,
      provider_id: provider === "google" ? "google.com" : "apple.com",
      token_type: "Bearer",
      expires_in: LEGACY_RESPONSE_EXPIRES_IN_SECONDS,
    };
    if (useCustomToken) {
      if (!c.env.FIREBASE_API_KEY || !c.env.FIREBASE_SERVICE_ACCOUNT_JSON) {
        return responseError(
          c,
          new NativeAuthError(503, "firebase_bridge_unavailable"),
        );
      }
      try {
        const firebaseExchange = await exchangeFirebaseProviderCredential(
          provider,
          decoded.id_token as string,
          decoded.access_token === null || decoded.access_token === undefined
            ? null
            : (decoded.access_token as string),
          c.env,
          dependencies.fetchImpl || fetch,
        );
        const identity = await resolveFirebaseIdentityByFirebaseUid(
          c.env.AUTH_DB,
          firebaseExchange.localId,
          provider,
        );
        const issued = await issueFirebaseCustomToken(
          c.env.AUTH_DB,
          identity.betterAuthUserId,
          c.env,
          now,
          identity.generation,
        );
        response.custom_token = issued.token;
      } catch (error) {
        if (error instanceof FirebaseCustomTokenBridgeError) {
          const status =
            error.code === "identity_not_admitted" ||
            error.code === "provider_identity_mismatch" ||
            error.code === "deletion_fence_active" ||
            error.code === "account_generation_conflict"
              ? 409
              : error.code === "provider_rejected"
                ? 502
                : 503;
          return responseError(c, new NativeAuthError(status, error.code));
        }
        return responseError(
          c,
          new NativeAuthError(503, "firebase_bridge_unavailable"),
        );
      }
    }
    c.header("cache-control", "no-store");
    return c.json(response);
  } catch (error) {
    if (error instanceof NativeAuthError) return responseError(c, error);
    return responseError(
      c,
      new NativeAuthError(503, "transaction_authority_unavailable"),
    );
  }
}

export function registerNativeAuthCompatibilityRoutes(
  app: NativeAuthApp,
  dependencies: NativeAuthCompatibilityDependencies = {},
  options: NativeAuthCompatibilityOptions = {},
): void {
  const surface = options.surface || "namespaced";
  const prefix = surface === "legacy" ? "/v1/auth" : "/v2/cf/auth";
  app.get(`${prefix}/authorize`, (c) => authorize(c, dependencies, surface));
  app.get(`${prefix}/callback/google`, (c) =>
    callback(c, "google", dependencies, surface, {
      code: queryString(c.req.query("code")),
      state: queryString(c.req.query("state")),
      error: queryString(c.req.query("error")),
      user: null,
    }),
  );
  app.post(`${prefix}/callback/apple`, async (c) => {
    const form = await readBoundedForm(c.req.raw, MAX_CALLBACK_BODY_BYTES);
    if (!form) {
      return callbackResponse(
        c,
        "Authentication failed",
        "The provider callback was too large.",
        undefined,
        400,
      );
    }
    return callback(c, "apple", dependencies, surface, {
      code: queryString(form.get("code")),
      state: queryString(form.get("state")),
      error: queryString(form.get("error")),
      user: queryString(form.get("user")),
    });
  });
  app.post(`${prefix}/token`, (c) => token(c, dependencies, surface));
}
