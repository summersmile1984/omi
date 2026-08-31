/**
 * Firebase custom-token bridge for imported identities.
 *
 * This module deliberately has no legacy-route wiring.  It is an isolated
 * authority that can be exercised by an authenticated internal caller after
 * the Firebase export has been imported and reconciled.  The bridge never
 * trusts a caller-supplied Firebase uid: it resolves the uid and the current
 * account generation from the Auth D1 projection and deletion fence.
 */

const FIREBASE_CUSTOM_TOKEN_AUDIENCE =
  "https://identitytoolkit.googleapis.com/google.identity.identitytoolkit.v1.IdentityToolkit";
const FIREBASE_SIGN_IN_URL =
  "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken";
const DEFAULT_TOKEN_TTL_SECONDS = 300;
const MAX_TOKEN_TTL_SECONDS = 3_600;
const MAX_SERVICE_ACCOUNT_BYTES = 32_000;
const MAX_API_KEY_BYTES = 512;
const MAX_UID_BYTES = 256;
const MAX_ERROR_BYTES = 128;

export type FirebaseCustomTokenBridgeEnv = {
  FIREBASE_API_KEY?: string;
  FIREBASE_PROJECT_ID?: string;
  FIREBASE_SERVICE_ACCOUNT_JSON?: string;
  FIREBASE_CUSTOM_TOKEN_TTL_SECONDS?: string;
};

export type FirebaseServiceAccount = {
  projectId: string;
  clientEmail: string;
  privateKey: string;
  privateKeyId?: string;
};

export type FirebaseIdentityBridgeRow = {
  firebaseUid: string;
  betterAuthUserId: string;
  providers: string[];
  importStatus: "completed" | "applying";
  projectionStatus: "imported" | "revoked" | "conflict";
  generation: number;
  fenceStatus: "clear" | "deleting" | "deleted";
};

export type FirebaseCustomTokenResult = {
  token: string;
  issuanceId: string;
  firebaseUid: string;
  accountGeneration: number;
  expiresAt: number;
};

export type FirebaseTokenExchangeResult = {
  idToken: string;
  refreshToken: string;
  expiresIn: number;
  localId: string;
};

export type FirebaseBridgeFailureCode =
  | "bridge_unavailable"
  | "identity_not_admitted"
  | "account_generation_conflict"
  | "deletion_fence_active"
  | "provider_unavailable"
  | "provider_rejected";

export class FirebaseCustomTokenBridgeError extends Error {
  readonly code: FirebaseBridgeFailureCode;

  constructor(code: FirebaseBridgeFailureCode, message = code) {
    super(message);
    this.name = "FirebaseCustomTokenBridgeError";
    this.code = code;
  }
}

type D1Statement = {
  bind(...values: unknown[]): D1Statement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  run(): Promise<{ success?: boolean; meta?: { changes?: number | bigint } }>;
};

export type FirebaseBridgeDatabase = {
  prepare(sql: string): D1Statement;
};

type FirebaseIdentityRow = Record<string, unknown>;

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function base64Url(value: string | Uint8Array): string {
  const bytes =
    typeof value === "string" ? new TextEncoder().encode(value) : value;
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}

