#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

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
  "lastSignedInAt",
  "lastLoginAt",
  "providerUserInfo",
  "disabled",
  "customAttributes",
  "phoneNumber",
  "lastRefreshAt",
]);
const FIREBASE_PROVIDER_FIELDS = new Set([
  "providerId",
  "rawId",
  "email",
  "displayName",
  "photoUrl",
]);
const PROVIDER_MAP = new Map([
  ["google.com", "google"],
  ["apple.com", "apple"],
]);
const PROVIDER_ISSUER = new Map([
  ["google", "https://accounts.google.com"],
  ["apple", "https://appleid.apple.com"],
]);
const SHA256 = /^[0-9a-f]{64}$/;
const DATABASE_ID = /^[0-9a-f-]{36}$/i;
const IMPORT_LEDGER_ID = "firebase";
const IMPORT_BATCH_SIZE = 50;
const MAX_MEM_COST = 18;
const MAX_ROUNDS = 8;
const MAX_ESTIMATED_SCRYPT_MEMORY_BYTES = 32 * 1024 * 1024;

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
  } catch {
    throw new FirebaseIdentityMigrationError(
      `${label} is not valid UTF-8 JSON`,
    );
  }
}

function requiredString(value, label) {
  if (typeof value !== "string" || !value.trim()) {
    throw new FirebaseIdentityMigrationError(
      `${label} must be a non-empty string`,
    );
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

function optionalBoolean(value, label, fallback = false) {
  if (value === undefined || value === null) return fallback;
  if (typeof value !== "boolean") {
    throw new FirebaseIdentityMigrationError(`${label} must be a boolean`);
  }
  return value;
}

function decodeBase64(value, label) {
  const raw = requiredString(value, label);
  const normalized = raw.replace(/-/g, "+").replace(/_/g, "/");
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(normalized)) {
    throw new FirebaseIdentityMigrationError(
      `${label} must use a valid base64 alphabet`,
    );
  }
  const decoded = Buffer.from(normalized, "base64");
  if (
    !decoded.length ||
    decoded.toString("base64").replace(/=+$/, "") !==
      normalized.replace(/=+$/, "")
  ) {
    throw new FirebaseIdentityMigrationError(
      `${label} must be canonical base64`,
    );
  }
  return decoded;
}

function positiveInteger(value, label, maximum) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new FirebaseIdentityMigrationError(
      `${label} must be an integer between 1 and ${maximum}`,
    );
  }
  return parsed;
}

export function parseFirebaseImportScryptConfig(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new FirebaseIdentityMigrationError(
      "Firebase scrypt configuration must be an object",
    );
  }
  const algorithm = String(raw.algorithm || "SCRYPT")
    .trim()
    .toUpperCase();
  if (algorithm !== "SCRYPT") {
    throw new FirebaseIdentityMigrationError(
      `unsupported Firebase password algorithm ${algorithm || "<empty>"}`,
    );
  }
  const signerKey = decodeBase64(
    raw.base64_signer_key ?? raw.signerKey,
    "base64_signer_key",
  );
  const saltSeparator = decodeBase64(
    raw.base64_salt_separator ?? raw.saltSeparator,
    "base64_salt_separator",
  );
  const rounds = positiveInteger(raw.rounds, "rounds", MAX_ROUNDS);
  const memCost = positiveInteger(
    raw.mem_cost ?? raw.memCost,
    "mem_cost",
    MAX_MEM_COST,
  );
  const estimatedMemory = 128 * 2 ** memCost * rounds;
  if (estimatedMemory > MAX_ESTIMATED_SCRYPT_MEMORY_BYTES) {
    throw new FirebaseIdentityMigrationError(
      "Firebase scrypt parameters exceed the Workers password-verification memory budget",
    );
  }
  const fingerprint = createHash("sha256")
    .update("omi-firebase-scrypt-v1\0")
    .update(signerKey)
    .update("\0")
    .update(saltSeparator)
    .update(`\0${rounds}\0${memCost}`)
    .digest("hex")
    .slice(0, 24);
  return Object.freeze({
    signerKey,
    saltSeparator,
    rounds,
    memCost,
    fingerprint,
  });
}

