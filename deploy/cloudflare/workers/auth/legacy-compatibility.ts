/**
 * Dormant adapter for the legacy Firebase/native-auth and external-app OAuth
 * contracts.  The exact /v1 routes intentionally do not call this module yet:
 * its callers must first provide a completed Firebase import ledger, configured
 * provider credentials, and a deletion-fence authority.
 */

export type LegacyAuthProvider = "google" | "apple";
export type LegacyAuthTransactionKind = "session" | "code";

type D1Statement = {
  bind(...values: unknown[]): D1Statement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  run(): Promise<unknown>;
};

type CompatibilityDatabase = {
  prepare(sql: string): D1Statement;
};

export type CompatibilityGateFailure =
  | "identity_import_incomplete"
  | "identity_projection_missing"
  | "identity_projection_conflict"
  | "identity_provider_mismatch"
  | "deletion_fence_unknown"
  | "deletion_in_progress"
  | "invalid_redirect_uri"
  | "invalid_pkce"
  | "invalid_opaque_secret"
  | "invalid_external_app"
  | "external_app_disabled"
  | "external_integration_missing"
  | "external_app_not_entitled"
  | "external_setup_incomplete"
  | "external_setup_target_unsafe";

export type CompatibilityGateResult =
  | { ok: true }
  | { ok: false; failure: CompatibilityGateFailure };

export type DeletionFence = {
  status: "clear" | "deleting" | "deleted";
};

export type FirebaseIdentityAdmission = {
  ledger: {
    id: string;
    status: "applying" | "completed";
    sourceSha256: string;
    configFingerprint: string;
    canonicalSha256: string;
  };
  projection: {
    firebaseUid: string;
    betterAuthUserId: string;
    providers: readonly string[];
    sourceImportId: string;
    status: "imported" | "revoked" | "conflict";
  };
  betterAuthUserId: string;
  requiredProviders: readonly LegacyAuthProvider[];
  deletionFence: DeletionFence | null;
};

export type ExternalAppAdmission = {
  uid: string;
  app: {
    id: string;
    enabled: boolean;
    externalIntegration: boolean;
    appHomeUrl: string;
    private: boolean;
    ownerUid?: string | null;
    paid: boolean;
    setupCompletedUrl?: string | null;
  };
  entitlement: { paid: boolean; tester: boolean };
  setup: { checked: boolean; completed: boolean; targetPinned: boolean };
  deletionFence: DeletionFence | null;
};

export type LegacyAuthTransactionInput = {
  id?: string;
  kind: LegacyAuthTransactionKind;
  provider: LegacyAuthProvider;
  lookupSecret: string;
  stateSecret: string;
  redirectUri: string;
  codeChallenge: string;
  codeChallengeMethod: string;
  encryptedPayload?: string | null;
  metadataEnvelopeEnc?: string | null;
  createdAt: number;
  expiresAt: number;
};

export type ExternalOAuthTransactionInput = {
  id?: string;
  appId: string;
  uid: string;
  stateSecret: string;
  csrfSecret: string;
  redirectUrl: string;
  appCatalogRevision: number;
  appPolicy: Record<string, unknown>;
  setupTargetHash?: string | null;
  createdAt: number;
  expiresAt: number;
};

export type ConsumedLegacyAuthTransaction = {
  id: string;
  kind: LegacyAuthTransactionKind;
  provider: LegacyAuthProvider;
  redirectUri: string;
  codeChallenge: string;
  encryptedPayload: string | null;
  metadataEnvelopeEnc: string | null;
  stateHash: string;
  consumedAt: number;
};

export type ConsumedExternalOAuthTransaction = {
  id: string;
  appId: string;
  uid: string;
  redirectUrl: string;
  appCatalogRevision: number;
  appPolicyJson: string;
  consumedAt: number;
};

