#!/usr/bin/env node
// LIFECYCLE: permanent
// Fail-closed Firebase Auth export -> Better Auth identity migration.
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";
import {
  encodeFirebasePasswordHash,
  parseFirebaseScryptConfig,
} from "./firebase-migration-password.js";
import {
  assertRequiredSocialProviders,
  SocialProviderConfigurationError,
} from "./social-providers.js";

const { Pool } = pg;
const FIREBASE_EXPORT_FIELDS = new Set([
  "localId",
  "email",
  "emailVerified",
  "displayName",
  "photoUrl",
  "passwordHash",
  "salt",
  "passwordSalt",
  "createdAt",
  "lastLoginAt",
  "providerUserInfo",
  "disabled",
  "customAttributes",
  "phoneNumber",
  "lastRefreshAt",
]);
const PROVIDER_MAP = new Map([
  ["google.com", "google"],
  ["apple.com", "apple"],
]);
const SHA256 = /^[0-9a-f]{64}$/;

export class FirebaseIdentityMigrationError extends Error {
  constructor(message) {
    super(message);
    this.name = "FirebaseIdentityMigrationError";
  }
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function parseJson(raw, label) {
  try {
    return JSON.parse(raw.toString("utf8"));
  } catch (error) {
    throw new FirebaseIdentityMigrationError(`${label} is not valid UTF-8 JSON`);
  }
}

function requiredString(value, label) {
  if (typeof value !== "string" || !value.trim()) {
    throw new FirebaseIdentityMigrationError(`${label} must be a non-empty string`);
  }
  return value.trim();
}

function optionalString(value, label) {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "string") {
    throw new FirebaseIdentityMigrationError(`${label} must be a string`);
  }
  return value.trim() || null;
}

function firebaseTimestamp(value, label, fallback = null) {
  if (value === undefined || value === null || value === "") {
    if (fallback) return fallback;
    throw new FirebaseIdentityMigrationError(`${label} is required`);
  }
  const milliseconds = Number(value);
  if (!Number.isSafeInteger(milliseconds) || milliseconds < 0) {
    throw new FirebaseIdentityMigrationError(`${label} must be epoch milliseconds`);
  }
  const parsed = new Date(milliseconds);
  if (!Number.isFinite(parsed.getTime())) {
    throw new FirebaseIdentityMigrationError(`${label} is outside the supported date range`);
  }
  return parsed.toISOString();
}

function deterministicAccountRowId(providerId, accountId) {
  return `fbmig_${sha256(`${providerId}\0${accountId}`).slice(0, 48)}`;
}

