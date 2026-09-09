import {
  jwtOptions,
  type JwtEnvironment,
} from "../../../../auth/shared/jwt-policy.mjs";
import { createLocalJWKSet, jwtVerify, type JSONWebKeySet } from "jose";
import { verifyAccessToken } from "../../../../auth/shared/access-verifier.mjs";
import { jwtPolicy } from "../../../../auth/shared/jwt-policy.mjs";

export function verifyProductToken(
  token: string,
  env: JwtEnvironment,
  baseURL: string,
  getJwks: () => Promise<unknown>,
) {
  const policy = jwtPolicy(env, baseURL);
  return verifyAccessToken(token, policy, {
    getJwks,
    verifySignature: (value, keys) =>
      jwtVerify(value, createLocalJWKSet(keys as JSONWebKeySet), {
        algorithms: ["ES256", "RS256", "EdDSA"],
        issuer: policy.issuer,
        audience: policy.audience,
      }),
  });
}

export function cloudflareJwtOptions(env: JwtEnvironment, baseURL: string) {
  const shared = jwtOptions(env, baseURL);
  return {
    ...shared,
    jwt: {
      ...shared.jwt,
      // MCP OAuth discovery and grant tokens retain their existing issuer.
      // Product access tokens carry the shared explicit iss/aud in definePayload;
      // /internal/verify selects that issuer only from trusted configuration.
      issuer: `${baseURL}/api/auth`,
    },
  };
}
