import type { AuthContext } from "./auth-context";

export type SessionAuthorityEnv = {
  AUTH: Fetcher;
  INTERNAL_ASSERTION_SECRET?: string;
};

export async function verifyBearer(
  request: Request,
  env: SessionAuthorityEnv,
  requestId: string,
): Promise<AuthContext | null> {
  const authorization = request.headers.get("authorization") || "";
  const cookie = request.headers.get("cookie") || "";
  if (!authorization.startsWith("Bearer ") && !cookie) return null;

  // Auth Worker is the only component that knows Better Auth's session/token
  // storage. Gateways never parse or trust a caller-provided uid.
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
