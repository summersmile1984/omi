import type { JwtPolicy } from "./jwt-policy.mjs";
export function verifyAccessToken(
  token: string,
  policy: Pick<JwtPolicy, "issuer" | "audience">,
  authority: {
    getJwks: () => Promise<unknown>;
    verifySignature: (
      token: string,
      keys: unknown,
    ) => Promise<{ payload: unknown }>;
  },
): Promise<{ uid: string; sid: string } | null>;