function encodeBase64Url(value) {
  return value
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function encodeFirebasePasswordHash(credentials, config) {
  const hash = decodeBase64(credentials.passwordHash, "passwordHash");
  const salt = decodeBase64(credentials.passwordSalt, "passwordSalt");
  if (hash.length !== config.signerKey.length) {
    throw new FirebaseIdentityMigrationError(
      "Firebase password hash length must match the signer key length",
    );
  }
  return [
    "firebase-scrypt-v1",
    config.fingerprint,
    encodeBase64Url(salt),
    encodeBase64Url(hash),
    "",
  ].join("$");
}

function firebaseTimestamp(value, label, fallback = null) {
  if (value === undefined || value === null || value === "") {
    if (fallback) return fallback;
    throw new FirebaseIdentityMigrationError(`${label} is required`);
  }
  const milliseconds = Number(value);
  if (!Number.isSafeInteger(milliseconds) || milliseconds < 0) {
    throw new FirebaseIdentityMigrationError(
      `${label} must be epoch milliseconds`,
    );
  }
  const parsed = new Date(milliseconds);
  if (!Number.isFinite(parsed.getTime())) {
    throw new FirebaseIdentityMigrationError(
      `${label} is outside the supported date range`,
    );
  }
  return parsed.toISOString();
}

function firebaseLastSignInTimestamp(user, userId, createdAt) {
  const official = user.lastSignedInAt;
  const apiVariant = user.lastLoginAt;
  const officialPresent =
    official !== undefined && official !== null && official !== "";
  const apiVariantPresent =
    apiVariant !== undefined && apiVariant !== null && apiVariant !== "";
  if (
    officialPresent &&
    apiVariantPresent &&
    Number(official) !== Number(apiVariant)
  ) {
    throw new FirebaseIdentityMigrationError(
      `user ${userId}: lastSignedInAt and lastLoginAt disagree`,
    );
  }
  return firebaseTimestamp(
    officialPresent ? official : apiVariant,
    `user ${userId}: lastSignedInAt`,
    createdAt,
  );
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
    const unknown = Object.keys(provider).filter(
      (key) => !FIREBASE_PROVIDER_FIELDS.has(key),
    );
    if (unknown.length) {
      throw new FirebaseIdentityMigrationError(
        `user ${userId}: providerUserInfo[${index}] contains unsupported fields: ${unknown.sort().join(", ")}`,
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
    for (const field of ["email", "displayName", "photoUrl"]) {
      optionalString(
        provider[field],
        `user ${userId}: providerUserInfo[${index}].${field}`,
      );
    }
    accounts.push({
      id: deterministicAccountRowId(providerId, accountId),
      issuer: PROVIDER_ISSUER.get(providerId),
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
    throw new FirebaseIdentityMigrationError(
      "Firebase export must be an object",
    );
  }
  if (!Array.isArray(source.users)) {
    throw new FirebaseIdentityMigrationError(
      "Firebase export must contain a users array",
    );
  }
  const users = [];
  const accounts = [];
  const seenIds = new Set();
  const seenEmails = new Set();
  const seenProviderAccounts = new Set();
  for (const [index, rawUser] of source.users.entries()) {
    if (!rawUser || typeof rawUser !== "object" || Array.isArray(rawUser)) {
      throw new FirebaseIdentityMigrationError(
        `users[${index}] must be an object`,
      );
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
      throw new FirebaseIdentityMigrationError(
        `duplicate Firebase uid ${userId}`,
      );
    }
    seenIds.add(userId);
    if (
      optionalBoolean(rawUser.disabled, `user ${userId}: disabled`) === true
    ) {
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
    const email = requiredString(
      rawUser.email,
      `user ${userId}: email`,
    ).toLowerCase();
    if (seenEmails.has(email)) {
      throw new FirebaseIdentityMigrationError(
        `duplicate Firebase email ${email}`,
      );
    }
    seenEmails.add(email);
    const createdAt = firebaseTimestamp(
      rawUser.createdAt,
      `user ${userId}: createdAt`,
    );
    const updatedAt = firebaseLastSignInTimestamp(rawUser, userId, createdAt);
    const timestamps = { createdAt, updatedAt };
    users.push({
      id: userId,
      name:
        optionalString(rawUser.displayName, `user ${userId}: displayName`) ??
        email.split("@")[0] ??
        "Imported User",
      email,
      emailVerified: optionalBoolean(
        rawUser.emailVerified,
        `user ${userId}: emailVerified`,
      ),
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
        issuer: "local:credential",
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

function assertRequiredSocialProviders(required, env) {
  const configured = new Set();
  for (const provider of ["google", "apple"]) {
    const prefix = provider.toUpperCase();
    const clientId = env[`${prefix}_CLIENT_ID`]?.trim();
    const clientSecret = env[`${prefix}_CLIENT_SECRET`]?.trim();
    if (Boolean(clientId) !== Boolean(clientSecret)) {
      throw new FirebaseIdentityMigrationError(
        `${prefix}_CLIENT_ID and ${prefix}_CLIENT_SECRET must be set together`,
      );
    }
    if (clientId && clientSecret) configured.add(provider);
  }
  const missing = [...required].filter((provider) => !configured.has(provider));
  if (missing.length) {
    throw new FirebaseIdentityMigrationError(
      `identity import requires configured social providers: ${missing.sort().join(", ")}`,
    );
  }
}

function canonicalTimestamp(value, label) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) {
    throw new FirebaseIdentityMigrationError(
      `D1 ${label} contains an invalid timestamp`,
    );
  }
  return date.toISOString();
}

function canonicalizeDatabaseRows(userRows, accountRows) {
  return {
    users: userRows.map((row) => ({
      id: row.id,
      name: row.name,
      email: row.email,
      emailVerified:
        row.emailVerified === true || Number(row.emailVerified) === 1,
      image: row.image ?? null,
      createdAt: canonicalTimestamp(row.createdAt, "user.createdAt"),
      updatedAt: canonicalTimestamp(row.updatedAt, "user.updatedAt"),
    })),
    accounts: accountRows.map((row) => ({
      id: row.id,
      issuer: row.issuer,
      accountId: row.accountId,
      providerId: row.providerId,
      userId: row.userId,
      password: row.password ?? null,
      createdAt: canonicalTimestamp(row.createdAt, "account.createdAt"),
      updatedAt: canonicalTimestamp(row.updatedAt, "account.updatedAt"),
    })),
  };
}

async function assertSchema(client) {
  const rows = await client.query(
    `SELECT name FROM sqlite_schema
     WHERE type = 'table'
       AND name IN ('user', 'account', 'session', 'auth_identity_imports')`,
  );
  const present = new Set(rows.map((row) => row.name));
  const missing = [
    "user",
    "account",
    "session",
    "auth_identity_imports",
  ].filter((table) => !present.has(table));
  if (missing.length) {
    throw new FirebaseIdentityMigrationError(
      `Better Auth D1 schema is missing: ${missing.join(", ")}`,
    );
  }
}

async function readLedger(client) {
  const rows = await client.query(
    `SELECT id, sourceSha256, configFingerprint, canonicalSha256,
            userCount, accountCount, status, startedAt, completedAt
     FROM auth_identity_imports`,
  );
  if (rows.length > 1) {
    throw new FirebaseIdentityMigrationError(
      "identity import ledger must contain at most one source",
    );
  }
  return rows[0] || null;
}

function assertLedgerMatches(ledger, plan, sourceSha256) {
  if (
    ledger.id !== IMPORT_LEDGER_ID ||
    ledger.sourceSha256 !== sourceSha256 ||
    ledger.configFingerprint !== plan.configFingerprint ||
    ledger.canonicalSha256 !== plan.canonicalSha256 ||
    Number(ledger.userCount) !== plan.users.length ||
    Number(ledger.accountCount) !== plan.accounts.length
  ) {
    throw new FirebaseIdentityMigrationError(
      "identity import ledger does not match the requested source",
    );
  }
}

async function readIdentityRows(client) {
  const [users, accounts, sessions] = await Promise.all([
    client.query(
      `SELECT id, name, email, emailVerified, image, createdAt, updatedAt
       FROM user ORDER BY id COLLATE BINARY`,
    ),
    client.query(
      `SELECT id, issuer, accountId, providerId, userId, password, createdAt, updatedAt
       FROM account ORDER BY id COLLATE BINARY`,
    ),
    client.query("SELECT COUNT(*) AS count FROM session"),
  ]);
  return {
    canonical: canonicalizeDatabaseRows(users, accounts),
    sessions: Number(sessions[0]?.count ?? -1),
  };
}

function assertPartialRowsMatch(actualRows, plannedRows, label) {
  const planned = new Map(plannedRows.map((row) => [row.id, row]));
  for (const actual of actualRows) {
    const expected = planned.get(actual.id);
    if (!expected || stableJson(actual) !== stableJson(expected)) {
      throw new FirebaseIdentityMigrationError(
        `Better Auth D1 contains a conflicting ${label} row`,
      );
    }
  }
}

async function verifyIdentityRows(client, plan) {
  const { canonical, sessions } = await readIdentityRows(client);
  if (sessions !== 0) {
    throw new FirebaseIdentityMigrationError(
      "identity migration verification requires zero pre-cutover sessions",
    );
  }
  const actualSha256 = sha256(stableJson(canonical));
  if (actualSha256 !== plan.canonicalSha256) {
    throw new FirebaseIdentityMigrationError(
      "Better Auth D1 user/account reconciliation hash does not match the Firebase export",
    );
  }
  return actualSha256;
}

async function verifyDatabase(client, plan, sourceSha256) {
  const ledger = await readLedger(client);
  if (!ledger || ledger.status !== "completed") {
    throw new FirebaseIdentityMigrationError(
      "identity import ledger is not completed",
    );
  }
  assertLedgerMatches(ledger, plan, sourceSha256);
  const actualSha256 = await verifyIdentityRows(client, plan);
  return {
    status: "verified",
    source_sha256: sourceSha256,
    canonical_sha256: actualSha256,
    config_fingerprint: plan.configFingerprint,
    users: plan.users.length,
    accounts: plan.accounts.length,
    sessions: 0,
  };
}

function chunks(values, size) {
  const result = [];
  for (let index = 0; index < values.length; index += size) {
    result.push(values.slice(index, index + size));
  }
  return result;
}

function userInsert(user) {
  if (user.image === null) {
    return {
      sql: `INSERT OR IGNORE INTO user
              (id, name, email, emailVerified, image, createdAt, updatedAt)
            VALUES (?, ?, ?, ?, NULL, ?, ?)`,
      params: [
        user.id,
        user.name,
        user.email,
        user.emailVerified ? "1" : "0",
        user.createdAt,
        user.updatedAt,
      ],
    };
  }
  return {
    sql: `INSERT OR IGNORE INTO user
            (id, name, email, emailVerified, image, createdAt, updatedAt)
          VALUES (?, ?, ?, ?, ?, ?, ?)`,
    params: [
      user.id,
      user.name,
      user.email,
      user.emailVerified ? "1" : "0",
      user.image,
      user.createdAt,
      user.updatedAt,
    ],
  };
}

function accountInsert(account) {
  if (account.password === null) {
    return {
      sql: `INSERT OR IGNORE INTO account
              (id, issuer, accountId, providerId, userId, password, createdAt, updatedAt)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)`,
      params: [
        account.id,
        account.issuer,
        account.accountId,
        account.providerId,
        account.userId,
        account.createdAt,
        account.updatedAt,
      ],
    };
  }
  return {
    sql: `INSERT OR IGNORE INTO account
            (id, issuer, accountId, providerId, userId, password, createdAt, updatedAt)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    params: [
      account.id,
      account.issuer,
      account.accountId,
      account.providerId,
      account.userId,
      account.password,
      account.createdAt,
      account.updatedAt,
    ],
  };
}

async function insertBatches(client, rows, toStatement) {
  for (const batch of chunks(rows, IMPORT_BATCH_SIZE)) {
    await client.batch(batch.map(toStatement));
  }
}

async function applyDatabase(client, plan, sourceSha256) {
  await assertSchema(client);
  const startedAt = String(Date.now());
  await client.query(
    `INSERT OR IGNORE INTO auth_identity_imports
       (id, sourceSha256, configFingerprint, canonicalSha256,
        userCount, accountCount, status, startedAt, completedAt)
     VALUES (?, ?, ?, ?, ?, ?, 'applying', ?, NULL)`,
    [
      IMPORT_LEDGER_ID,
      sourceSha256,
      plan.configFingerprint,
      plan.canonicalSha256,
      String(plan.users.length),
      String(plan.accounts.length),
      startedAt,
    ],
  );
  const ledger = await readLedger(client);
  if (!ledger) {
    throw new FirebaseIdentityMigrationError(
      "identity import could not claim the D1 ledger",
    );
  }
  assertLedgerMatches(ledger, plan, sourceSha256);
  if (ledger.status === "completed") {
    const result = await verifyDatabase(client, plan, sourceSha256);
    return { ...result, status: "already_applied" };
  }
  if (ledger.status !== "applying") {
    throw new FirebaseIdentityMigrationError(
      "identity import ledger has an unsupported state",
    );
  }

  const existing = await readIdentityRows(client);
  if (existing.sessions !== 0) {
    throw new FirebaseIdentityMigrationError(
      "identity import target must contain no Better Auth sessions",
    );
  }
  assertPartialRowsMatch(existing.canonical.users, plan.users, "user");
  assertPartialRowsMatch(existing.canonical.accounts, plan.accounts, "account");

  await insertBatches(client, plan.users, userInsert);
  await insertBatches(client, plan.accounts, accountInsert);
  await verifyIdentityRows(client, plan);

  await client.query(
    `UPDATE auth_identity_imports
     SET status = 'completed', completedAt = ?
     WHERE id = ? AND sourceSha256 = ? AND canonicalSha256 = ?
       AND configFingerprint = ? AND status = 'applying'`,
    [
      String(Date.now()),
      IMPORT_LEDGER_ID,
      sourceSha256,
      plan.canonicalSha256,
      plan.configFingerprint,
    ],
  );
  return verifyDatabase(client, plan, sourceSha256);
}

export async function runIdentityImport(
  command,
  plan,
  sourceSha256,
  client = null,
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
  if (!client) {
    throw new FirebaseIdentityMigrationError(
      "D1 client is required for apply or verify",
    );
  }
  assertRequiredSocialProviders(new Set(plan.requiredSocialProviders), env);
  await assertSchema(client);
  if (command === "apply") {
    const result = await applyDatabase(client, plan, sourceSha256);
    return result.status === "verified"
      ? { ...result, status: "applied" }
      : result;
  }
  if (command === "verify") {
    return verifyDatabase(client, plan, sourceSha256);
  }
  throw new FirebaseIdentityMigrationError(`unsupported command ${command}`);
}

export function createCloudflareD1Client({
  accountId,
  databaseId,
  apiToken,
  fetchImpl = fetch,
}) {
  if (!accountId || !apiToken || !DATABASE_ID.test(databaseId || "")) {
    throw new FirebaseIdentityMigrationError(
      "CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, and a valid CLOUDFLARE_D1_DATABASE_ID are required",
    );
  }
  const endpoint = `https://api.cloudflare.com/client/v4/accounts/${encodeURIComponent(accountId)}/d1/database/${encodeURIComponent(databaseId)}/query`;

  const execute = async (body) => {
    let response;
    try {
      response = await fetchImpl(endpoint, {
        method: "POST",
        headers: {
          authorization: `Bearer ${apiToken}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(body),
      });
    } catch {
      throw new FirebaseIdentityMigrationError("Cloudflare D1 API unavailable");
    }
    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new FirebaseIdentityMigrationError(
        `Cloudflare D1 API returned invalid JSON (${response.status})`,
      );
    }
    const results = Array.isArray(payload?.result) ? payload.result : [];
    if (
      !response.ok ||
      payload?.success !== true ||
      !results.length ||
      results.some((result) => result?.success !== true)
    ) {
      throw new FirebaseIdentityMigrationError(
        `Cloudflare D1 API query failed (${response.status})`,
      );
    }
    return results;
  };

  return Object.freeze({
    async query(sql, params = []) {
      const results = await execute({ sql, params });
      return Array.isArray(results[0]?.results) ? results[0].results : [];
    },
    async batch(batch) {
      if (!Array.isArray(batch) || !batch.length) return [];
      return execute({ batch });
    },
  });
}

function parseArguments(argv) {
  const [command, ...rest] = argv;
  if (!new Set(["validate", "apply", "verify"]).has(command)) {
    throw new FirebaseIdentityMigrationError(
      "usage: import-firebase-identities.mjs validate|apply|verify --users FILE --hash-config FILE",
    );
  }
  const options = {};
  for (let index = 0; index < rest.length; index += 2) {
    const flag = rest[index];
    const value = rest[index + 1];
    if (!new Set(["--users", "--hash-config"]).has(flag) || !value) {
      throw new FirebaseIdentityMigrationError(
        `invalid argument ${flag || "<missing>"}`,
      );
    }
    options[flag.slice(2)] = value;
  }
  if (!options.users || !options["hash-config"]) {
    throw new FirebaseIdentityMigrationError(
      "--users and --hash-config are required",
    );
  }
  return {
    command,
    usersPath: options.users,
    hashConfigPath: options["hash-config"],
  };
}

async function readPrivateInput(filePath, label) {
  let pathMetadata;
  try {
    pathMetadata = await lstat(filePath);
  } catch {
    throw new FirebaseIdentityMigrationError(
      `${label} is missing or unreadable`,
    );
  }
  if (!pathMetadata.isFile() || pathMetadata.isSymbolicLink()) {
    throw new FirebaseIdentityMigrationError(
      `${label} must be a regular file, not a symlink`,
    );
  }
  let handle;
  try {
    handle = await open(filePath, constants.O_RDONLY | constants.O_NOFOLLOW);
  } catch {
    throw new FirebaseIdentityMigrationError(
      `${label} is missing or unreadable`,
    );
  }
  try {
    const metadata = await handle.stat();
    if (
      !metadata.isFile() ||
      metadata.dev !== pathMetadata.dev ||
      metadata.ino !== pathMetadata.ino
    ) {
      throw new FirebaseIdentityMigrationError(
        `${label} changed while it was being opened`,
      );
    }
    if ((metadata.mode & 0o77) !== 0) {
      throw new FirebaseIdentityMigrationError(
        `${label} must be mode 0600 or stricter`,
      );
    }
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}

async function loadPlan(usersPath, hashConfigPath) {
  const [usersRaw, configRaw] = await Promise.all([
    readPrivateInput(usersPath, "Firebase user export"),
    readPrivateInput(hashConfigPath, "Firebase hash configuration"),
  ]);
  const source = parseJson(usersRaw, "Firebase user export");
  const configDocument = parseJson(configRaw, "Firebase hash configuration");
  const config = parseFirebaseImportScryptConfig(
    configDocument.hash_config ?? configDocument,
  );
  return {
    plan: planFirebaseIdentityImport(source, config),
    sourceSha256: sha256(usersRaw),
  };
}

async function main() {
  try {
    const { command, usersPath, hashConfigPath } = parseArguments(
      process.argv.slice(2),
    );
    const { plan, sourceSha256 } = await loadPlan(usersPath, hashConfigPath);
    let client = null;
    if (command !== "validate") {
      const databaseId = process.env.CLOUDFLARE_D1_DATABASE_ID || "";
      if (
        command === "apply" &&
        process.env.CLOUDFLARE_IDENTITY_IMPORT_CONFIRM !== databaseId
      ) {
        throw new FirebaseIdentityMigrationError(
          "apply requires CLOUDFLARE_IDENTITY_IMPORT_CONFIRM to equal CLOUDFLARE_D1_DATABASE_ID",
        );
      }
      client = createCloudflareD1Client({
        accountId: process.env.CLOUDFLARE_ACCOUNT_ID,
        databaseId,
        apiToken: process.env.CLOUDFLARE_API_TOKEN,
      });
    }
    const result = await runIdentityImport(
      command,
      plan,
      sourceSha256,
      client,
      process.env,
    );
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    const message =
      error instanceof FirebaseIdentityMigrationError
        ? error.message
        : "identity migration failed";
    process.stderr.write(`${JSON.stringify({ error: message })}\n`);
    process.exitCode = 2;
  }
}

if (fileURLToPath(import.meta.url) === path.resolve(process.argv[1] || "")) {
  await main();
}