function decodeBase64(value: string): Uint8Array {
  const normalized = value.replace(/\s+/g, "").replaceAll("-", "+").replaceAll("_", "/");
  if (!normalized || !/^[A-Za-z0-9+/]*={0,2}$/.test(normalized)) {
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
  const padded = normalized + "===".slice((normalized.length + 3) % 4);
  let binary: string;
  try {
    binary = atob(padded);
  } catch {
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function privateKeyBytes(pem: string): Uint8Array {
  const begin = "-----BEGIN PRIVATE KEY-----";
  const end = "-----END PRIVATE KEY-----";
  if (!pem.includes(begin) || !pem.includes(end)) {
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
  const encoded = pem.slice(pem.indexOf(begin) + begin.length, pem.indexOf(end));
  return decodeBase64(encoded);
}

function validProjectId(value: unknown): value is string {
  return typeof value === "string" && /^[a-z0-9][a-z0-9-]{2,62}$/.test(value);
}

function validClientEmail(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length <= 320 &&
    /^[^\s@]+@[^\s@]+$/.test(value)
  );
}

function validUid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    utf8Bytes(value) >= 1 &&
    utf8Bytes(value) <= MAX_UID_BYTES &&
    !/[\u0000-\u001f\u007f]/.test(value)
  );
}

function validApiKey(value: unknown): value is string {
  return (
    typeof value === "string" &&
    utf8Bytes(value) <= MAX_API_KEY_BYTES &&
    /^[A-Za-z0-9_-]{8,512}$/.test(value)
  );
}

function strictInteger(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function configuredTtl(env: FirebaseCustomTokenBridgeEnv): number {
  if (env.FIREBASE_CUSTOM_TOKEN_TTL_SECONDS === undefined) {
    return DEFAULT_TOKEN_TTL_SECONDS;
  }
  const ttl = strictInteger(env.FIREBASE_CUSTOM_TOKEN_TTL_SECONDS);
  if (!ttl || ttl < 60 || ttl > MAX_TOKEN_TTL_SECONDS) {
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
  return ttl;
}

export function parseFirebaseServiceAccount(
  value: string | undefined,
): FirebaseServiceAccount | null {
  if (!value || utf8Bytes(value) > MAX_SERVICE_ACCOUNT_BYTES) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const object = parsed as Record<string, unknown>;
  const projectId = object.project_id;
  const clientEmail = object.client_email;
  const privateKey = object.private_key;
  const privateKeyId = object.private_key_id;
  if (
    !validProjectId(projectId) ||
    !validClientEmail(clientEmail) ||
    typeof privateKey !== "string" ||
    utf8Bytes(privateKey) > 8_192 ||
    !privateKey.includes("-----BEGIN PRIVATE KEY-----") ||
    !privateKey.includes("-----END PRIVATE KEY-----") ||
    (privateKeyId !== undefined &&
      (typeof privateKeyId !== "string" || !/^[A-Za-z0-9_-]{1,256}$/.test(privateKeyId)))
  ) {
    return null;
  }
  try {
    // A Firebase service account uses an RSA PKCS#8 key.  Reject a syntactic
    // PEM wrapper around a tiny/malformed base64 blob before it can reach the
    // signing path.
    if (privateKeyBytes(privateKey).byteLength < 256) return null;
  } catch {
    return null;
  }
  return {
    projectId,
    clientEmail,
    privateKey,
    ...(typeof privateKeyId === "string" ? { privateKeyId } : {}),
  };
}

function serviceAccount(env: FirebaseCustomTokenBridgeEnv): FirebaseServiceAccount {
  const account = parseFirebaseServiceAccount(env.FIREBASE_SERVICE_ACCOUNT_JSON);
  if (!account) throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  if (env.FIREBASE_PROJECT_ID !== undefined && env.FIREBASE_PROJECT_ID !== account.projectId) {
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
  return account;
}

function parseProviders(value: unknown): string[] | null {
  let parsed = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (
    !Array.isArray(parsed) ||
    parsed.length === 0 ||
    parsed.length > 16 ||
    parsed.some((provider) => typeof provider !== "string" || provider.length === 0)
  ) {
    return null;
  }
  const providers = parsed as string[];
  return new Set(providers).size === providers.length ? providers : null;
}

function normalizedIdentityRow(row: FirebaseIdentityRow): FirebaseIdentityBridgeRow | null {
  const firebaseUid = row.firebaseUid;
  const betterAuthUserId = row.betterAuthUserId;
  const providers = parseProviders(row.providersJson);
  const generation = strictInteger(row.generation);
  if (
    !validUid(firebaseUid) ||
    !validUid(betterAuthUserId) ||
    !providers ||
    row.importStatus !== "completed" ||
    row.projectionStatus !== "imported" ||
    !generation ||
    generation < 1 ||
    (row.fenceStatus !== "clear" &&
      row.fenceStatus !== "deleting" &&
      row.fenceStatus !== "deleted")
  ) {
    return null;
  }
  return {
    firebaseUid,
    betterAuthUserId,
    providers,
    importStatus: row.importStatus,
    projectionStatus: row.projectionStatus,
    generation,
    fenceStatus: row.fenceStatus,
  };
}

/** Resolve only a completed imported identity with an explicit fence row. */
export async function resolveFirebaseIdentityBridge(
  database: FirebaseBridgeDatabase,
  betterAuthUserId: string,
): Promise<FirebaseIdentityBridgeRow> {
  if (!validUid(betterAuthUserId)) {
    throw new FirebaseCustomTokenBridgeError("identity_not_admitted");
  }
  let row: FirebaseIdentityRow | null;
  try {
    row = await database
      .prepare(
        `SELECT p.firebaseUid, p.betterAuthUserId, p.providersJson,
                p.status AS projectionStatus, i.status AS importStatus,
                f.generation, f.status AS fenceStatus
           FROM cf_firebase_identity_projection AS p
           JOIN auth_identity_imports AS i ON i.id = p.sourceImportId
           JOIN cf_auth_deletion_fences AS f
             ON f.uid = p.betterAuthUserId
          WHERE p.betterAuthUserId = ?
          LIMIT 1`,
      )
      .bind(betterAuthUserId)
      .first<FirebaseIdentityRow>();
  } catch {
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
  const identity = row ? normalizedIdentityRow(row) : null;
  if (!identity) {
    throw new FirebaseCustomTokenBridgeError("identity_not_admitted");
  }
  if (identity.fenceStatus !== "clear") {
    throw new FirebaseCustomTokenBridgeError("deletion_fence_active");
  }
  return identity;
}

async function signCustomToken(
  account: FirebaseServiceAccount,
  identity: FirebaseIdentityBridgeRow,
  issuanceId: string,
  now: number,
  expiresAt: number,
): Promise<string> {
  const header = {
    alg: "RS256",
    typ: "JWT",
    ...(account.privateKeyId ? { kid: account.privateKeyId } : {}),
  };
  const payload = {
    iss: account.clientEmail,
    sub: account.clientEmail,
    aud: FIREBASE_CUSTOM_TOKEN_AUDIENCE,
    iat: now,
    exp: expiresAt,
    uid: identity.firebaseUid,
    claims: {
      omi_account_generation: identity.generation,
      omi_bridge: "firebase-v1",
      jti: issuanceId,
    },
  };
  const unsigned = `${base64Url(JSON.stringify(header))}.${base64Url(JSON.stringify(payload))}`;
  let key: CryptoKey;
  try {
    key = await crypto.subtle.importKey(
      "pkcs8",
      privateKeyBytes(account.privateKey) as BufferSource,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const signature = await crypto.subtle.sign(
      "RSASSA-PKCS1-v1_5",
      key,
      new TextEncoder().encode(unsigned),
    );
    return `${unsigned}.${base64Url(new Uint8Array(signature))}`;
  } catch {
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
}

async function tokenHash(token: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(token),
  );
  return base64Url(new Uint8Array(digest));
}

function changes(result: { meta?: { changes?: number | bigint } }): number {
  const value = result.meta?.changes;
  return typeof value === "bigint" ? Number(value) : Number(value || 0);
}

/**
 * Mint a short-lived Firebase custom token and record only a token hash.
 * Reservation/issuance rows are fenced in SQL, so deletion cannot race the
 * final issuance update and receive a token after the fence becomes active.
 */
export async function issueFirebaseCustomToken(
  database: FirebaseBridgeDatabase,
  betterAuthUserId: string,
  env: FirebaseCustomTokenBridgeEnv,
  now = Math.floor(Date.now() / 1_000),
  expectedGeneration?: number,
): Promise<FirebaseCustomTokenResult> {
  const identity = await resolveFirebaseIdentityBridge(database, betterAuthUserId);
  if (
    expectedGeneration !== undefined &&
    (!Number.isSafeInteger(expectedGeneration) || expectedGeneration !== identity.generation)
  ) {
    throw new FirebaseCustomTokenBridgeError("account_generation_conflict");
  }
  if (!Number.isSafeInteger(now) || now <= 0) {
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
  const account = serviceAccount(env);
  const ttl = configuredTtl(env);
  const expiresAt = now + ttl;
  const issuanceId = crypto.randomUUID();
  try {
    const reserved = await database
      .prepare(
        `INSERT INTO cf_firebase_bridge_issuances
           (issuanceId, firebaseUid, betterAuthUserId, accountGeneration,
            status, issuedAt, expiresAt, tokenHash, lastError)
         VALUES (?, ?, ?, ?, 'reserved', ?, ?, NULL, NULL)`,
      )
      .bind(
        issuanceId,
        identity.firebaseUid,
        identity.betterAuthUserId,
        identity.generation,
        now,
        expiresAt,
      )
      .run();
    if (changes(reserved) !== 1) {
      throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
    }
  } catch (error) {
    if (error instanceof FirebaseCustomTokenBridgeError) throw error;
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }

  try {
    const token = await signCustomToken(account, identity, issuanceId, now, expiresAt);
    const hash = await tokenHash(token);
    const updated = await database
      .prepare(
        `UPDATE cf_firebase_bridge_issuances
            SET status = 'issued', tokenHash = ?
          WHERE issuanceId = ? AND betterAuthUserId = ?
            AND accountGeneration = ? AND status = 'reserved'`,
      )
      .bind(hash, issuanceId, identity.betterAuthUserId, identity.generation)
      .run();
    if (changes(updated) !== 1) {
      throw new FirebaseCustomTokenBridgeError("deletion_fence_active");
    }
    return {
      token,
      issuanceId,
      firebaseUid: identity.firebaseUid,
      accountGeneration: identity.generation,
      expiresAt,
    };
  } catch (error) {
    try {
      await database
        .prepare(
          `UPDATE cf_firebase_bridge_issuances
              SET status = 'failed', lastError = ?
            WHERE issuanceId = ? AND status = 'reserved'`,
        )
        .bind(
          error instanceof FirebaseCustomTokenBridgeError
            ? error.code.slice(0, MAX_ERROR_BYTES)
            : "bridge_unavailable",
          issuanceId,
        )
        .run();
    } catch {
      // The token is never returned if the audit update fails.  The reserved
      // row is recoverable by an expiry cleanup/reconciliation pass.
    }
    if (error instanceof FirebaseCustomTokenBridgeError) throw error;
    throw new FirebaseCustomTokenBridgeError("bridge_unavailable");
  }
}

function responseNumber(value: unknown, min: number, max: number): number | null {
  const parsed = strictInteger(value);
  return parsed !== null && parsed >= min && parsed <= max ? parsed : null;
}

/** Exchange a custom token for Firebase ID/refresh tokens through REST. */
export async function exchangeFirebaseCustomToken(
  token: string,
  env: FirebaseCustomTokenBridgeEnv,
  fetcher: typeof fetch = fetch,
): Promise<FirebaseTokenExchangeResult> {
  if (!token || utf8Bytes(token) > 8_192 || !validApiKey(env.FIREBASE_API_KEY)) {
    throw new FirebaseCustomTokenBridgeError("provider_unavailable");
  }
  const endpoint = `${FIREBASE_SIGN_IN_URL}?key=${encodeURIComponent(env.FIREBASE_API_KEY)}`;
  let response: Response;
  try {
    response = await fetcher(endpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token, returnSecureToken: true }),
    });
  } catch {
    throw new FirebaseCustomTokenBridgeError("provider_unavailable");
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new FirebaseCustomTokenBridgeError(
      response.status >= 500 ? "provider_unavailable" : "provider_rejected",
    );
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new FirebaseCustomTokenBridgeError("provider_unavailable");
  }
  const object = body as Record<string, unknown>;
  const idToken = object.idToken;
  const refreshToken = object.refreshToken;
  const localId = object.localId;
  const expiresIn = responseNumber(object.expiresIn, 1, 86_400);
  if (
    typeof idToken !== "string" ||
    !idToken ||
    typeof refreshToken !== "string" ||
    !refreshToken ||
    !validUid(localId) ||
    expiresIn === null
  ) {
    throw new FirebaseCustomTokenBridgeError("provider_unavailable");
  }
  return { idToken, refreshToken, expiresIn, localId };
}

export const firebaseCustomTokenBridgeConstants = Object.freeze({
  customTokenAudience: FIREBASE_CUSTOM_TOKEN_AUDIENCE,
  defaultTokenTtlSeconds: DEFAULT_TOKEN_TTL_SECONDS,
  maxTokenTtlSeconds: MAX_TOKEN_TTL_SECONDS,
});
