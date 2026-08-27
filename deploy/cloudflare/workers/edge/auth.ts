import type { EdgeEnv } from "./env";
import { AUTH_CONTEXT_HEADER, AUTH_SIGNATURE_HEADER, encodeAuthContext, signAuthContext } from "../shared/auth-context";
import type { AuthContext } from "../shared/auth-context";

export async function verifyBearer(request: Request, env: EdgeEnv, requestId: string): Promise<AuthContext | null> {
  const authorization = request.headers.get("authorization") || "";
  if (!authorization.startsWith("Bearer ")) return null;

  // Auth Worker is the only component that knows Better Auth's session/token
  // storage. The Edge never parses or trusts a caller-provided uid.
  if (!env.INTERNAL_ASSERTION_SECRET) return null;
  const response = await env.AUTH.fetch(
    new Request("https://auth.internal/internal/verify", {
      method: "POST",
      headers: {
        authorization,
        "x-request-id": requestId,
        "x-internal-assertion-secret": env.INTERNAL_ASSERTION_SECRET,
      },
    }),
  );
  if (!response.ok) return null;
  const body = (await response.json()) as Partial<AuthContext>;
  if (
    typeof body.uid !== "string" ||
    !body.uid ||
    (body.authority !== "firebase" && body.authority !== "better-auth" && body.authority !== "internal")
  ) {
    return null;
  }
  return { uid: body.uid, authority: body.authority, sessionGeneration: body.sessionGeneration, requestId };
}

export function stripUntrustedHeaders(request: Request): Headers {
  const headers = new Headers(request.headers);
  for (const key of ["x-omi-auth-context", "x-omi-uid", "x-omi-session-generation", "x-omi-internal-signature"]) {
    headers.delete(key);
  }
  return headers;
}

export async function attachAuthContext(headers: Headers, context: AuthContext, secret?: string): Promise<void> {
  const encoded = encodeAuthContext(context);
  headers.set(AUTH_CONTEXT_HEADER, encoded);
  const signature = await signAuthContext(encoded, secret);
  if (signature) headers.set(AUTH_SIGNATURE_HEADER, signature);
}
