import { Buffer } from "node:buffer";
import {
  createCipheriv,
  createHash,
  scrypt as scryptCallback,
  timingSafeEqual,
} from "node:crypto";
import {
  hashPassword as hashBetterAuthPassword,
  verifyPassword as verifyBetterAuthPassword,
} from "better-auth/crypto";

const FIREBASE_PASSWORD_PREFIX = "firebase-scrypt-v1";
const FIREBASE_PASSWORD_PARTS = 5;
const AES_ALGORITHM = "aes-256-ctr";
const AES_KEY_BYTES = 32;
const AES_IV_BYTES = 16;
const MAX_MEM_COST = 18;
const MAX_ROUNDS = 8;
const MAX_ESTIMATED_SCRYPT_MEMORY_BYTES = 32 * 1024 * 1024;

export type FirebaseScryptEnvironment = {
  AUTH_FIREBASE_SCRYPT_SIGNER_KEY?: string;
  AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR?: string;
  AUTH_FIREBASE_SCRYPT_ROUNDS?: string;
  AUTH_FIREBASE_SCRYPT_MEM_COST?: string;
};

export type FirebaseScryptConfig = {
  algorithm: "SCRYPT";
  signerKey: Uint8Array;
  saltSeparator: Uint8Array;
  rounds: number;
  memCost: number;
  fingerprint: string;
};

export type FirebasePasswordUpgradeResult =
  "not_migrated" | "upgraded" | "already_upgraded";

export class FirebasePasswordMigrationConfigurationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FirebasePasswordMigrationConfigurationError";
  }
}

function decodeBase64(value: unknown, setting: string): Uint8Array {
  if (typeof value !== "string" || !value.trim()) {
    throw new FirebasePasswordMigrationConfigurationError(
      `${setting} must be non-empty base64`,
    );
  }
  const normalized = value.trim().replace(/-/g, "+").replace(/_/g, "/");
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(normalized)) {
    throw new FirebasePasswordMigrationConfigurationError(
      `${setting} must use a valid base64 alphabet`,
    );
  }
  const decoded = Buffer.from(normalized, "base64");
  if (
    !decoded.length ||
    decoded.toString("base64").replace(/=+$/, "") !==
      normalized.replace(/=+$/, "")
  ) {
    throw new FirebasePasswordMigrationConfigurationError(
      `${setting} must be canonical base64`,
    );
  }
  return decoded;
}

function positiveInteger(
  value: unknown,
  setting: string,
  maximum: number,
): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new FirebasePasswordMigrationConfigurationError(
      `${setting} must be an integer between 1 and ${maximum}`,
    );
  }
  return parsed;
}

function encodeBase64Url(value: Uint8Array): string {
  return Buffer.from(value)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

export function parseFirebaseScryptConfig(raw: unknown): FirebaseScryptConfig {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new FirebasePasswordMigrationConfigurationError(
      "Firebase scrypt configuration must be an object",
    );
  }
  const values = raw as Record<string, unknown>;
  const algorithm = String(values.algorithm || "SCRYPT")
    .trim()
    .toUpperCase();
  if (algorithm !== "SCRYPT") {
    throw new FirebasePasswordMigrationConfigurationError(
      `unsupported Firebase password algorithm ${algorithm || "<empty>"}`,
    );
  }
  const signerKey = decodeBase64(
    values.base64_signer_key ?? values.signerKey,
    "base64_signer_key",
  );
  const saltSeparator = decodeBase64(
    values.base64_salt_separator ?? values.saltSeparator,
    "base64_salt_separator",
  );
  const rounds = positiveInteger(values.rounds, "rounds", MAX_ROUNDS);
  const memCost = positiveInteger(
    values.mem_cost ?? values.memCost,
    "mem_cost",
    MAX_MEM_COST,
  );
  const estimatedMemory = 128 * 2 ** memCost * rounds;
  if (estimatedMemory > MAX_ESTIMATED_SCRYPT_MEMORY_BYTES) {
    throw new FirebasePasswordMigrationConfigurationError(
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
    algorithm: "SCRYPT" as const,
    signerKey,
    saltSeparator,
    rounds,
    memCost,
    fingerprint,
  });
}