function normalizeProviderAccounts(user, userId, timestamps) {
  const rawProviders = user.providerUserInfo ?? [];
  if (!Array.isArray(rawProviders)) {
    throw new FirebaseIdentityMigrationError(
      `user ${userId}: providerUserInfo must be an array`,
    );
  }
  const accounts = [];
  const seenProviders = new Set();
  for (const [index, provider] of rawProviders.entries()) {
    if (!provider || typeof provider !== "object" || Array.isArray(provider)) {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: providerUserInfo[${index}] must be an object`,
      );
    }
    const firebaseProvider = requiredString(
      provider.providerId,
      `user ${userId}: providerUserInfo[${index}].providerId`,
    );
    const providerId = PROVIDER_MAP.get(firebaseProvider);
    if (!providerId) {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: unsupported Firebase provider ${firebaseProvider}`,
      );
    }
    if (seenProviders.has(providerId)) {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: duplicate ${providerId} provider account`,
      );
    }
    seenProviders.add(providerId);
    const accountId = requiredString(
      provider.rawId,
      `user ${userId}: providerUserInfo[${index}].rawId`,
    );
    accounts.push({
      id: deterministicAccountRowId(providerId, accountId),
      accountId,
      providerId,
      userId,
      password: null,
      ...timestamps,
    });
  }
  return accounts;
}

export function planFirebaseIdentityImport(source, hashConfig) {
  if (!source || typeof source !== "object" || Array.isArray(source)) {
    throw new FirebaseIdentityMigrationError("Firebase export must be an object");
  }
  if (!Array.isArray(source.users)) {
    throw new FirebaseIdentityMigrationError("Firebase export must contain a users array");
  }
  const users = [];
  const accounts = [];
  const seenIds = new Set();
  const seenEmails = new Set();
  const seenProviderAccounts = new Set();
  for (const [index, rawUser] of source.users.entries()) {
    if (!rawUser || typeof rawUser !== "object" || Array.isArray(rawUser)) {
      throw new FirebaseIdentityMigrationError(`users[${index}] must be an object`);
    }
    const unknown = Object.keys(rawUser).filter(
      (key) => !FIREBASE_EXPORT_FIELDS.has(key),
    );
    if (unknown.length) {
      throw new FirebaseIdentityMigrationError(
        `users[${index}] contains unsupported fields: ${unknown.sort().join(", ")}`,
      );
    }
    const userId = requiredString(rawUser.localId, `users[${index}].localId`);
    if (seenIds.has(userId)) {
      throw new FirebaseIdentityMigrationError(`duplicate Firebase uid ${userId}`);
    }
    seenIds.add(userId);
    if (rawUser.disabled === true) {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: disabled Firebase users require explicit remediation before import`,
      );
    }
    const customAttributes = optionalString(
      rawUser.customAttributes,
      `user ${userId}: customAttributes`,
    );
    if (customAttributes && customAttributes !== "{}") {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: non-empty customAttributes require explicit reconciliation before import`,
      );
    }
    const phoneNumber = optionalString(
      rawUser.phoneNumber,
      `user ${userId}: phoneNumber`,
    );
    if (phoneNumber) {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: phoneNumber identities require explicit reconciliation before import`,
      );
    }
    const email = requiredString(rawUser.email, `user ${userId}: email`).toLowerCase();
    if (seenEmails.has(email)) {
      throw new FirebaseIdentityMigrationError(`duplicate Firebase email ${email}`);
    }
    seenEmails.add(email);
    const createdAt = firebaseTimestamp(
      rawUser.createdAt,
      `user ${userId}: createdAt`,
    );
    const updatedAt = firebaseTimestamp(
      rawUser.lastLoginAt,
      `user ${userId}: lastLoginAt`,
      createdAt,
    );
    const timestamps = { createdAt, updatedAt };
    users.push({
      id: userId,
      name:
        optionalString(rawUser.displayName, `user ${userId}: displayName`) ??
        email.split("@")[0] ??
        "Imported User",
      email,
      emailVerified: rawUser.emailVerified === true,
      image: optionalString(rawUser.photoUrl, `user ${userId}: photoUrl`),
      ...timestamps,
    });
    const passwordHash = optionalString(
      rawUser.passwordHash,
      `user ${userId}: passwordHash`,
    );
    const passwordSalt = optionalString(
      rawUser.passwordSalt ?? rawUser.salt,
      `user ${userId}: passwordSalt`,
    );
    if (Boolean(passwordHash) !== Boolean(passwordSalt)) {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: passwordHash and passwordSalt must be present together`,
      );
    }
    if (passwordHash && passwordSalt) {
      accounts.push({
        id: deterministicAccountRowId("credential", userId),
        accountId: userId,
        providerId: "credential",
        userId,
        password: encodeFirebasePasswordHash(
          { passwordHash, passwordSalt },
          hashConfig,
        ),
        ...timestamps,
      });
    }
    const socialAccounts = normalizeProviderAccounts(
      rawUser,
      userId,
      timestamps,
    );
    for (const account of socialAccounts) {
      const identity = `${account.providerId}\0${account.accountId}`;
      if (seenProviderAccounts.has(identity)) {
        throw new FirebaseIdentityMigrationError(
          `duplicate Firebase provider identity ${account.providerId}`,
        );
      }
      seenProviderAccounts.add(identity);
      accounts.push(account);
    }
    if (!passwordHash && socialAccounts.length === 0) {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: no supported password, Google, or Apple sign-in identity`,
      );
    }
  }
  users.sort((left, right) => left.id.localeCompare(right.id));
  accounts.sort((left, right) => left.id.localeCompare(right.id));
  const canonical = { users, accounts };
  return Object.freeze({
    users,
    accounts,
    canonicalSha256: sha256(stableJson(canonical)),
    configFingerprint: hashConfig.fingerprint,
    requiredSocialProviders: Object.freeze(
      [...new Set(accounts.map((account) => account.providerId))]
        .filter((provider) => provider !== "credential")
        .sort(),
    ),
  });
}

