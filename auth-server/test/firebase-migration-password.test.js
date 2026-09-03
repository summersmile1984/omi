import assert from "node:assert/strict";
import test from "node:test";
import {
  FirebasePasswordMigrationConfigurationError,
  encodeFirebasePasswordHash,
  hashPassword,
  isFirebasePasswordHash,
  parseFirebaseScryptConfig,
  verifyMigratedFirebasePassword,
  verifyPassword,
} from "../src/firebase-migration-password.js";

const OFFICIAL_FIREBASE_SAMPLE = Object.freeze({
  config: {
    algorithm: "SCRYPT",
    base64_signer_key:
      "jxspr8Ki0RYycVU8zykbdLGjFQ3McFUH0uiiTvC8pVMXAn210wjLNmdZJzxUECKbm0QsEmYUSDzZvpjeJ9WmXA==",
    base64_salt_separator: "Bw==",
    rounds: 8,
    mem_cost: 14,
  },
  passwordSalt: "42xEC+ixf3L2lw==",
  passwordHash:
    "lSrfV15cpx95/sZS2W9c9Kp6i/LVgQNDNC/qzrCnh1SAyZvqmZqAjTdn3aoItz+VHjoZilo78198JAdRuid5lQ==",
  password: "user1password",
});

function envFor(config) {
  return {
    AUTH_FIREBASE_SCRYPT_SIGNER_KEY: config.base64_signer_key,
    AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR: config.base64_salt_separator,
    AUTH_FIREBASE_SCRYPT_ROUNDS: String(config.rounds),
    AUTH_FIREBASE_SCRYPT_MEM_COST: String(config.mem_cost),
  };
}

test("verifies the password sample published by Firebase", async () => {
  const config = parseFirebaseScryptConfig(OFFICIAL_FIREBASE_SAMPLE.config);
  const hash = encodeFirebasePasswordHash(OFFICIAL_FIREBASE_SAMPLE, config);
  assert.equal(isFirebasePasswordHash(hash), true);
  assert.equal(
    await verifyMigratedFirebasePassword(
      { hash, password: OFFICIAL_FIREBASE_SAMPLE.password },
      envFor(OFFICIAL_FIREBASE_SAMPLE.config),
    ),
    true,
  );
  assert.equal(
    await verifyMigratedFirebasePassword(
      { hash, password: "definitely-wrong" },
      envFor(OFFICIAL_FIREBASE_SAMPLE.config),
    ),
    false,
  );
});

test("fails closed before password work when migration configuration is absent or mismatched", async () => {
  const config = parseFirebaseScryptConfig(OFFICIAL_FIREBASE_SAMPLE.config);
  const hash = encodeFirebasePasswordHash(OFFICIAL_FIREBASE_SAMPLE, config);
  await assert.rejects(
    verifyMigratedFirebasePassword({ hash, password: "secret" }, {}),
    FirebasePasswordMigrationConfigurationError,
  );
  await assert.rejects(
    verifyMigratedFirebasePassword(
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
      parseFirebaseScryptConfig({
        ...OFFICIAL_FIREBASE_SAMPLE.config,
        mem_cost: 30,
      }),
    /mem_cost must be an integer/,
  );
  assert.throws(
    () =>
      parseFirebaseScryptConfig({
        ...OFFICIAL_FIREBASE_SAMPLE.config,
        rounds: 0,
      }),
    /rounds must be an integer/,
  );
  assert.throws(
    () =>
      encodeFirebasePasswordHash(
        { passwordHash: "AQ==", passwordSalt: "Ag==" },
        parseFirebaseScryptConfig(OFFICIAL_FIREBASE_SAMPLE.config),
      ),
    /hash length must match/,
  );
});
