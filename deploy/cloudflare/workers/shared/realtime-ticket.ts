import {
  signAuthContext,
  verifyAuthContextSignature,
  type AuthAuthority,
  type AuthContext,
} from "./auth-context";

const AUDIENCE = "realtime-web";
const MAX_TICKET_LIFETIME_SECONDS = 30;
const CLOCK_SKEW_SECONDS = 5;

type RealtimeTicket = {
  version: 1;
  audience: typeof AUDIENCE;
  uid: string;
  authority: AuthAuthority;
  sessionGeneration?: string;
  requestId: string;
  ticketId: string;
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

function validAuthority(value: unknown): value is AuthAuthority {
  return value === "firebase" || value === "better-auth" || value === "internal";
}

export async function createRealtimeTicket(
  identity: AuthContext,
  secret: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<string | null> {
  if (!secret) return null;
  const payload: RealtimeTicket = {
    version: 1,
    audience: AUDIENCE,
    uid: identity.uid,
    authority: identity.authority,
    sessionGeneration: identity.sessionGeneration,
    requestId: identity.requestId,
    ticketId: crypto.randomUUID(),
    issuedAt: nowSeconds,
    expiresAt: nowSeconds + MAX_TICKET_LIFETIME_SECONDS,
  };
  const encoded = encode(payload);
  const signature = await signAuthContext(encoded, secret);
  return signature ? `${encoded}.${signature}` : null;
}

export async function verifyRealtimeTicket(
  ticket: string,
  secret: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<AuthContext | null> {
  const separator = ticket.lastIndexOf(".");
  if (separator <= 0 || separator === ticket.length - 1) return null;
  const encoded = ticket.slice(0, separator);
  const signature = ticket.slice(separator + 1);
  if (!(await verifyAuthContextSignature(encoded, signature, secret))) return null;

  try {
    const value = decode(encoded) as Partial<RealtimeTicket>;
    if (
      value.version !== 1 ||
      value.audience !== AUDIENCE ||
      typeof value.uid !== "string" ||
      value.uid.length < 1 ||
      value.uid.length > 512 ||
      !validAuthority(value.authority) ||
      typeof value.requestId !== "string" ||
      !value.requestId ||
      typeof value.ticketId !== "string" ||
      !value.ticketId ||
      typeof value.issuedAt !== "number" ||
      typeof value.expiresAt !== "number" ||
      value.issuedAt > nowSeconds + CLOCK_SKEW_SECONDS ||
      value.expiresAt < nowSeconds ||
      value.expiresAt - value.issuedAt > MAX_TICKET_LIFETIME_SECONDS
    ) {
      return null;
    }
    return {
      uid: value.uid,
      authority: value.authority,
      sessionGeneration: value.sessionGeneration,
      requestId: value.requestId,
    };
  } catch {
    return null;
  }
}