function parseArguments(argv) {
  const [command, ...rest] = argv;
  if (!new Set(["validate", "apply", "verify"]).has(command)) {
    throw new FirebaseIdentityMigrationError(
      "usage: import-firebase-users.js validate|apply|verify --users FILE --hash-config FILE",
    );
  }
  const options = {};
  for (let index = 0; index < rest.length; index += 2) {
    const flag = rest[index];
    const value = rest[index + 1];
    if (!new Set(["--users", "--hash-config"]).has(flag) || !value) {
      throw new FirebaseIdentityMigrationError(`invalid argument ${flag || "<missing>"}`);
    }
    options[flag.slice(2)] = value;
  }
  if (!options.users || !options["hash-config"]) {
    throw new FirebaseIdentityMigrationError("--users and --hash-config are required");
  }
  return { command, usersPath: options.users, hashConfigPath: options["hash-config"] };
}

async function loadPlan(usersPath, hashConfigPath) {
  const [usersRaw, configRaw] = await Promise.all([
    readFile(usersPath),
    readFile(hashConfigPath),
  ]);
  const source = parseJson(usersRaw, "Firebase user export");
  const configDocument = parseJson(configRaw, "Firebase hash configuration");
  const config = parseFirebaseScryptConfig(
    configDocument.hash_config ?? configDocument,
  );
  return {
    plan: planFirebaseIdentityImport(source, config),
    sourceSha256: sha256(usersRaw),
  };
}

async function assertSchema(client) {
  const result = await client.query(
    `SELECT table_name FROM information_schema.tables
     WHERE table_schema = current_schema()
       AND table_name IN ('user', 'account', 'session', 'auth_identity_imports')`,
  );
  const present = new Set(result.rows.map((row) => row.table_name));
  const missing = ["user", "account", "session", "auth_identity_imports"].filter(
    (table) => !present.has(table),
  );
  if (missing.length) {
    throw new FirebaseIdentityMigrationError(
      `Better Auth schema is missing: ${missing.join(", ")}`,
    );
  }
}

function canonicalizeDatabaseRows(userRows, accountRows) {
  return {
    users: userRows.map((row) => ({
      id: row.id,
      name: row.name,
      email: row.email,
      emailVerified: row.emailVerified,
      image: row.image,
      createdAt: new Date(row.createdAt).toISOString(),
      updatedAt: new Date(row.updatedAt).toISOString(),
    })),
    accounts: accountRows.map((row) => ({
      id: row.id,
      accountId: row.accountId,
      providerId: row.providerId,
      userId: row.userId,
      password: row.password,
      createdAt: new Date(row.createdAt).toISOString(),
      updatedAt: new Date(row.updatedAt).toISOString(),
    })),
  };
}

async function verifyDatabase(client, plan, sourceSha256) {
  const ledger = await client.query(
    `SELECT "sourceSha256", "configFingerprint", "canonicalSha256",
            "userCount", "accountCount"
     FROM "auth_identity_imports"`,
  );
  if (ledger.rows.length !== 1) {
    throw new FirebaseIdentityMigrationError(
      "identity import ledger must contain exactly one completed source",
    );
  }
  const record = ledger.rows[0];
  if (
    record.sourceSha256 !== sourceSha256 ||
    record.configFingerprint !== plan.configFingerprint ||
    record.canonicalSha256 !== plan.canonicalSha256 ||
    record.userCount !== plan.users.length ||
    record.accountCount !== plan.accounts.length
  ) {
    throw new FirebaseIdentityMigrationError(
      "identity import ledger does not match the requested source",
    );
  }
  const users = await client.query(
    `SELECT id, name, email, "emailVerified", image, "createdAt", "updatedAt"
     FROM "user" ORDER BY id COLLATE "C"`,
  );
  const accounts = await client.query(
    `SELECT id, "accountId", "providerId", "userId", password, "createdAt", "updatedAt"
     FROM "account" ORDER BY id COLLATE "C"`,
  );
  const sessions = await client.query(
    `SELECT count(*)::int AS count FROM "session"`,
  );
  if (sessions.rows[0].count !== 0) {
    throw new FirebaseIdentityMigrationError(
      "identity migration verification requires zero pre-cutover sessions",
    );
  }
  const canonical = canonicalizeDatabaseRows(users.rows, accounts.rows);
  const actualSha256 = sha256(stableJson(canonical));
  if (actualSha256 !== plan.canonicalSha256) {
    throw new FirebaseIdentityMigrationError(
      "Better Auth user/account reconciliation hash does not match the Firebase export",
    );
  }
  return {
    status: "verified",
    source_sha256: sourceSha256,
    canonical_sha256: actualSha256,
    config_fingerprint: plan.configFingerprint,
    users: users.rows.length,
    accounts: accounts.rows.length,
    sessions: 0,
  };
}

