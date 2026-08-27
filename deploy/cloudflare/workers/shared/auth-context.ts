export type AuthAuthority = "firebase" | "better-auth" | "internal";

export type AuthContext = {
  uid: string;
  authority: AuthAuthority;
  sessionGeneration?: string;
  requestId: string;
};

export const AUTH_CONTEXT_HEADER = "x-omi-auth-context";
export const AUTH_SIGNATURE_HEADER = "x-omi-internal-signature";

export function encodeAuthContext(context: AuthContext): string {
  const bytes = new TextEncoder().encode(JSON.stringify(context));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export function decodeAuthContext(value: string | null): AuthContext | null {
  if (!value) return null;
  try {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const parsed = JSON.parse(new TextDecoder().decode(bytes)) as Partial<AuthContext>;
    if (
      typeof parsed.uid !== "string" ||
      !parsed.uid ||
      (parsed.authority !== "firebase" && parsed.authority !== "better-auth" && parsed.authority !== "internal") ||
      typeof parsed.requestId !== "string"
    ) {
      return null;
    }
    return parsed as AuthContext;
  } catch {
    return null;
  }
}

export async function signAuthContext(value: string, secret: string | undefined): Promise<string | null> {
  if (!secret) return null;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value));
  const bytes = new Uint8Array(signature);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
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
    const padded = signature.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((signature.length + 3) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return await crypto.subtle.verify("HMAC", key, bytes, new TextEncoder().encode(value));
  } catch {
    return false;
  }
}
