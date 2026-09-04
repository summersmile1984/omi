import { accessIdentity } from "./jwt-policy.mjs";

export async function verifyAccessToken(token, policy, authority) {
  // Fetch outside the credential-error catch: a key-store outage is retryable,
  // not evidence that a user's otherwise valid credential was revoked.
  const keys = await authority.getJwks();
  try {
    const verified = await authority.verifySignature(token, keys);
    return accessIdentity(verified.payload, policy);
  } catch (error) {
    if (/^ERR_(JWT|JWS|JWKS?|JOSE)_/.test(error?.code || "")) return null;
    throw error;
  }
}
