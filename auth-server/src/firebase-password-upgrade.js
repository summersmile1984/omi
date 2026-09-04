import { createAuthMiddleware } from "better-auth/api";
import {
  isFirebasePasswordHash,
  serverFirebaseScrypt,
} from "../../auth/shared/firebase-scrypt.mjs";
import { recordFallback } from "../../runtime/shared/fallback.mjs";
import { hashPassword } from "./firebase-migration-password.js";

function credentialRow(row) {
  if (
    typeof row?.id !== "string" ||
    !row.id ||
    typeof row.password !== "string" ||
    !row.password
  ) {
    throw new Error("credential password row is malformed");
  }
  return row;
}

/** Upgrade only the current credential that this password still verifies. */
export async function upgradeMigratedFirebasePassword(
  pool,
  userId,
  password,
  env = process.env,
  nativeHash = hashPassword,
) {
  const result = await pool.query(
    `SELECT id, password FROM "account"
     WHERE "userId" = $1 AND "providerId" = 'credential'
     ORDER BY "createdAt", id`,
    [userId],
  );
  if (result.rows.length === 0) return "not_migrated";
  if (result.rows.length !== 1)
    throw new Error("user has multiple credential accounts");
  const credential = credentialRow(result.rows[0]);
  if (!isFirebasePasswordHash(credential.password)) return "not_migrated";

  // An importer or password reset may have replaced the sign-in snapshot.
  // Verify this exact envelope before comparing it in the atomic update.
  if (
    !(await serverFirebaseScrypt.verify(
      { hash: credential.password, password },
      env,
    ))
  ) {
    return "credential_changed";
  }
  const replacement = await nativeHash(password);
  if (!replacement || isFirebasePasswordHash(replacement)) {
    throw new Error("native password hasher returned an invalid hash");
  }
  const updated = await pool.query(
    `UPDATE "account" SET password = $1, "updatedAt" = $2
     WHERE id = $3 AND password = $4`,
    [replacement, new Date(), credential.id, credential.password],
  );
  if (updated.rowCount === 1) return "upgraded";
  if (updated.rowCount !== 0)
    throw new Error("credential update returned invalid row count");
  // A concurrent reset, deletion or first login owns the current value.
  return "credential_changed";
}

export function firebasePasswordUpgradeHook(pool, env = process.env) {
  return createAuthMiddleware(async (ctx) => {
    if (ctx.path !== "/sign-in/email") return;
    const userId = ctx.context.newSession?.user.id;
    const password = ctx.body?.password;
    if (!userId || typeof password !== "string") return;
    try {
      await upgradeMigratedFirebasePassword(
        pool,
        userId,
        password,
        env,
        ctx.context.password.hash,
      );
    } catch {
      // Successful authentication already owns the session. Keep its verified
      // legacy hash usable when the best-effort upgrade cannot be persisted.
      recordFallback({
        component: "other",
        from: "postgres",
        to: "none",
        reason: "dependency_unavailable",
        outcome: "degraded",
      });
    }
  });
}
