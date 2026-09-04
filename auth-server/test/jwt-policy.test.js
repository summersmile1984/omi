import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import {
  accessIdentity,
  jwtOptions,
  jwtPolicy,
  sessionPayload,
} from "../../auth/shared/jwt-policy.mjs";

const fixture = JSON.parse(
  fs.readFileSync(new URL("../../contracts/auth/claims.json", import.meta.url))
);
const policy = jwtPolicy(
  { AUTH_JWT_AUDIENCE: fixture.audience },
  fixture.issuer
);
for (const example of fixture.cases) {
  test(`shared claims: ${example.name}`, () => {
    const now = 2000000000;
    const payload = {
      sub: "existing-user",
      uid: "existing-user",
      sid: "current-session",
      iss: fixture.issuer,
      aud: fixture.audience,
      iat: now + (example.iatOffset ?? -10),
      exp: now + (example.expOffset ?? 3590),
      ...example.set,
    };
    if (example.drop) delete payload[example.drop];
    assert.equal(
      Boolean(accessIdentity(payload, policy, now)),
      Boolean(example.valid)
    );
  });
}

test("both plugins use explicit signing, lifetime and rotation options", () => {
  const options = jwtOptions({}, fixture.issuer);
  assert.deepEqual(options.jwks, {
    keyPairConfig: { alg: "ES256" },
    rotationInterval: 2592000,
    gracePeriod: 172800,
  });
  assert.equal(options.jwt.expirationTime, "3600s");
  assert.equal(options.jwt.issuer, fixture.issuer);
  assert.equal(options.jwt.audience, fixture.issuer);
  assert.deepEqual(
    options.jwt.definePayload({
      user: { id: "legacy" },
      session: { id: "s", userId: "legacy" },
    }),
    { uid: "legacy", sid: "s", iss: fixture.issuer, aud: fixture.issuer }
  );
  assert.throws(() =>
    sessionPayload({ user: { id: "u" }, session: { id: "s", userId: "other" } })
  );
  assert.throws(() =>
    jwtOptions({ AUTH_JWKS_GRACE_SECONDS: "3599" }, fixture.issuer)
  );
  assert.throws(() => jwtPolicy({}, "https://auth.fixture.invalid/path"));
});
