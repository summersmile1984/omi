import assert from "node:assert/strict";
import test from "node:test";
import {
  FirebaseIdentityMigrationError,
  planFirebaseIdentityImport,
} from "../src/import-firebase-users.js";
import { parseFirebaseScryptConfig } from "../src/firebase-migration-password.js";

const config = parseFirebaseScryptConfig({
  algorithm: "SCRYPT",
  base64_signer_key:
    "jxspr8Ki0RYycVU8zykbdLGjFQ3McFUH0uiiTvC8pVMXAn210wjLNmdZJzxUECKbm0QsEmYUSDzZvpjeJ9WmXA==",
  base64_salt_separator: "Bw==",
  rounds: 8,
  mem_cost: 14,
});

const passwordHash =
  "lSrfV15cpx95/sZS2W9c9Kp6i/LVgQNDNC/qzrCnh1SAyZvqmZqAjTdn3aoItz+VHjoZilo78198JAdRuid5lQ==";

test("plans password, Google, and Apple identities while preserving Firebase uid", () => {
  const source = {
    users: [
      {
        localId: "firebase-uid-1",
        email: "Owner@Example.COM",
        emailVerified: true,
        displayName: "Owner",
        photoUrl: "https://images.operator.invalid/owner.png",
        passwordHash,
        salt: "42xEC+ixf3L2lw==",
        createdAt: "1700000000000",
        lastLoginAt: "1700000100000",
        providerUserInfo: [
          { providerId: "google.com", rawId: "google-sub-1" },
          { providerId: "apple.com", rawId: "apple-sub-1" },
        ],
      },
    ],
  };
  const first = planFirebaseIdentityImport(source, config);
  const second = planFirebaseIdentityImport(source, config);
  assert.equal(first.canonicalSha256, second.canonicalSha256);
  assert.equal(first.users[0].id, "firebase-uid-1");
  assert.equal(first.users[0].email, "owner@example.com");
  assert.deepEqual(
    first.accounts.map((account) => account.providerId).sort(),
    ["apple", "credential", "google"],
  );
  assert.deepEqual(first.requiredSocialProviders, ["apple", "google"]);
  assert.equal(
    first.accounts.find((account) => account.providerId === "credential")
      .accountId,
    "firebase-uid-1",
  );
});

test("fails closed for identities that cannot sign in after migration", () => {
  const base = {
    localId: "uid-1",
    email: "user@example.com",
    createdAt: "1700000000000",
  };
  assert.throws(
    () => planFirebaseIdentityImport({ users: [base] }, config),
    /no supported password, Google, or Apple/,
  );
  assert.throws(
    () =>
      planFirebaseIdentityImport(
        {
          users: [
            {
              ...base,
              disabled: true,
              passwordHash,
              salt: "42xEC+ixf3L2lw==",
            },
          ],
        },
        config,
      ),
    /disabled Firebase users require explicit remediation/,
  );
  assert.throws(
    () =>
      planFirebaseIdentityImport(
        {
          users: [
            {
              ...base,
              providerUserInfo: [
                { providerId: "facebook.com", rawId: "facebook-user" },
              ],
            },
          ],
        },
        config,
      ),
    FirebaseIdentityMigrationError,
  );
});

test("rejects duplicate email and provider authority before touching PostgreSQL", () => {
  const user = {
    emailVerified: true,
    createdAt: "1700000000000",
    providerUserInfo: [{ providerId: "google.com", rawId: "same-google-sub" }],
  };
  assert.throws(
    () =>
      planFirebaseIdentityImport(
        {
          users: [
            { ...user, localId: "uid-1", email: "same@example.com" },
            { ...user, localId: "uid-2", email: "SAME@example.com" },
          ],
        },
        config,
      ),
    /duplicate Firebase email/,
  );
  assert.throws(
    () =>
      planFirebaseIdentityImport(
        {
          users: [
            { ...user, localId: "uid-1", email: "one@example.com" },
            { ...user, localId: "uid-2", email: "two@example.com" },
          ],
        },
        config,
      ),
    /duplicate Firebase provider identity/,
  );
});
