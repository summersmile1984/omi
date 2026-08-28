export type AuthAuthority = "firebase" | "better-auth" | "internal";
export type AuthAudience = "api-core" | "api-ai" | "auth" | "jobs" | "realtime";

export type AuthContext = {
  uid: string;
  authority: AuthAuthority;
  displayName?: string;
  accountCreatedAt?: number;
  sessionGeneration?: string;
  requestId: string;
};

export type SignedAuthContext = AuthContext & {
  version: 1;
  audience: AuthAudience;
  assertionId: string;
  issuedAt: number;
  expiresAt: number;
  method: string;
  path: string;
};

export const AUTH_CONTEXT_HEADER = "x-omi-auth-context";
export const AUTH_SIGNATURE_HEADER = "x-omi-internal-signature";
export const AUTH_CONTEXT_LIFETIME_SECONDS = 60;
const CLOCK_SKEW_SECONDS = 5;

function validAuthority(value: unknown): value is AuthAuthority {
  return (
    value === "firebase" || value === "better-auth" || value === "internal"
  );
}

function validAudience(value: unknown): value is AuthAudience {
  return (
    value === "api-core" ||
    value === "api-ai" ||
    value === "auth" ||
    value === "jobs" ||
    value === "realtime"
  );
}

export function encodeAuthContext(context: SignedAuthContext): string {
  const bytes = new TextEncoder().encode(JSON.stringify(context));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

export function decodeAuthContext(
  value: string | null,
): SignedAuthContext | null {
  if (!value) return null;
  try {
    const padded =
      value.replace(/-/g, "+").replace(/_/g, "/") +
      "===".slice((value.length + 3) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (character) =>
      character.charCodeAt(0),
    );
    const parsed = JSON.parse(
      new TextDecoder().decode(bytes),
    ) as Partial<SignedAuthContext>;
    if (
      parsed.version !== 1 ||
      typeof parsed.uid !== "string" ||
      !parsed.uid ||
      !validAuthority(parsed.authority) ||
      typeof parsed.requestId !== "string" ||
      !parsed.requestId ||
      !validAudience(parsed.audience) ||
      typeof parsed.assertionId !== "string" ||
      !parsed.assertionId ||
      typeof parsed.issuedAt !== "number" ||
      typeof parsed.expiresAt !== "number" ||
      typeof parsed.method !== "string" ||
      !parsed.method ||
      typeof parsed.path !== "string" ||
      !parsed.path.startsWith("/") ||
      (parsed.accountCreatedAt !== undefined &&
        (typeof parsed.accountCreatedAt !== "number" ||
          !Number.isInteger(parsed.accountCreatedAt) ||
          parsed.accountCreatedAt <= 0))
    ) {
      return null;
    }
    return parsed as SignedAuthContext;
  } catch {
    return null;
  }
}

export async function signAuthContext(
  value: string,
  secret: string | undefined,
): Promise<string | null> {
  if (!secret) return null;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(value),
  );
  const bytes = new Uint8Array(signature);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

export async function verifyAuthContextSignature(
  value: string,
  signature: string | null,
  secret: string | undefined,
): Promise<boolean> {
  if (!signature || !secret) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["verify"],
    );
    const padded =
      signature.replace(/-/g, "+").replace(/_/g, "/") +
      "===".slice((signature.length + 3) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (character) =>
      character.charCodeAt(0),
    );
    return await crypto.subtle.verify(
      "HMAC",
      key,
      bytes,
      new TextEncoder().encode(value),
    );
  } catch {
    return false;
  }
}

export async function createSignedAuthContext(
  identity: AuthContext,
  audience: AuthAudience,
  method: string,
  path: string,
  secret: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<{
  context: SignedAuthContext;
  encoded: string;
  signature: string;
} | null> {
  if (!secret || !path.startsWith("/")) return null;
  const context: SignedAuthContext = {
    ...identity,
    version: 1,
    audience,
    assertionId: crypto.randomUUID(),
    issuedAt: nowSeconds,
    expiresAt: nowSeconds + AUTH_CONTEXT_LIFETIME_SECONDS,
    method: method.toUpperCase(),
    path,
  };
  const encoded = encodeAuthContext(context);
  const signature = await signAuthContext(encoded, secret);
  return signature ? { context, encoded, signature } : null;
}

export async function verifyRequestAuthContext(
  request: Request,
  audience: AuthAudience,
  secret: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<SignedAuthContext | null> {
  const encoded = request.headers.get(AUTH_CONTEXT_HEADER);
  const signature = request.headers.get(AUTH_SIGNATURE_HEADER);
  if (
    !encoded ||
    !(await verifyAuthContextSignature(encoded, signature, secret))
  ) {
    return null;
  }
  const context = decodeAuthContext(encoded);
  const path = new URL(request.url).pathname;
  if (
    !context ||
    context.audience !== audience ||
    context.method !== request.method.toUpperCase() ||
    context.path !== path ||
    context.issuedAt > nowSeconds + CLOCK_SKEW_SECONDS ||
    context.expiresAt < nowSeconds ||
    context.expiresAt - context.issuedAt > AUTH_CONTEXT_LIFETIME_SECONDS
  ) {
    return null;
  }
  return context;
}
