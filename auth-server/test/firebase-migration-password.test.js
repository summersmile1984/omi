import {
  hashPassword,
  verifyPassword,
} from "../src/firebase-migration-password.js";
import assert from "node:assert/strict";
import test from "node:test";
import {
  FirebasePasswordMigrationConfigurationError,
  encodeFirebasePasswordHash,
  isFirebasePasswordHash,
  serverFirebaseScrypt,
} from "../../auth/shared/firebase-scrypt.mjs";

import { readFileSync } from "node:fs";
const OFFICIAL_FIREBASE_SAMPLE = JSON.parse(
  readFileSync(
    new URL("../../contracts/auth/firebase-scrypt.json", import.meta.url),
    "utf8",
  ),
);

function envFor(config) {
  return {
    AUTH_FIREBASE_SCRYPT_SIGNER_KEY: config.base64_signer_key,
    AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR: config.base64_salt_separator,
    AUTH_FIREBASE_SCRYPT_ROUNDS: String(config.rounds),
    AUTH_FIREBASE_SCRYPT_MEM_COST: String(config.mem_cost),
  };
}

test("verifies the password sample published by Firebase", async () => {
  const config = serverFirebaseScrypt.parseConfig(
    OFFICIAL_FIREBASE_SAMPLE.config,
  );
  const hash = encodeFirebasePasswordHash(OFFICIAL_FIREBASE_SAMPLE, config);
  assert.equal(isFirebasePasswordHash(hash), true);
  assert.equal(
    await serverFirebaseScrypt.verify(
      { hash, password: OFFICIAL_FIREBASE_SAMPLE.password },
      envFor(OFFICIAL_FIREBASE_SAMPLE.config),
    ),
    true,
  );
  assert.equal(
    await serverFirebaseScrypt.verify(
      { hash, password: "definitely-wrong" },
      envFor(OFFICIAL_FIREBASE_SAMPLE.config),
    ),
    false,
  );
});

test("fails closed before password work when migration configuration is absent or mismatched", async () => {
  const config = serverFirebaseScrypt.parseConfig(
    OFFICIAL_FIREBASE_SAMPLE.config,
  );
  const hash = encodeFirebasePasswordHash(OFFICIAL_FIREBASE_SAMPLE, config);
  await assert.rejects(
    serverFirebaseScrypt.verify({ hash, password: "secret" }, {}),
    FirebasePasswordMigrationConfigurationError,
  );
  await assert.rejects(
    serverFirebaseScrypt.verify(
      { hash, password: "secret" },
      {
        ...envFor(OFFICIAL_FIREBASE_SAMPLE.config),
        AUTH_FIREBASE_SCRYPT_ROUNDS: "7",
      },
    ),
    /fingerprint does not match/,
  );
});

test("keeps new Better Auth passwords on its native hash while accepting migrated envelopes", async () => {
  const nativeHash = await hashPassword("new-account-password");
  assert.equal(isFirebasePasswordHash(nativeHash), false);
  assert.equal(
    await verifyPassword({
      hash: nativeHash,
      password: "new-account-password",
    }),
    true,
  );
  assert.equal(
    await verifyPassword({ hash: nativeHash, password: "wrong-password" }),
    false,
  );
});

test("rejects unsafe or incomplete Firebase scrypt parameters", () => {
  assert.throws(
    () =>
      serverFirebaseScrypt.parseConfig({
        ...OFFICIAL_FIREBASE_SAMPLE.config,
        mem_cost: 30,
      }),
    /mem_cost must be an integer/,
  );
  assert.throws(
    () =>
      serverFirebaseScrypt.parseConfig({
        ...OFFICIAL_FIREBASE_SAMPLE.config,
        rounds: 0,
      }),
    /rounds must be an integer/,
  );
  assert.throws(
    () =>
      encodeFirebasePasswordHash(
        { passwordHash: "AQ==", passwordSalt: "Ag==" },
        serverFirebaseScrypt.parseConfig(OFFICIAL_FIREBASE_SAMPLE.config),
      ),
    /hash length must match/,
  );
});

test("the Server policy retains its existing wider imported-principal budget", () => {
  const config = serverFirebaseScrypt.parseConfig({
    ...OFFICIAL_FIREBASE_SAMPLE.config,
    rounds: 9,
    mem_cost: 18,
  });
  assert.equal(config.rounds, 9);
  assert.equal(config.memCost, 18);
});