const FORBIDDEN_REDIRECT_SCHEMES = new Set([
  "https",
  "javascript",
  "data",
  "vbscript",
  "file",
  "blob",
  "filesystem",
  "about",
]);
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const OPAQUE_SECRET = /^[\x21-\x7e]{16,512}$/;
const PKCE_VALUE = /^[A-Za-z0-9._~-]{43,128}$/;
const ASCII_LETTERS = /^[a-z]$/;
const ASCII_SCHEME = /^[a-z][a-z0-9+.-]*$/;

function failure(failureCode: CompatibilityGateFailure): CompatibilityGateResult {
  return { ok: false, failure: failureCode };
}

function validUid(uid: string): boolean {
  return uid.length >= 1 && uid.length <= 256 && !uid.includes("/");
}

function validOpaqueSecret(secret: string): boolean {
  return OPAQUE_SECRET.test(secret);
}

/**
 * Exported for the namespaced adapter so redirect validation has one source of
 * truth.  The exact legacy route remains owned by the Python service.
 */
export function isValidLegacyRedirectUri(value: string): boolean {
  return validRedirectUri(value);
}

/** Keep PKCE validation identical between the dormant authority and its seam. */
export function isValidLegacyPkceChallenge(
  challenge: string,
  method: string,
): boolean {
  return validPkce(challenge, method);
}

/** Used only for validating generated/returned opaque transaction secrets. */
export function isValidLegacyOpaqueSecret(value: string): boolean {
  return validOpaqueSecret(value);
}

function validRedirectUri(value: string): boolean {
  if (!value || value.length > 2048) return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  const scheme = parsed.protocol.slice(0, -1).toLowerCase();
  if (!scheme || !ASCII_LETTERS.test(scheme[0] || "") || !ASCII_SCHEME.test(scheme)) {
    return false;
  }
  if (FORBIDDEN_REDIRECT_SCHEMES.has(scheme)) return false;
  if (scheme === "http") {
    return (
      !parsed.username &&
      !parsed.password &&
      LOOPBACK_HOSTS.has(parsed.hostname.toLowerCase())
    );
  }
  return true;
}

function validPkce(challenge: string, method: string): boolean {
  return method === "S256" && PKCE_VALUE.test(challenge);
}

function isPrivateIpv4(hostname: string): boolean {
  const parts = hostname.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) {
    return false;
  }
  const octets = parts.map(Number);
  if (octets.some((part) => part > 255)) return false;
  const [first, second] = octets;
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168) ||
    (first === 198 && (second === 18 || second === 19)) ||
    first >= 224
  );
}

function publicHttpsUrl(value: string): boolean {
  if (!value || value.length > 2048) return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  const hostname = parsed.hostname.toLowerCase();
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    (parsed.port && parsed.port !== "443") ||
    !hostname ||
    hostname === "localhost" ||
    hostname.endsWith(".local") ||
    hostname === "::1" ||
    hostname.startsWith("fc") ||
    hostname.startsWith("fd") ||
    hostname.startsWith("fe80:") ||
    isPrivateIpv4(hostname)
  ) {
    return false;
  }
  return true;
}

export function sha256Base64Url(value: string): Promise<string> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)).then((digest) => {
    // btoa is available in Workers and Node's Web Crypto test runtime. Keep
    // this implementation independent of Buffer so the adapter is portable.
    let binary = "";
    for (const byte of new Uint8Array(digest)) binary += String.fromCharCode(byte);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  });
}

export async function pkceChallengeForVerifier(verifier: string): Promise<string> {
  if (!validOpaqueSecret(verifier) || !PKCE_VALUE.test(verifier)) {
    throw new Error("invalid PKCE verifier");
  }
  return sha256Base64Url(verifier);
}

