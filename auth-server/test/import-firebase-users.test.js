import assert from "node:assert/strict";
import {
  chmod,
  mkdtemp,
  readFile,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import test from "node:test";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
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

function runValidate(usersPath, hashConfigPath) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [
        path.resolve("src/import-firebase-users.js"),
        "validate",
        "--users",
        usersPath,
        "--hash-config",
        hashConfigPath,
      ],
      { cwd: path.resolve("."), stdio: ["ignore", "pipe", "pipe"] },
    );
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (status) => resolve({ status, stderr }));
  });
}

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

test("CLI rejects symlinked or group/world-readable Firebase import artifacts", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "omi-auth-import-"));
  try {
    const users = path.join(root, "users.json");
    const hashConfig = path.join(root, "hash-config.json");
    const sourceUsers = await readFile(
      path.resolve("test/fixtures/firebase-users.sample.json"),
    );
    const sourceHashConfig = await readFile(
      path.resolve("test/fixtures/firebase-hash-config.sample.json"),
    );
    await writeFile(users, sourceUsers);
    await writeFile(hashConfig, sourceHashConfig);
    await chmod(users, 0o644);
    await chmod(hashConfig, 0o600);
    const modeResult = await runValidate(users, hashConfig);
    assert.equal(modeResult.status, 2);
    assert.match(modeResult.stderr, /mode 0600/);

    await chmod(users, 0o600);
    const symlinkPath = path.join(root, "users-link.json");
    await symlink(users, symlinkPath);
    const symlinkResult = await runValidate(symlinkPath, hashConfig);
    assert.equal(symlinkResult.status, 2);
    assert.match(symlinkResult.stderr, /regular file, not a symlink/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
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

test("fails closed instead of silently dropping unsupported Firebase profile data", () => {
  const base = {
    localId: "uid-profile-data",
    email: "profile@example.com",
    createdAt: "1700000000000",
    passwordHash,
    salt: "42xEC+ixf3L2lw==",
  };
  assert.throws(
    () =>
      planFirebaseIdentityImport(
        { users: [{ ...base, customAttributes: '{"plan":"pro"}' }] },
        config,
      ),
    /non-empty customAttributes require explicit reconciliation/,
  );
  assert.throws(
    () =>
      planFirebaseIdentityImport(
        { users: [{ ...base, phoneNumber: "+15551234567" }] },
        config,
      ),
    /phoneNumber identities require explicit reconciliation/,
  );
  assert.doesNotThrow(() =>
    planFirebaseIdentityImport(
      { users: [{ ...base, customAttributes: "{}" }] },
      config,
    ),
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