export function firebaseScryptConfigFromEnv(
  env: FirebaseScryptEnvironment,
): FirebaseScryptConfig | null {
  const signerKey = env.AUTH_FIREBASE_SCRYPT_SIGNER_KEY?.trim();
  const saltSeparator = env.AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR?.trim();
  const rounds = env.AUTH_FIREBASE_SCRYPT_ROUNDS?.trim();
  const memCost = env.AUTH_FIREBASE_SCRYPT_MEM_COST?.trim();
  const present = [signerKey, saltSeparator, rounds, memCost].filter(Boolean);
  if (!present.length) return null;
  if (present.length !== 4) {
    throw new FirebasePasswordMigrationConfigurationError(
      "AUTH_FIREBASE_SCRYPT_SIGNER_KEY, AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR, AUTH_FIREBASE_SCRYPT_ROUNDS, and AUTH_FIREBASE_SCRYPT_MEM_COST must be set together",
    );
  }
  return parseFirebaseScryptConfig({
    algorithm: "SCRYPT",
    base64_signer_key: signerKey,
    base64_salt_separator: saltSeparator,
    rounds,
    mem_cost: memCost,
  });
}

export function encodeFirebasePasswordHash(
  credentials: { passwordHash: string; passwordSalt: string },
  config: FirebaseScryptConfig,
): string {
  const hash = decodeBase64(credentials.passwordHash, "passwordHash");
  const salt = decodeBase64(credentials.passwordSalt, "passwordSalt");
  if (hash.length !== config.signerKey.length) {
    throw new FirebasePasswordMigrationConfigurationError(
      "Firebase password hash length must match the signer key length",
    );
  }
  return [
    FIREBASE_PASSWORD_PREFIX,
    config.fingerprint,
    encodeBase64Url(salt),
    encodeBase64Url(hash),
    "",
  ].join("$");
}

export function isFirebasePasswordHash(hash: unknown): hash is string {
  return (
    typeof hash === "string" && hash.startsWith(`${FIREBASE_PASSWORD_PREFIX}$`)
  );
}

function parseFirebasePasswordHash(hash: string) {
  const parts = hash.split("$");
  if (
    parts.length !== FIREBASE_PASSWORD_PARTS ||
    parts[0] !== FIREBASE_PASSWORD_PREFIX ||
    parts[4] !== "" ||
    !/^[0-9a-f]{24}$/.test(parts[1])
  ) {
    throw new FirebasePasswordMigrationConfigurationError(
      "migrated Firebase password envelope is malformed",
    );
  }
  return {
    fingerprint: parts[1],
    salt: decodeBase64(parts[2], "migrated password salt"),
    expectedHash: decodeBase64(parts[3], "migrated password hash"),
  };
}

export async function hashFirebasePassword(
  password: string,
  salt: Uint8Array,
  config: FirebaseScryptConfig,
): Promise<Uint8Array | null> {
  if (!password.length) return null;
  const combinedSalt = Buffer.concat([salt, config.saltSeparator]);
  const derivedKey = await new Promise<Uint8Array>((resolve, reject) => {
    scryptCallback(
      password,
      combinedSalt,
      AES_KEY_BYTES,
      {
        N: 2 ** config.memCost,
        r: config.rounds,
        p: 1,
        maxmem: Math.max(
          128 * 2 ** config.memCost * config.rounds + 1024,
          32 * 1024 * 1024,
        ),
      },
      (error, key) => {
        if (error) reject(error);
        else resolve(key);
      },
    );
  });
  const cipher = createCipheriv(
    AES_ALGORITHM,
    derivedKey,
    Buffer.alloc(AES_IV_BYTES, 0),
  );
  return Buffer.concat([cipher.update(config.signerKey), cipher.final()]);
}