export function evaluateFirebaseIdentityAdmission(
  input: FirebaseIdentityAdmission,
): CompatibilityGateResult {
  if (
    input.ledger.id !== "firebase" ||
    input.ledger.status !== "completed" ||
    !input.ledger.sourceSha256 ||
    !input.ledger.configFingerprint ||
    !input.ledger.canonicalSha256
  ) {
    return failure("identity_import_incomplete");
  }
  if (
    input.projection.status !== "imported" ||
    input.projection.firebaseUid !== input.betterAuthUserId ||
    input.projection.betterAuthUserId !== input.betterAuthUserId ||
    input.projection.sourceImportId !== input.ledger.id
  ) {
    return failure(
      input.projection.status === "imported"
        ? "identity_projection_missing"
        : "identity_projection_conflict",
    );
  }
  const providers = new Set(input.projection.providers);
  if (
    providers.size !== input.projection.providers.length ||
    input.requiredProviders.some((provider) => !providers.has(provider))
  ) {
    return failure("identity_provider_mismatch");
  }
  if (!input.deletionFence) return failure("deletion_fence_unknown");
  if (input.deletionFence.status !== "clear") return failure("deletion_in_progress");
  return { ok: true };
}

export function evaluateExternalAppAdmission(
  input: ExternalAppAdmission,
): CompatibilityGateResult {
  if (!validUid(input.uid) || !input.app.id || input.app.id.length > 256) {
    return failure("invalid_external_app");
  }
  if (!input.app.enabled) return failure("external_app_disabled");
  if (!input.app.externalIntegration || !publicHttpsUrl(input.app.appHomeUrl)) {
    return failure("external_integration_missing");
  }
  if (
    input.app.private &&
    input.app.ownerUid !== input.uid &&
    !input.entitlement.tester
  ) {
    return failure("external_app_not_entitled");
  }
  if (input.app.paid && !input.entitlement.paid) {
    return failure("external_app_not_entitled");
  }
  if (input.app.setupCompletedUrl) {
    if (!publicHttpsUrl(input.app.setupCompletedUrl)) {
      return failure("external_setup_target_unsafe");
    }
    if (!input.setup.checked || !input.setup.completed || !input.setup.targetPinned) {
      return failure("external_setup_incomplete");
    }
  }
  if (!input.deletionFence) return failure("deletion_fence_unknown");
  if (input.deletionFence.status !== "clear") return failure("deletion_in_progress");
  return { ok: true };
}

function validateAuthTransactionInput(
  input: LegacyAuthTransactionInput,
): CompatibilityGateResult {
  if (
    (input.kind !== "session" && input.kind !== "code") ||
    (input.provider !== "google" && input.provider !== "apple") ||
    !validOpaqueSecret(input.lookupSecret) ||
    !validOpaqueSecret(input.stateSecret)
  ) {
    return failure("invalid_opaque_secret");
  }
  if (!validRedirectUri(input.redirectUri)) return failure("invalid_redirect_uri");
  if (!validPkce(input.codeChallenge, input.codeChallengeMethod)) {
    return failure("invalid_pkce");
  }
  if (
    !Number.isSafeInteger(input.createdAt) ||
    !Number.isSafeInteger(input.expiresAt) ||
    input.expiresAt <= input.createdAt ||
    input.expiresAt - input.createdAt > 600_000
  ) {
    return failure("invalid_opaque_secret");
  }
  if (
    input.kind === "session" && input.encryptedPayload !== undefined && input.encryptedPayload !== null
  ) {
    return failure("invalid_opaque_secret");
  }
  if (
    input.kind === "code" &&
    (typeof input.encryptedPayload !== "string" ||
      input.encryptedPayload.length === 0 ||
      input.encryptedPayload.length > 65_536)
  ) {
    return failure("invalid_opaque_secret");
  }
  if (
    input.metadataEnvelopeEnc !== undefined &&
    input.metadataEnvelopeEnc !== null &&
    (input.metadataEnvelopeEnc.length < 20 ||
      input.metadataEnvelopeEnc.length > 400_000 ||
      !input.metadataEnvelopeEnc.startsWith("v1."))
  ) {
    return failure("invalid_opaque_secret");
  }
  return { ok: true };
}

function assertValid(result: CompatibilityGateResult): void {
  if (!result.ok) throw new Error(result.failure);
}