async function applyDatabase(client, plan, sourceSha256) {
  await client.query("BEGIN ISOLATION LEVEL SERIALIZABLE");
  try {
    await client.query("SELECT pg_advisory_xact_lock(hashtext('omi-auth-identity-import'))");
    await assertSchema(client);
    const existingLedger = await client.query(
      `SELECT "sourceSha256" FROM "auth_identity_imports"`,
    );
    if (existingLedger.rows.length) {
      const result = await verifyDatabase(client, plan, sourceSha256);
      await client.query("COMMIT");
      return { ...result, status: "already_applied" };
    }
    const counts = await client.query(
      `SELECT
         (SELECT count(*)::int FROM "user") AS users,
         (SELECT count(*)::int FROM "account") AS accounts,
         (SELECT count(*)::int FROM "session") AS sessions`,
    );
    if (Object.values(counts.rows[0]).some((count) => count !== 0)) {
      throw new FirebaseIdentityMigrationError(
        "identity import target must contain no Better Auth users, accounts, or sessions",
      );
    }
    for (const user of plan.users) {
      await client.query(
        `INSERT INTO "user"
           (id, name, email, "emailVerified", image, "createdAt", "updatedAt")
         VALUES ($1, $2, $3, $4, $5, $6, $7)`,
        [
          user.id,
          user.name,
          user.email,
          user.emailVerified,
          user.image,
          user.createdAt,
          user.updatedAt,
        ],
      );
    }
    for (const account of plan.accounts) {
      await client.query(
        `INSERT INTO "account"
           (id, "accountId", "providerId", "userId", password, "createdAt", "updatedAt")
         VALUES ($1, $2, $3, $4, $5, $6, $7)`,
        [
          account.id,
          account.accountId,
          account.providerId,
          account.userId,
          account.password,
          account.createdAt,
          account.updatedAt,
        ],
      );
    }
    await client.query(
      `INSERT INTO "auth_identity_imports"
         ("sourceSha256", "configFingerprint", "canonicalSha256", "userCount", "accountCount")
       VALUES ($1, $2, $3, $4, $5)`,
      [
        sourceSha256,
        plan.configFingerprint,
        plan.canonicalSha256,
        plan.users.length,
        plan.accounts.length,
      ],
    );
    const result = await verifyDatabase(client, plan, sourceSha256);
    await client.query("COMMIT");
    return { ...result, status: "applied" };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  }
}

export async function runIdentityImport(
  command,
  plan,
  sourceSha256,
  databaseUrl,
  env = process.env,
) {
  if (!SHA256.test(sourceSha256)) {
    throw new FirebaseIdentityMigrationError("source SHA-256 is invalid");
  }
  if (command === "validate") {
    return {
      status: "validated",
      source_sha256: sourceSha256,
      canonical_sha256: plan.canonicalSha256,
      config_fingerprint: plan.configFingerprint,
      users: plan.users.length,
      accounts: plan.accounts.length,
      required_social_providers: plan.requiredSocialProviders,
    };
  }
  if (!databaseUrl) {
    throw new FirebaseIdentityMigrationError("DATABASE_URL is required for apply or verify");
  }
  assertRequiredSocialProviders(new Set(plan.requiredSocialProviders), env);
  const pool = new Pool({ connectionString: databaseUrl, max: 1 });
  const client = await pool.connect();
  try {
    if (command === "apply") return await applyDatabase(client, plan, sourceSha256);
    await assertSchema(client);
    return await verifyDatabase(client, plan, sourceSha256);
  } finally {
    client.release();
    await pool.end();
  }
}

async function main() {
  try {
    const { command, usersPath, hashConfigPath } = parseArguments(
      process.argv.slice(2),
    );
    const { plan, sourceSha256 } = await loadPlan(usersPath, hashConfigPath);
    const result = await runIdentityImport(
      command,
      plan,
      sourceSha256,
      process.env.DATABASE_URL,
    );
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    const message =
      error instanceof FirebaseIdentityMigrationError ||
      error instanceof SocialProviderConfigurationError
        ? error.message
        : "identity migration failed";
    process.stderr.write(`${JSON.stringify({ error: message })}\n`);
    process.exitCode = 2;
  }
}

if (fileURLToPath(import.meta.url) === path.resolve(process.argv[1] || "")) {
  await main();
}
