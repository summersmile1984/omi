import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../shared/auth-context";
import type { AuthAudience, AuthContext } from "../shared/auth-context";

export function stripUntrustedHeaders(
  request: Request,
  options: { preserveClientAuth?: boolean } = {},
): Headers {
  const headers = new Headers(request.headers);
  for (const key of [
    "x-omi-auth-context",
    "x-omi-uid",
    "x-omi-session-generation",
    "x-omi-internal-signature",
  ]) {
    headers.delete(key);
  }
  if (!options.preserveClientAuth) {
    headers.delete("authorization");
    headers.delete("cookie");
  }
  return headers;
}

export async function attachAuthContext(
  headers: Headers,
  context: AuthContext,
  secret: string | undefined,
  audience: AuthAudience,
  target: { method: string; url: string | URL },
): Promise<void> {
  const signed = await createSignedAuthContext(
    context,
    audience,
    target.method,
    new URL(target.url).pathname,
    secret,
  );
  if (!signed) return;
  headers.set(AUTH_CONTEXT_HEADER, signed.encoded);
  headers.set(AUTH_SIGNATURE_HEADER, signed.signature);
}