export async function verifyMigratedFirebasePassword(
  credentials: { hash: string; password: string },
  env: FirebaseScryptEnvironment,
): Promise<boolean> {
  const envelope = parseFirebasePasswordHash(credentials.hash);
  const config = firebaseScryptConfigFromEnv(env);
  if (!config) {
    throw new FirebasePasswordMigrationConfigurationError(
      "Firebase password migration verifier is not configured",
    );
  }
  if (config.fingerprint !== envelope.fingerprint) {
    throw new FirebasePasswordMigrationConfigurationError(
      "Firebase password migration verifier fingerprint does not match the imported credential",
    );
  }
  const candidate = await hashFirebasePassword(
    credentials.password,
    envelope.salt,
    config,
  );
  if (!candidate || candidate.length !== envelope.expectedHash.length) {
    if (envelope.expectedHash.length) {
      timingSafeEqual(envelope.expectedHash, envelope.expectedHash);
    }
    return false;
  }
  return timingSafeEqual(candidate, envelope.expectedHash);
}

export async function hashPassword(password: string): Promise<string> {
  return hashBetterAuthPassword(password);
}

export async function verifyPassword(
  credentials: { hash: string; password: string },
  env: FirebaseScryptEnvironment,
): Promise<boolean> {
  if (isFirebasePasswordHash(credentials.hash)) {
    return verifyMigratedFirebasePassword(credentials, env);
  }
  return verifyBetterAuthPassword(credentials);
}

type CredentialPasswordRow = {
  id: unknown;
  password: unknown;
};

function credentialPasswordRow(value: CredentialPasswordRow): {
  id: string;
  password: string;
} {
  if (
    typeof value.id !== "string" ||
    !value.id ||
    typeof value.password !== "string" ||
    !value.password
  ) {
    throw new Error("credential password row is malformed");
  }
  return { id: value.id, password: value.password };
}

async function findOnlyCredentialPassword(
  database: D1Database,
  userId: string,
): Promise<{ id: string; password: string } | null> {
  const result = await database
    .prepare(
      `SELECT id, password FROM account
       WHERE userId = ? AND providerId = 'credential'
       ORDER BY createdAt, id`,
    )
    .bind(userId)
    .all<CredentialPasswordRow>();
  if (!result.success) throw new Error("credential password query failed");
  if (result.results.length === 0) return null;
  if (result.results.length !== 1) {
    throw new Error("user has multiple credential accounts");
  }
  return credentialPasswordRow(result.results[0]);
}

/**
 * Replace a successfully verified Firebase password envelope with Better
 * Auth's native password hash. The conditional update makes concurrent first
 * logins idempotent without ever overwriting a newer password.
 */
export async function upgradeMigratedFirebasePassword(
  database: D1Database,
  userId: string,
  password: string,
  nativeHash: (password: string) => Promise<string> = hashPassword,
): Promise<FirebasePasswordUpgradeResult> {
  const credential = await findOnlyCredentialPassword(database, userId);
  if (!credential || !isFirebasePasswordHash(credential.password)) {
    return "not_migrated";
  }

  const replacement = await nativeHash(password);
  if (!replacement || isFirebasePasswordHash(replacement)) {
    throw new Error("native password hasher returned an invalid hash");
  }

  // Better Auth's D1/Kysely adapter stores date fields as ISO strings. Keep
  // direct migration writes on the same representation even though SQLite's
  // column affinity accepts both text and integers.
  const updatedAt = new Date().toISOString();
  const result = await database
    .prepare(
      `UPDATE account SET password = ?, updatedAt = ?
       WHERE id = ? AND password = ?`,
    )
    .bind(replacement, updatedAt, credential.id, credential.password)
    .run();
  if (!result.success) throw new Error("credential password update failed");
  const changes = Number(result.meta.changes);
  if (!Number.isSafeInteger(changes) || changes < 0 || changes > 1) {
    throw new Error("credential password update returned invalid changes");
  }
  if (changes === 1) return "upgraded";

  const current = await database
    .prepare("SELECT id, password FROM account WHERE id = ?")
    .bind(credential.id)
    .first<CredentialPasswordRow>();
  if (current) {
    const currentCredential = credentialPasswordRow(current);
    if (!isFirebasePasswordHash(currentCredential.password)) {
      return "already_upgraded";
    }
  }
  throw new Error("credential password upgrade lost a concurrent update");
}
