import type { EdgeEnv } from "./env";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../shared/auth-context";
import type { AuthAudience, AuthContext } from "../shared/auth-context";

export async function verifyBearer(
  request: Request,
  env: EdgeEnv,
  requestId: string,
): Promise<AuthContext | null> {
  const authorization = request.headers.get("authorization") || "";
  const cookie = request.headers.get("cookie") || "";
  if (!authorization.startsWith("Bearer ") && !cookie) return null;

  // Auth Worker is the only component that knows Better Auth's session/token
  // storage. The Edge never parses or trusts a caller-provided uid.
  if (!env.INTERNAL_ASSERTION_SECRET) return null;
  try {
    const headers = new Headers({
      "x-request-id": requestId,
      "x-internal-assertion-secret": env.INTERNAL_ASSERTION_SECRET,
    });
    if (authorization) headers.set("authorization", authorization);
    if (cookie) headers.set("cookie", cookie);
    const response = await env.AUTH.fetch(
      new Request("https://auth.internal/internal/verify", {
        method: "POST",
        headers,
      }),
    );
    if (!response.ok) return null;
    const body = (await response.json()) as Partial<AuthContext>;
    if (
      typeof body.uid !== "string" ||
      !body.uid ||
      (body.authority !== "firebase" &&
        body.authority !== "better-auth" &&
        body.authority !== "internal")
    ) {
      return null;
    }
    return {
      uid: body.uid,
      authority: body.authority,
      displayName:
        typeof body.displayName === "string" && body.displayName.trim()
          ? body.displayName.trim().slice(0, 120)
          : undefined,
      accountCreatedAt:
        typeof body.accountCreatedAt === "number" &&
        Number.isInteger(body.accountCreatedAt) &&
        body.accountCreatedAt > 0
          ? body.accountCreatedAt
          : undefined,
      sessionGeneration: body.sessionGeneration,
      requestId,
    };
  } catch {
    return null;
  }
}

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
