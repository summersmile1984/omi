import {
  createCipheriv,
  createHash,
  scrypt as scryptCallback,
  timingSafeEqual,
} from "node:crypto";
import { promisify } from "node:util";
import {
  hashPassword as hashBetterAuthPassword,
  verifyPassword as verifyBetterAuthPassword,
} from "better-auth/crypto";

const scrypt = promisify(scryptCallback);
const FIREBASE_PASSWORD_PREFIX = "firebase-scrypt-v1";
const FIREBASE_PASSWORD_PARTS = 5;
const AES_ALGORITHM = "aes-256-ctr";
const AES_KEY_BYTES = 32;
const AES_IV_BYTES = 16;
const MAX_MEM_COST = 18;
const MAX_ROUNDS = 32;

export class FirebasePasswordMigrationConfigurationError extends Error {
  constructor(message) {
    super(message);
    this.name = "FirebasePasswordMigrationConfigurationError";
  }
}

function decodeBase64(value, setting) {
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

function positiveInteger(value, setting, maximum) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new FirebasePasswordMigrationConfigurationError(
      `${setting} must be an integer between 1 and ${maximum}`,
    );
  }
  return parsed;
}

export function parseFirebaseScryptConfig(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new FirebasePasswordMigrationConfigurationError(
      "Firebase scrypt configuration must be an object",
    );
  }
  const algorithm = String(raw.algorithm || "SCRYPT").trim().toUpperCase();
  if (algorithm !== "SCRYPT") {
    throw new FirebasePasswordMigrationConfigurationError(
      `unsupported Firebase password algorithm ${algorithm || "<empty>"}`,
    );
  }
  const signerKeyBase64 = String(
    raw.base64_signer_key ?? raw.signerKey ?? "",
  ).trim();
  const saltSeparatorBase64 = String(
    raw.base64_salt_separator ?? raw.saltSeparator ?? "",
  ).trim();
  const signerKey = decodeBase64(signerKeyBase64, "base64_signer_key");
  const saltSeparator = decodeBase64(
    saltSeparatorBase64,
    "base64_salt_separator",
  );
  const rounds = positiveInteger(raw.rounds, "rounds", MAX_ROUNDS);
  const memCost = positiveInteger(
    raw.mem_cost ?? raw.memCost,
    "mem_cost",
    MAX_MEM_COST,
  );
  const fingerprint = createHash("sha256")
    .update("omi-firebase-scrypt-v1\0")
    .update(signerKey)
    .update("\0")
    .update(saltSeparator)
    .update(`\0${rounds}\0${memCost}`)
    .digest("hex")
    .slice(0, 24);
  return Object.freeze({
    algorithm,
    signerKey,
    saltSeparator,
    rounds,
    memCost,
    fingerprint,
  });
}

export function firebaseScryptConfigFromEnv(env = process.env) {
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
  { passwordHash, passwordSalt },
  config,
) {
  const hash = decodeBase64(passwordHash, "passwordHash");
  const salt = decodeBase64(passwordSalt, "passwordSalt");
  if (hash.length !== config.signerKey.length) {
    throw new FirebasePasswordMigrationConfigurationError(
      "Firebase password hash length must match the signer key length",
    );
  }
  return [
    FIREBASE_PASSWORD_PREFIX,
    config.fingerprint,
    salt.toString("base64url"),
    hash.toString("base64url"),
    "",
  ].join("$");
}

export function isFirebasePasswordHash(hash) {
  return (
    typeof hash === "string" &&
    hash.startsWith(`${FIREBASE_PASSWORD_PREFIX}$`)
  );
}

function parseFirebasePasswordHash(hash) {
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

export async function hashFirebasePassword(password, salt, config) {
  if (typeof password !== "string" || !password.length) return null;
  const combinedSalt = Buffer.concat([salt, config.saltSeparator]);
  const derivedKey = await scrypt(password, combinedSalt, AES_KEY_BYTES, {
    N: 2 ** config.memCost,
    r: config.rounds,
    p: 1,
    maxmem: Math.max(
      128 * 2 ** config.memCost * config.rounds + 1024,
      32 * 1024 * 1024,
    ),
  });
  const cipher = createCipheriv(
    AES_ALGORITHM,
    derivedKey,
    Buffer.alloc(AES_IV_BYTES, 0),
  );
  return Buffer.concat([cipher.update(config.signerKey), cipher.final()]);
}

export async function verifyMigratedFirebasePassword(
  { hash, password },
  env = process.env,
) {
  const envelope = parseFirebasePasswordHash(hash);
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
    password,
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

export async function hashPassword(password) {
  return hashBetterAuthPassword(password);
}

export async function verifyPassword({ hash, password }) {
  if (isFirebasePasswordHash(hash)) {
    return verifyMigratedFirebasePassword({ hash, password });
  }
  return verifyBetterAuthPassword({ hash, password });
}