export async function createLegacyAuthTransaction(
  database: CompatibilityDatabase,
  input: LegacyAuthTransactionInput,
): Promise<{ id: string; lookupHash: string; stateHash: string }> {
  assertValid(validateAuthTransactionInput(input));
  const id = input.id || crypto.randomUUID();
  const lookupHash = await sha256Base64Url(input.lookupSecret);
  const stateHash = await sha256Base64Url(input.stateSecret);
  await database
    .prepare(
      `INSERT INTO cf_legacy_auth_transactions
         (id, kind, provider, lookupHash, stateHash, redirectUri,
          codeChallenge, codeChallengeMethod, encryptedPayload,
          metadataEnvelopeEnc, status, expiresAt, createdAt, consumedAt)
       VALUES (?, ?, ?, ?, ?, ?, ?, 'S256', ?, ?, 'pending', ?, ?, NULL)`,
    )
    .bind(
      id,
      input.kind,
      input.provider,
      lookupHash,
      stateHash,
      input.redirectUri,
      input.codeChallenge,
      input.encryptedPayload ?? null,
      input.metadataEnvelopeEnc ?? null,
      input.expiresAt,
      input.createdAt,
    )
    .run();
  return { id, lookupHash, stateHash };
}

function consumedAuthRow(row: Record<string, unknown>): ConsumedLegacyAuthTransaction {
  return {
    id: String(row.id),
    kind: String(row.kind) as LegacyAuthTransactionKind,
    provider: String(row.provider) as LegacyAuthProvider,
    redirectUri: String(row.redirectUri),
    codeChallenge: String(row.codeChallenge),
    encryptedPayload:
      typeof row.encryptedPayload === "string" ? row.encryptedPayload : null,
    metadataEnvelopeEnc:
      typeof row.metadataEnvelopeEnc === "string"
        ? row.metadataEnvelopeEnc
        : null,
    stateHash: String(row.stateHash),
    consumedAt: Number(row.consumedAt),
  };
}

export async function consumeLegacyAuthTransaction(
  database: CompatibilityDatabase,
  input: {
    lookupSecret: string;
    kind: LegacyAuthTransactionKind;
    provider: LegacyAuthProvider;
    /**
     * Required for code transactions. Session callbacks do not know the
     * caller redirect until their authenticated metadata envelope is opened,
     * so they may consume by state/provider first and verify the redirect
     * against that envelope afterwards.
     */
    redirectUri?: string;
    codeVerifier?: string;
    now: number;
  },
): Promise<ConsumedLegacyAuthTransaction | null> {
  if (
    !validOpaqueSecret(input.lookupSecret) ||
    (input.redirectUri !== undefined && !validRedirectUri(input.redirectUri)) ||
    (input.kind === "code" && input.redirectUri === undefined)
  ) {
    return null;
  }
  const lookupHash = await sha256Base64Url(input.lookupSecret);
  const redirectPredicate =
    input.redirectUri === undefined ? "" : "AND redirectUri = ?";
  const row = await database
    .prepare(
      `SELECT id, kind, provider, redirectUri, codeChallenge,
              encryptedPayload, metadataEnvelopeEnc, stateHash
         FROM cf_legacy_auth_transactions
        WHERE lookupHash = ? AND kind = ? AND provider = ?
          ${redirectPredicate} AND status = 'pending' AND expiresAt > ?`,
    )
    .bind(
      lookupHash,
      input.kind,
      input.provider,
      ...(input.redirectUri === undefined ? [] : [input.redirectUri]),
      input.now,
    )
    .first<Record<string, unknown>>();
  if (!row) return null;
  if (input.kind === "code") {
    if (!input.codeVerifier || !PKCE_VALUE.test(input.codeVerifier)) return null;
    const expected = await pkceChallengeForVerifier(input.codeVerifier);
    if (expected !== row.codeChallenge) return null;
  }
  const consumedAt = input.now;
  const consumed = await database
    .prepare(
      `UPDATE cf_legacy_auth_transactions
          SET status = 'consumed', consumedAt = ?
        WHERE id = ? AND status = 'pending' AND expiresAt > ?
        RETURNING id, kind, provider, redirectUri, codeChallenge,
                  encryptedPayload, metadataEnvelopeEnc, stateHash, consumedAt`,
    )
    .bind(consumedAt, row.id, input.now)
    .first<Record<string, unknown>>();
  return consumed ? consumedAuthRow(consumed) : null;
}

