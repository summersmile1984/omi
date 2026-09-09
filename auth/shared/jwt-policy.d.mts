export const JWT_LIFETIME_SECONDS: 3600;
export const JWT_ROTATION_SECONDS: number;
export const JWT_GRACE_SECONDS: number;
export type JwtEnvironment = {
  AUTH_JWT_ISSUER?: string;
  AUTH_JWT_AUDIENCE?: string;
  AUTH_JWKS_ROTATION_SECONDS?: string;
  AUTH_JWKS_GRACE_SECONDS?: string;
};
export type JwtPolicy = {
  issuer: string;
  audience: string;
  rotation: number;
  grace: number;
};
export type SessionIdentity = {
  user: { id: string };
  session: { id: string; userId: string };
};
export function jwtPolicy(env: JwtEnvironment, baseURL: string): JwtPolicy;
export function sessionPayload(identity: SessionIdentity): {
  uid: string;
  sid: string;
};
export function jwtOptions(
  env: JwtEnvironment,
  baseURL: string,
): {
  jwks: {
    keyPairConfig: { alg: "ES256" };
    rotationInterval: number;
    gracePeriod: number;
  };
  jwt: {
    issuer: string;
    audience: string;
    expirationTime: string;
    definePayload: (identity: SessionIdentity) => {
      uid: string;
      sid: string;
      iss: string;
      aud: string;
    };
  };
};
export function accessIdentity(
  payload: unknown,
  policy: Pick<JwtPolicy, "issuer" | "audience">,
  now?: number,
): { uid: string; sid: string } | null;
