/** Shared Better Auth access-token contract for PostgreSQL and Workers. */
export const JWT_LIFETIME_SECONDS = 3600;
export const JWT_ROTATION_SECONDS = 30 * 24 * 3600;
export const JWT_GRACE_SECONDS = 2 * 24 * 3600;

function origin(value, name) {
  const url = new URL(value);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/"
  ) {
    throw new Error(`${name} must be an HTTP(S) origin`);
  }
  return url.origin;
}

function seconds(value, fallback, name) {
  if (value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new Error(`${name} must be positive integer seconds`);
  }
  return parsed;
}

export function jwtPolicy(env, baseURL) {
  const issuer = origin(env.AUTH_JWT_ISSUER || baseURL, "AUTH_JWT_ISSUER");
  const audience = origin(env.AUTH_JWT_AUDIENCE || issuer, "AUTH_JWT_AUDIENCE");
  const rotation = seconds(
    env.AUTH_JWKS_ROTATION_SECONDS,
    JWT_ROTATION_SECONDS,
    "AUTH_JWKS_ROTATION_SECONDS",
  );
  const grace = seconds(
    env.AUTH_JWKS_GRACE_SECONDS,
    JWT_GRACE_SECONDS,
    "AUTH_JWKS_GRACE_SECONDS",
  );
  if (grace < JWT_LIFETIME_SECONDS)
    throw new Error("JWKS grace must cover the access-token lifetime");
  return { issuer, audience, rotation, grace };
}

export function sessionPayload({ user, session }) {
  if (
    typeof user?.id !== "string" ||
    !user.id ||
    typeof session?.id !== "string" ||
    !session.id ||
    session.userId !== user.id
  ) {
    throw new Error(
      "A live session owned by the user is required to issue an access token",
    );
  }
  return { uid: user.id, sid: session.id };
}

export function jwtOptions(env, baseURL) {
  const policy = jwtPolicy(env, baseURL);
  return {
    jwks: {
      keyPairConfig: { alg: "ES256" },
      rotationInterval: policy.rotation,
      gracePeriod: policy.grace,
    },
    jwt: {
      issuer: policy.issuer,
      audience: policy.audience,
      expirationTime: `${JWT_LIFETIME_SECONDS}s`,
      definePayload: (identity) => ({
        ...sessionPayload(identity),
        iss: policy.issuer,
        aud: policy.audience,
      }),
    },
  };
}

/** This checks claims only AFTER the target's cryptographic verifier succeeds. */
export function accessIdentity(
  payload,
  policy,
  now = Math.floor(Date.now() / 1000),
) {
  if (
    !payload ||
    typeof payload !== "object" ||
    typeof payload.sub !== "string" ||
    !payload.sub ||
    payload.uid !== payload.sub ||
    typeof payload.sid !== "string" ||
    !payload.sid ||
    payload.iss !== policy.issuer ||
    payload.aud !== policy.audience ||
    !Number.isSafeInteger(payload.iat) ||
    !Number.isSafeInteger(payload.exp) ||
    payload.iat > now ||
    payload.exp <= now ||
    payload.exp <= payload.iat ||
    payload.exp - payload.iat > JWT_LIFETIME_SECONDS
  )
    return null;
  return { uid: payload.sub, sid: payload.sid };
}
