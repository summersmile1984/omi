import { signAuthContext, verifyAuthContextSignature } from "./auth-context";

export const REALTIME_BOOTSTRAP_HEADER = "x-omi-realtime-bootstrap";
export const REALTIME_BOOTSTRAP_SIGNATURE_HEADER =
  "x-omi-realtime-bootstrap-signature";

const MAX_BOOTSTRAP_LIFETIME_SECONDS = 30;
const CLOCK_SKEW_SECONDS = 5;

export type RealtimeBootstrap = {
  version: 1;
  requestId: string;
  sessionId: string;
  issuedAt: number;
  expiresAt: number;
};

function encode(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function decode(value: string): unknown {
  const padded =
    value.replace(/-/g, "+").replace(/_/g, "/") +
    "===".slice((value.length + 3) % 4);
  const binary = atob(padded);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

export async function createRealtimeBootstrap(
  requestId: string,
  secret: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<{ encoded: string; signature: string } | null> {
  if (!secret) return null;
  const bootstrap: RealtimeBootstrap = {
    version: 1,
    requestId,
    sessionId: crypto.randomUUID(),
    issuedAt: nowSeconds,
    expiresAt: nowSeconds + MAX_BOOTSTRAP_LIFETIME_SECONDS,
  };
  const encoded = encode(bootstrap);
  const signature = await signAuthContext(encoded, secret);
  return signature ? { encoded, signature } : null;
}

export async function verifyRealtimeBootstrap(
  encoded: string | null,
  signature: string | null,
  secret: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<RealtimeBootstrap | null> {
  if (
    !encoded ||
    !(await verifyAuthContextSignature(encoded, signature, secret))
  ) {
    return null;
  }
  try {
    const value = decode(encoded) as Partial<RealtimeBootstrap>;
    if (
      value.version !== 1 ||
      typeof value.requestId !== "string" ||
      !value.requestId ||
      typeof value.sessionId !== "string" ||
      !value.sessionId ||
      typeof value.issuedAt !== "number" ||
      typeof value.expiresAt !== "number" ||
      value.issuedAt > nowSeconds + CLOCK_SKEW_SECONDS ||
      value.expiresAt < nowSeconds ||
      value.expiresAt - value.issuedAt > MAX_BOOTSTRAP_LIFETIME_SECONDS
    ) {
      return null;
    }
    return value as RealtimeBootstrap;
  } catch {
    return null;
  }
}
