/**
 * Firebase anonymous-source attestation for app-owner migration.
 *
 * The caller credential is verified by Firebase Identity Toolkit before its
 * signed claims are inspected.  No Firebase uid or token is persisted or
 * returned: downstream services receive only domain-separated HMAC evidence.
 */

const FIREBASE_LOOKUP_URL =
  "https://identitytoolkit.googleapis.com/v1/accounts:lookup";
const MAX_ID_TOKEN_BYTES = 8_192;
const MAX_LOOKUP_RESPONSE_BYTES = 32_000;
const MAX_UID_BYTES = 256;
const CLOCK_SKEW_SECONDS = 60;
const MAX_TOKEN_LIFETIME_SECONDS = 2 * 60 * 60;

export type FirebaseAnonymousIdentityEnv = {
  FIREBASE_API_KEY?: string;
  FIREBASE_PROJECT_ID?: string;
  FIREBASE_IDENTITY_PROJECTION_SECRET?: string;
};

export type FirebaseAnonymousIdentityAttestation = {
  sourceRef: string;
  sourceUidHash: string;
  sourceProofHash: string;
  sourceCredentialGeneration: number;
  sourceProjectionRevision: string;
  attestedAt: number;
  expiresAt: number;
};

export type FirebaseAnonymousIdentityFailureCode =
  | "bridge_unavailable"
  | "source_identity_rejected"
  | "source_identity_mismatch"
  | "source_identity_revoked";

export class FirebaseAnonymousIdentityError extends Error {
  readonly code: FirebaseAnonymousIdentityFailureCode;

  constructor(code: FirebaseAnonymousIdentityFailureCode) {
    super(code);
    this.name = "FirebaseAnonymousIdentityError";
    this.code = code;
  }
}

type FirebaseLookupUser = {
  localId?: unknown;
  disabled?: unknown;
  providerUserInfo?: unknown;
  validSince?: unknown;
};

type FirebaseClaims = {
  aud?: unknown;
  iss?: unknown;
  sub?: unknown;
  user_id?: unknown;
  iat?: unknown;
  exp?: unknown;
  auth_time?: unknown;
  firebase?: unknown;
};

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
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
    utf8Bytes(value) >= 8 &&
    utf8Bytes(value) <= 512 &&
    /^[A-Za-z0-9_-]+$/.test(value)
  );
}

function validProjectId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[a-z0-9][a-z0-9-]{2,62}$/.test(value)
  );
}

function integer(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function decodeJwtPayload(token: string): FirebaseClaims {
  const parts = token.split(".");
  if (parts.length !== 3 || parts.some((part) => !part)) {
    throw new FirebaseAnonymousIdentityError("source_identity_rejected");
  }
  try {
    const encoded = parts[1].replaceAll("-", "+").replaceAll("_", "/");
    const padded = encoded + "===".slice((encoded.length + 3) % 4);
    const binary = atob(padded);
    if (binary.length > 8_192) throw new Error("payload too large");
    const parsed = JSON.parse(
      new TextDecoder().decode(
        Uint8Array.from(binary, (character) => character.charCodeAt(0)),
      ),
    );
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("invalid payload");
    }
    return parsed as FirebaseClaims;
  } catch (error) {
    if (error instanceof FirebaseAnonymousIdentityError) throw error;
    throw new FirebaseAnonymousIdentityError("source_identity_rejected");
  }
}

function configuredSecret(env: FirebaseAnonymousIdentityEnv): string {
  const value = env.FIREBASE_IDENTITY_PROJECTION_SECRET;
  if (
    typeof value !== "string" ||
    utf8Bytes(value) < 32 ||
    utf8Bytes(value) > 4_096
  ) {
    throw new FirebaseAnonymousIdentityError("bridge_unavailable");
  }
  return value;
}

