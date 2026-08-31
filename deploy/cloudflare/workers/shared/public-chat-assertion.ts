import { signAuthContext } from "./auth-context";

export const PUBLIC_CHAT_ASSERTION_HEADER = "x-omi-public-chat-assertion";
export const PUBLIC_CHAT_ASSERTION_LIFETIME_SECONDS = 60;

type PublicChatAssertion = {
  version: 1;
  kind: "public-shared-chat";
  subject: string;
  requestId: string;
  audience: "api-core";
  assertionId: string;
  issuedAt: number;
  expiresAt: number;
  method: string;
  path: string;
};

function encode(value: PublicChatAssertion): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

/** Sign the anonymous public-chat boundary separately from AuthContext. */
export async function createPublicChatAssertion(
  subject: string,
  requestId: string,
  target: { method: string; url: string | URL },
  secret: string | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<string | null> {
  if (!/^[0-9a-f]{64}$/.test(subject) || !requestId || !secret) return null;
  const path = new URL(target.url).pathname;
  if (!path.startsWith("/")) return null;
  const assertion: PublicChatAssertion = {
    version: 1,
    kind: "public-shared-chat",
    subject,
    requestId,
    audience: "api-core",
    assertionId: crypto.randomUUID(),
    issuedAt: nowSeconds,
    expiresAt: nowSeconds + PUBLIC_CHAT_ASSERTION_LIFETIME_SECONDS,
    method: target.method.toUpperCase(),
    path,
  };
  const encoded = encode(assertion);
  const signature = await signAuthContext(encoded, secret);
  return signature ? `${encoded}.${signature}` : null;
}
