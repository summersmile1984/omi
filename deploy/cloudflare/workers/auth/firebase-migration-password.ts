import {
  hashPassword as hashBetterAuthPassword,
  verifyPassword as verifyBetterAuthPassword,
} from "better-auth/crypto";
import {
  isFirebasePasswordHash,
  workersFirebaseScrypt,
  type FirebaseScryptEnvironment,
} from "../../../../auth/shared/firebase-scrypt.mjs";

export type FirebasePasswordUpgradeResult =
  | "not_migrated"
  | "upgraded"
  | "already_upgraded";

export async function hashPassword(password: string): Promise<string> {
  return hashBetterAuthPassword(password);
}

export async function verifyPassword(
  credentials: { hash: string; password: string },
  env: FirebaseScryptEnvironment,
): Promise<boolean> {
  if (isFirebasePasswordHash(credentials.hash)) {
    return workersFirebaseScrypt.verify(credentials, env);
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