function hex(bytes: ArrayBuffer): string {
  return [...new Uint8Array(bytes)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

async function hmac(secret: string, value: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return hex(
    await crypto.subtle.sign(
      "HMAC",
      key,
      new TextEncoder().encode(value),
    ),
  );
}

function claimsForAnonymousSource(
  claims: FirebaseClaims,
  projectId: string,
  localId: string,
  validSince: number,
  now: number,
): { authTime: number; expiresAt: number } {
  const issuedAt = integer(claims.iat);
  const expiresAt = integer(claims.exp);
  const authTime = integer(claims.auth_time);
  const firebase =
    claims.firebase &&
    typeof claims.firebase === "object" &&
    !Array.isArray(claims.firebase)
      ? (claims.firebase as Record<string, unknown>)
      : null;
  if (
    claims.aud !== projectId ||
    claims.iss !== `https://securetoken.google.com/${projectId}` ||
    claims.sub !== localId ||
    (claims.user_id !== undefined && claims.user_id !== localId) ||
    firebase?.sign_in_provider !== "anonymous" ||
    issuedAt === null ||
    expiresAt === null ||
    authTime === null ||
    issuedAt < 0 ||
    authTime < 0 ||
    authTime > issuedAt ||
    issuedAt > now + CLOCK_SKEW_SECONDS ||
    expiresAt <= now - CLOCK_SKEW_SECONDS ||
    expiresAt <= issuedAt ||
    expiresAt - issuedAt > MAX_TOKEN_LIFETIME_SECONDS
  ) {
    throw new FirebaseAnonymousIdentityError("source_identity_rejected");
  }
  if (authTime < validSince) {
    throw new FirebaseAnonymousIdentityError("source_identity_revoked");
  }
  return { authTime, expiresAt };
}

/**
 * Verify a currently valid Firebase anonymous credential and return only
 * keyed, non-reversible identity evidence for App D1.
 */
export async function attestFirebaseAnonymousIdentity(
  idToken: string,
  expectedFirebaseUid: string,
  env: FirebaseAnonymousIdentityEnv,
  fetcher: typeof fetch = fetch,
  now = Math.floor(Date.now() / 1_000),
): Promise<FirebaseAnonymousIdentityAttestation> {
  if (
    typeof idToken !== "string" ||
    utf8Bytes(idToken) < 1 ||
    utf8Bytes(idToken) > MAX_ID_TOKEN_BYTES ||
    !validUid(expectedFirebaseUid)
  ) {
    throw new FirebaseAnonymousIdentityError("source_identity_rejected");
  }
  if (!validApiKey(env.FIREBASE_API_KEY) || !validProjectId(env.FIREBASE_PROJECT_ID)) {
    throw new FirebaseAnonymousIdentityError("bridge_unavailable");
  }
  const secret = configuredSecret(env);
  let response: Response;
  try {
    response = await fetcher(
      `${FIREBASE_LOOKUP_URL}?key=${encodeURIComponent(env.FIREBASE_API_KEY)}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ idToken }),
      },
    );
  } catch {
    throw new FirebaseAnonymousIdentityError("bridge_unavailable");
  }
  const declaredLength = Number(response.headers.get("content-length") || "0");
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_LOOKUP_RESPONSE_BYTES
  ) {
    throw new FirebaseAnonymousIdentityError("bridge_unavailable");
  }
  let body: unknown;
  try {
    const raw = await response.text();
    if (utf8Bytes(raw) > MAX_LOOKUP_RESPONSE_BYTES) {
      throw new Error("response too large");
    }
    body = JSON.parse(raw);
  } catch {
    throw new FirebaseAnonymousIdentityError(
      response.ok || response.status >= 500
        ? "bridge_unavailable"
        : "source_identity_rejected",
    );
  }
  if (!response.ok) {
    throw new FirebaseAnonymousIdentityError(
      response.status >= 500
        ? "bridge_unavailable"
        : "source_identity_rejected",
    );
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new FirebaseAnonymousIdentityError("bridge_unavailable");
  }
  const users = (body as { users?: unknown }).users;
  if (!Array.isArray(users) || users.length !== 1) {
    throw new FirebaseAnonymousIdentityError("source_identity_rejected");
  }
  const user = users[0] as FirebaseLookupUser;
  const providers = user?.providerUserInfo;
  const validSince = integer(user?.validSince);
  if (
    !user ||
    typeof user !== "object" ||
    !validUid(user.localId) ||
    user.localId !== expectedFirebaseUid ||
    user.disabled === true ||
    user.disabled === 1 ||
    !Array.isArray(providers) ||
    providers.length !== 0 ||
    validSince === null ||
    validSince < 0
  ) {
    throw new FirebaseAnonymousIdentityError(
      validUid(user?.localId) && user.localId !== expectedFirebaseUid
        ? "source_identity_mismatch"
        : "source_identity_rejected",
    );
  }
  const claims = claimsForAnonymousSource(
    decodeJwtPayload(idToken),
    env.FIREBASE_PROJECT_ID,
    user.localId,
    validSince,
    now,
  );
  const sourceUidHash = await hmac(
    secret,
    `omi-firebase-anonymous-uid-v1\0${env.FIREBASE_PROJECT_ID}\0${user.localId}`,
  );
  const sourceProofHash = await hmac(
    secret,
    `omi-firebase-anonymous-proof-v1\0${sourceUidHash}\0${validSince}\0${claims.authTime}`,
  );
  const sourceProjectionRevision = await hmac(
    secret,
    `omi-firebase-anonymous-revision-v1\0${sourceUidHash}\0${sourceProofHash}`,
  );
  return {
    sourceRef: `fb-anon-${sourceUidHash}`,
    sourceUidHash,
    sourceProofHash,
    sourceCredentialGeneration: validSince,
    sourceProjectionRevision,
    attestedAt: now,
    expiresAt: claims.expiresAt,
  };
}

export const firebaseAnonymousIdentityConstants = Object.freeze({
  lookupUrl: FIREBASE_LOOKUP_URL,
  maxIdTokenBytes: MAX_ID_TOKEN_BYTES,
});
