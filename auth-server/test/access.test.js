import assert from "node:assert/strict";
import test from "node:test";
import { betterAuth } from "better-auth";
import { memoryAdapter } from "better-auth/adapters/memory";
import { jwt } from "better-auth/plugins";
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import { jwtOptions, jwtPolicy } from "../../auth/shared/jwt-policy.mjs";
import { accessHandler } from "../src/access.js";

const baseURL = "https://auth.fixture.invalid";
const internalSecret = "synthetic-internal-secret";
const { privateKey, publicKey } = await generateKeyPair("ES256");
const publicJwk = await exportJWK(publicKey);

async function fixture({ daysExpired = 1, keysUnavailable = false, sessionUnavailable = false, active = true } = {}) {
  const now = Math.floor(Date.now() / 1000);
  const token = await new SignJWT({
    uid: "existing-user", sub: "existing-user", sid: "current-session",
    iss: baseURL, aud: baseURL, iat: now, exp: now + 3600,
  }).setProtectedHeader({ alg: "ES256", kid: "rotated-key" }).sign(privateKey);
  const storedKey = {
    id: "rotated-key", alg: "ES256", publicKey: JSON.stringify(publicJwk),
    privateKey: "unused-for-verification", createdAt: new Date(0),
    expiresAt: new Date(Date.now() - daysExpired * 86400 * 1000),
  };
  // The real plugin owns publication/grace filtering. A mock verifyJWT would
  // miss its catch-all and acceptance of keys no longer present in public JWKS.
  const auth = betterAuth({
    baseURL, secret: "synthetic-better-auth-secret-at-least-32-characters",
    database: memoryAdapter({ user: [], session: [], jwks: [] }),
    plugins: [jwt({
      ...jwtOptions({}, baseURL),
      adapter: { getJwks: async () => {
        if (keysUnavailable) throw new Error("synthetic key store unavailable");
        return [storedKey];
      } },
    })],
  });
  let sessionQueries = 0;
  const handler = accessHandler({
    auth, policy: jwtPolicy({}, baseURL), secret: internalSecret,
    pool: { query: async (_sql, values) => {
      sessionQueries++;
      assert.deepEqual(values, ["current-session", "existing-user"]);
      if (sessionUnavailable) throw new Error("synthetic session store unavailable");
      return { rows: active ? [{ id: "current-session" }] : [] };
    } },
  });
  const result = { status: 200, body: undefined };
  const response = {
    status(value) { result.status = value; return this; },
    set() { return this; },
    json(value) { result.body = value; return this; },
  };
  await handler({ get: (name) => ({
    authorization: `Bearer ${token}`, "x-internal-assertion-secret": internalSecret,
  })[name] }, response);
  return { ...result, auth, sessionQueries };
}

test("a rotated key inside the publication grace window still admits an active legacy principal", async () => {
  const result = await fixture();
  assert.equal(result.status, 200);
  assert.equal(result.sessionQueries, 1);
  assert.deepEqual(result.body, {
    uid: "existing-user", sessionGeneration: "current-session", authority: "better-auth",
  });
});

test("a key outside publication grace cannot authenticate even a current session", async () => {
  const result = await fixture({ daysExpired: 3 });
  const response = await result.auth.handler(new Request(`${baseURL}/api/auth/jwks`));
  assert.deepEqual((await response.json()).keys, []);
  assert.equal(result.status, 401);
  assert.equal(result.sessionQueries, 0);
});

test("a key store failure is retryable instead of invalidating credentials", async () => {
  const result = await fixture({ keysUnavailable: true });
  assert.equal(result.status, 503);
  assert.equal(result.sessionQueries, 0);
  assert.deepEqual(result.body, { error: "identity_store_unavailable" });
});

test("a session store failure is retryable after a valid signature", async () => {
  const result = await fixture({ sessionUnavailable: true });
  assert.equal(result.status, 503);
  assert.equal(result.sessionQueries, 1);
  assert.deepEqual(result.body, { error: "identity_store_unavailable" });
});

test("a missing or revoked session rejects a cryptographically valid token", async () => {
  const result = await fixture({ active: false });
  assert.equal(result.status, 401);
  assert.equal(result.sessionQueries, 1);
  assert.deepEqual(result.body, { error: "unauthorized" });
});
