import type { AuthContext } from "../shared/auth-context";
import type { EdgeEnv } from "./env";

export const BYOK_PROVIDERS = [
  "openai",
  "anthropic",
  "gemini",
  "deepgram",
] as const;

export type ByokProvider = (typeof BYOK_PROVIDERS)[number];

const BYOK_HEARTBEAT_TTL_SECONDS = 7 * 24 * 60 * 60;
const MAX_BYOK_KEY_LENGTH = 8_192;
const SHA256_HEX = /^[a-f0-9]{64}$/;

const headerName = (provider: ByokProvider) => `x-byok-${provider}`;
type ByokEnrollmentRow = {
  active: number;
  openai_fingerprint: string | null;
  anthropic_fingerprint: string | null;
  gemini_fingerprint: string | null;
  deepgram_fingerprint: string | null;
  last_seen_at: number;
};

const FINGERPRINT_COLUMNS = {
  openai: "openai_fingerprint",
  anthropic: "anthropic_fingerprint",
  gemini: "gemini_fingerprint",
  deepgram: "deepgram_fingerprint",
} as const satisfies Record<ByokProvider, keyof ByokEnrollmentRow>;

export type ByokHeaderValidation =
  | { context: AuthContext; response?: never }
  | { context?: never; response: Response };

function bytesToHex(value: ArrayBuffer): string {
  return Array.from(new Uint8Array(value), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

async function sha256Hex(value: string): Promise<string> {
  return bytesToHex(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
  );
}

async function pepperedFingerprint(
  pepper: string,
  fingerprint: string,
): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(pepper),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return bytesToHex(
    await crypto.subtle.sign(
      "HMAC",
      key,
      new TextEncoder().encode(fingerprint),
    ),
  );
}

function safeEqualHex(left: string, right: string): boolean {
  if (
    left.length !== right.length ||
    !SHA256_HEX.test(left) ||
    !SHA256_HEX.test(right)
  ) {
    return false;
  }
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function configuredPepper(env: EdgeEnv): string | null {
  const pepper = (env.BYOK_FINGERPRINT_PEPPER || "").trim();
  return pepper.length >= 32 ? pepper : null;
}

export function stripByokHeaders(headers: Headers): void {
  for (const provider of BYOK_PROVIDERS) headers.delete(headerName(provider));
}

function requestKeys(headers: Headers): Partial<Record<ByokProvider, string>> {
  return Object.fromEntries(
    BYOK_PROVIDERS.flatMap((provider) => {
      const value = headers.get(headerName(provider));
      return value ? [[provider, value] as const] : [];
    }),
  );
}

export async function validateByokHeaders(
  env: EdgeEnv,
  identity: AuthContext,
  headers: Headers,
  options: { recoverInvalid?: boolean; now?: number } = {},
): Promise<ByokHeaderValidation> {
  const keys = requestKeys(headers);
  if (Object.keys(keys).length === 0) return { context: identity };
  if (!env.APP_DB) {
    stripByokHeaders(headers);
    return {
      response: Response.json(
        { error: "byok_state_unavailable" },
        { status: 503 },
      ),
    };
  }

  let enrollment: ByokEnrollmentRow | null;
  try {
    enrollment = await env.APP_DB.prepare(
      "SELECT active, openai_fingerprint, anthropic_fingerprint, gemini_fingerprint, " +
        "deepgram_fingerprint, last_seen_at FROM cf_user_byok_enrollments WHERE uid = ?",
    )
      .bind(identity.uid)
      .first<ByokEnrollmentRow>();
  } catch {
    stripByokHeaders(headers);
    return {
      response: Response.json(
        { error: "byok_state_unavailable" },
        { status: 503 },
      ),
    };
  }

  const now = options.now ?? Math.floor(Date.now() / 1000);
  if (
    !enrollment ||
    enrollment.active !== 1 ||
    !Number.isInteger(enrollment.last_seen_at) ||
    now - enrollment.last_seen_at > BYOK_HEARTBEAT_TTL_SECONDS
  ) {
    stripByokHeaders(headers);
    return { context: identity };
  }

  const pepper = configuredPepper(env);
  if (!pepper) {
    stripByokHeaders(headers);
    return {
      response: Response.json(
        { error: "byok_validation_unavailable" },
        { status: 503 },
      ),
    };
  }

  let validationError: string | null = null;
  for (const provider of BYOK_PROVIDERS) {
    const rawKey = keys[provider];
    const stored = enrollment[FINGERPRINT_COLUMNS[provider]];
    if (!rawKey) {
      validationError = `BYOK key header missing for enrolled provider: ${provider}`;
      break;
    }
    if (rawKey.length > MAX_BYOK_KEY_LENGTH || !stored) {
      validationError = `BYOK key fingerprint mismatch for provider: ${provider}`;
      break;
    }
    const plain = await sha256Hex(rawKey);
    const peppered = await pepperedFingerprint(pepper, plain);
    // The plain comparison is import compatibility for legacy enrollments.
    // Every enrollment written by this Worker uses only the HMAC form.
    if (!safeEqualHex(stored, peppered) && !safeEqualHex(stored, plain)) {
      validationError = `BYOK key fingerprint mismatch for provider: ${provider}`;
      break;
    }
  }

  if (validationError) {
    stripByokHeaders(headers);
    if (options.recoverInvalid) return { context: identity };
    return {
      response: Response.json({ detail: validationError }, { status: 403 }),
    };
  }
  return { context: { ...identity, byokActive: true } };
}

export function parseByokActivationPayload(
  payload: unknown,
): { fingerprints: Record<ByokProvider, string> } | Response {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return Response.json(
      { detail: "Invalid BYOK activation payload" },
      { status: 400 },
    );
  }
  const fingerprints = (payload as { fingerprints?: unknown }).fingerprints;
  if (
    !fingerprints ||
    typeof fingerprints !== "object" ||
    Array.isArray(fingerprints)
  ) {
    return Response.json(
      { detail: "Invalid BYOK activation payload" },
      { status: 400 },
    );
  }
  const supplied = Object.keys(fingerprints);
  const missing = BYOK_PROVIDERS.filter(
    (provider) => !supplied.includes(provider),
  );
  if (missing.length) {
    return Response.json(
      {
        detail: `Missing fingerprints for providers: [${missing
          .map((provider) => `'${provider}'`)
          .join(", ")}]`,
      },
      { status: 400 },
    );
  }
  for (const provider of supplied) {
    if (!BYOK_PROVIDERS.includes(provider as ByokProvider)) {
      return Response.json(
        { detail: `Unknown provider: ${provider}` },
        { status: 400 },
      );
    }
    const fingerprint = (fingerprints as Record<string, unknown>)[provider];
    if (typeof fingerprint !== "string" || !SHA256_HEX.test(fingerprint)) {
      return Response.json(
        {
          detail: `Invalid fingerprint for ${provider}: expected lowercase hex SHA-256 (64 chars)`,
        },
        { status: 400 },
      );
    }
  }
  return {
    fingerprints: Object.fromEntries(
      BYOK_PROVIDERS.map((provider) => [
        provider,
        (fingerprints as Record<ByokProvider, string>)[provider],
      ]),
    ) as Record<ByokProvider, string>,
  };
}

export async function activateByok(
  env: EdgeEnv,
  uid: string,
  fingerprints: Record<ByokProvider, string>,
  now = Math.floor(Date.now() / 1000),
): Promise<Response> {
  const pepper = configuredPepper(env);
  if (!pepper || !env.APP_DB) {
    return Response.json(
      { error: "byok_enrollment_unavailable" },
      { status: 503 },
    );
  }
  const stored = await Promise.all(
    BYOK_PROVIDERS.map((provider) =>
      pepperedFingerprint(pepper, fingerprints[provider]),
    ),
  );
  try {
    await env.APP_DB.prepare(
      "INSERT INTO cf_user_byok_enrollments " +
        "(uid, active, openai_fingerprint, anthropic_fingerprint, gemini_fingerprint, " +
        "deepgram_fingerprint, last_seen_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?) " +
        "ON CONFLICT(uid) DO UPDATE SET active = 1, openai_fingerprint = excluded.openai_fingerprint, " +
        "anthropic_fingerprint = excluded.anthropic_fingerprint, gemini_fingerprint = excluded.gemini_fingerprint, " +
        "deepgram_fingerprint = excluded.deepgram_fingerprint, last_seen_at = excluded.last_seen_at, " +
        "updated_at = excluded.updated_at",
    )
      .bind(uid, ...stored, now, now)
      .run();
  } catch {
    return Response.json(
      { error: "byok_enrollment_unavailable" },
      { status: 503 },
    );
  }
  return Response.json({ active: true });
}

export async function deactivateByok(
  env: EdgeEnv,
  uid: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Response> {
  if (!env.APP_DB) {
    return Response.json(
      { error: "byok_enrollment_unavailable" },
      { status: 503 },
    );
  }
  try {
    await env.APP_DB.prepare(
      "INSERT INTO cf_user_byok_enrollments " +
        "(uid, active, openai_fingerprint, anthropic_fingerprint, gemini_fingerprint, " +
        "deepgram_fingerprint, last_seen_at, updated_at) VALUES (?, 0, NULL, NULL, NULL, NULL, ?, ?) " +
        "ON CONFLICT(uid) DO UPDATE SET active = 0, openai_fingerprint = NULL, " +
        "anthropic_fingerprint = NULL, gemini_fingerprint = NULL, deepgram_fingerprint = NULL, " +
        "last_seen_at = excluded.last_seen_at, updated_at = excluded.updated_at",
    )
      .bind(uid, now, now)
      .run();
  } catch {
    return Response.json(
      { error: "byok_enrollment_unavailable" },
      { status: 503 },
    );
  }
  return Response.json({ active: false });
}