function validateExternalTransactionInput(
  input: ExternalOAuthTransactionInput,
): CompatibilityGateResult {
  if (
    !validUid(input.uid) ||
    !input.appId ||
    input.appId.length > 256 ||
    !validOpaqueSecret(input.stateSecret) ||
    !validOpaqueSecret(input.csrfSecret) ||
    !publicHttpsUrl(input.redirectUrl) ||
    !Number.isSafeInteger(input.appCatalogRevision) ||
    input.appCatalogRevision < 1 ||
    !Number.isSafeInteger(input.createdAt) ||
    !Number.isSafeInteger(input.expiresAt) ||
    input.expiresAt <= input.createdAt ||
    input.expiresAt - input.createdAt > 600_000
  ) {
    return failure("invalid_external_app");
  }
  return { ok: true };
}

export async function createExternalOAuthTransaction(
  database: CompatibilityDatabase,
  input: ExternalOAuthTransactionInput,
): Promise<{ id: string; stateHash: string; csrfHash: string }> {
  assertValid(validateExternalTransactionInput(input));
  const id = input.id || crypto.randomUUID();
  const stateHash = await sha256Base64Url(input.stateSecret);
  const csrfHash = await sha256Base64Url(input.csrfSecret);
  const appPolicyJson = JSON.stringify(input.appPolicy);
  if (
    !appPolicyJson ||
    appPolicyJson.length > 65_536 ||
    Array.isArray(input.appPolicy) ||
    typeof input.appPolicy !== "object"
  ) {
    throw new Error("invalid_external_app");
  }
  await database
    .prepare(
      `INSERT INTO cf_external_oauth_transactions
         (id, appId, uid, stateHash, csrfHash, redirectUrl,
          appCatalogRevision, appPolicyJson, setupTargetHash, status,
          expiresAt, createdAt, consumedAt)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL)`,
    )
    .bind(
      id,
      input.appId,
      input.uid,
      stateHash,
      csrfHash,
      input.redirectUrl,
      input.appCatalogRevision,
      appPolicyJson,
      input.setupTargetHash ?? null,
      input.expiresAt,
      input.createdAt,
    )
    .run();
  return { id, stateHash, csrfHash };
}

export async function consumeExternalOAuthTransaction(
  database: CompatibilityDatabase,
  input: { stateSecret: string; csrfSecret: string; uid: string; now: number },
): Promise<ConsumedExternalOAuthTransaction | null> {
  if (
    !validUid(input.uid) ||
    !validOpaqueSecret(input.stateSecret) ||
    !validOpaqueSecret(input.csrfSecret)
  ) {
    return null;
  }
  const stateHash = await sha256Base64Url(input.stateSecret);
  const csrfHash = await sha256Base64Url(input.csrfSecret);
  const row = await database
    .prepare(
      `UPDATE cf_external_oauth_transactions
          SET status = 'consumed', consumedAt = ?
        WHERE stateHash = ? AND csrfHash = ? AND uid = ?
          AND status = 'pending' AND expiresAt > ?
        RETURNING id, appId, uid, redirectUrl, appCatalogRevision,
                  appPolicyJson, consumedAt`,
    )
    .bind(input.now, stateHash, csrfHash, input.uid, input.now)
    .first<Record<string, unknown>>();
  if (!row) return null;
  return {
    id: String(row.id),
    appId: String(row.appId),
    uid: String(row.uid),
    redirectUrl: String(row.redirectUrl),
    appCatalogRevision: Number(row.appCatalogRevision),
    appPolicyJson: String(row.appPolicyJson),
    consumedAt: Number(row.consumedAt),
  };
}
