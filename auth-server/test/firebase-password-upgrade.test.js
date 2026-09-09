import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { betterAuth } from "better-auth";
import { memoryAdapter } from "better-auth/adapters/memory";
import { bearer } from "better-auth/plugins";
import {
  encodeFirebasePasswordHash,
  isFirebasePasswordHash,
  serverFirebaseScrypt,
} from "../../auth/shared/firebase-scrypt.mjs";
import {
  hashPassword,
  verifyPassword,
} from "../src/firebase-migration-password.js";
import {
  firebasePasswordUpgradeHook,
  upgradeMigratedFirebasePassword,
} from "../src/firebase-password-upgrade.js";

const sample = JSON.parse(
  readFileSync(
    new URL("../../contracts/auth/firebase-scrypt.json", import.meta.url),
    "utf8",
  ),
);
const env = {
  AUTH_FIREBASE_SCRYPT_SIGNER_KEY: sample.config.base64_signer_key,
  AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR: sample.config.base64_salt_separator,
  AUTH_FIREBASE_SCRYPT_ROUNDS: String(sample.config.rounds),
  AUTH_FIREBASE_SCRYPT_MEM_COST: String(sample.config.mem_cost),
};
const envelope = encodeFirebasePasswordHash(
  sample,
  serverFirebaseScrypt.parseConfig(sample.config),
);
const baseURL = "https://auth.fixture.invalid";

function poolFor(rows, { failUpdate = false, concurrentPassword } = {}) {
  return {
    query: async (sql, values) => {
      if (sql.startsWith("SELECT")) {
        return {
          rows: rows
            .filter(
              (r) => r.userId === values[0] && r.providerId === "credential",
            )
            .map((r) => ({ ...r })),
        };
      }
      assert.ok(sql.includes("WHERE id = $3 AND password = $4"));
      if (failUpdate)
        throw new Error("synthetic database outage with private query details");
      const row = rows.find((r) => r.id === values[2]);
      if (concurrentPassword && row) row.password = concurrentPassword;
      if (!row || row.password !== values[3]) return { rowCount: 0 };
      row.password = values[0];
      row.updatedAt = values[1];
      return { rowCount: 1 };
    },
  };
}

async function signInFixture(options = {}) {
  const database = {
    user: [
      {
        id: "imported-user",
        name: "Synthetic",
        email: "synthetic@example.invalid",
        emailVerified: true,
        createdAt: new Date(),
        updatedAt: new Date(),
      },
    ],
    account: [
      {
        id: "imported-credential",
        userId: "imported-user",
        accountId: "imported-user",
        providerId: "credential",
        password: envelope,
        createdAt: new Date(),
        updatedAt: new Date(),
      },
    ],
    session: [],
    verification: [],
  };
  const auth = betterAuth({
    baseURL,
    secret: "synthetic-better-auth-secret-at-least-32-characters",
    database: memoryAdapter(database),
    emailAndPassword: {
      enabled: true,
      password: {
        hash: hashPassword,
        verify: (credentials) =>
          isFirebasePasswordHash(credentials.hash)
            ? serverFirebaseScrypt.verify(credentials, env)
            : verifyPassword(credentials),
      },
    },
    hooks: {
      after: firebasePasswordUpgradeHook(
        poolFor(database.account, options),
        env,
      ),
    },
    plugins: [bearer({ requireSignature: true })],
  });
  const signIn = (password) =>
    auth.handler(
      new Request(`${baseURL}/api/auth/sign-in/email`, {
        method: "POST",
        headers: { "content-type": "application/json", origin: baseURL },
        body: JSON.stringify({ email: "synthetic@example.invalid", password }),
      }),
    );
  return { auth, database, signIn };
}

test("real sign-in upgrades a legacy principal only after a correct password creates its session", async () => {
  const { signIn, database } = await signInFixture();
  assert.equal((await signIn("wrong-password")).status, 401);
  assert.equal(database.account[0].password, envelope);
  assert.equal(database.session.length, 0);
  const response = await signIn(sample.password);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).user.id, "imported-user");
  assert.ok(response.headers.get("set-auth-token"));
  assert.equal(database.session.length, 1);
  assert.equal(isFirebasePasswordHash(database.account[0].password), false);
  const replacement = database.account[0].password;
  assert.equal((await signIn(sample.password)).status, 200);
  assert.equal(database.account[0].password, replacement);
});

test("failed upgrade preserves the verified session and emits only bounded shared telemetry", async () => {
  const { auth, signIn, database } = await signInFixture({ failUpdate: true });
  const events = [];
  const original = console.warn;
  console.warn = (value) => events.push(JSON.parse(value));
  let response;
  try {
    response = await signIn(sample.password);
  } finally {
    console.warn = original;
  }
  assert.equal(response.status, 200);
  assert.equal(database.account[0].password, envelope);
  const session = await auth.handler(
    new Request(`${baseURL}/api/auth/get-session`, {
      headers: {
        authorization: `Bearer ${response.headers.get("set-auth-token")}`,
      },
    }),
  );
  assert.equal((await session.json()).user.id, "imported-user");
  assert.deepEqual(events, [
    {
      event: "fallback",
      component: "other",
      from: "postgres",
      to: "none",
      reason: "dependency_unavailable",
      outcome: "degraded",
    },
  ]);
});

test("native and absent credentials avoid hashing; concurrent reset wins the conditional update", async () => {
  const rows = [
    {
      id: "credential",
      userId: "user",
      providerId: "credential",
      password: "native-current",
    },
  ];
  let calls = 0;
  const hash = async () => {
    calls++;
    return "native-new";
  };
  assert.equal(
    await upgradeMigratedFirebasePassword(
      poolFor(rows),
      "missing",
      sample.password,
      env,
      hash,
    ),
    "not_migrated",
  );
  assert.equal(
    await upgradeMigratedFirebasePassword(
      poolFor(rows),
      "user",
      sample.password,
      env,
      hash,
    ),
    "not_migrated",
  );
  assert.equal(calls, 0);
  rows[0].password = envelope;
  assert.equal(
    await upgradeMigratedFirebasePassword(
      poolFor(rows, { concurrentPassword: "native-reset" }),
      "user",
      sample.password,
      env,
      hash,
    ),
    "credential_changed",
  );
  assert.equal(rows[0].password, "native-reset");
  assert.equal(calls, 1);
});

test("a changed migrated password or ambiguous ownership never writes an old sign-in password", async () => {
  const rows = [
    {
      id: "credential",
      userId: "user",
      providerId: "credential",
      password: envelope,
    },
  ];
  const mustNotHash = async () => {
    throw new Error("must not hash");
  };
  assert.equal(
    await upgradeMigratedFirebasePassword(
      poolFor(rows),
      "user",
      "old-password",
      env,
      mustNotHash,
    ),
    "credential_changed",
  );
  assert.equal(rows[0].password, envelope);
  rows.push({ ...rows[0], id: "duplicate" });
  await assert.rejects(
    upgradeMigratedFirebasePassword(
      poolFor(rows),
      "user",
      sample.password,
      env,
      mustNotHash,
    ),
    /multiple credential accounts/,
  );
});
