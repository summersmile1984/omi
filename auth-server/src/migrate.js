import { getMigrations } from "better-auth/db/migration";
import { auth, authOptions, pool } from "./auth.js";

async function pendingMigrations() {
  const migrations = await getMigrations(authOptions);
  return {
    migrations,
    pending: migrations.toBeCreated.length + migrations.toBeAdded.length,
  };
}

function jwkMetadata(publicKey) {
  let jwk;
  try {
    jwk = JSON.parse(publicKey);
  } catch (_err) {
    throw new Error("Better Auth JWKS row contains malformed publicKey JSON");
  }
  if (jwk?.kty === "EC" && jwk?.crv === "P-256")
    return { alg: "ES256", crv: "P-256", current: true };
  if (jwk?.kty === "OKP" && jwk?.crv === "Ed25519")
    return { alg: "EdDSA", crv: "Ed25519", current: false };
  if (jwk?.kty === "RSA" && typeof jwk?.n === "string")
    return { alg: "RS256", crv: null, current: false };
  throw new Error(
    `Better Auth JWKS row uses an unsupported key shape kty=${String(jwk?.kty)} crv=${String(jwk?.crv)}`,
  );
}

async function ensureJwksMetadataColumns() {
  await pool.query(`ALTER TABLE "jwks" ADD COLUMN IF NOT EXISTS alg text`);
  await pool.query(`ALTER TABLE "jwks" ADD COLUMN IF NOT EXISTS crv text`);
}

async function checkJwksMetadataColumns() {
  const result = await pool.query(
    `SELECT column_name FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'jwks'
       AND column_name IN ('alg', 'crv')`,
  );
  const columns = new Set(result.rows.map((row) => row.column_name));
  if (!columns.has('alg') || !columns.has('crv'))
    throw new Error('Better Auth JWKS metadata columns are missing');
}

async function reconcileJwksRows({ checkOnly }) {
  const result = await pool.query(
    `SELECT id, "publicKey", "createdAt", "expiresAt", alg, crv
     FROM "jwks" ORDER BY "createdAt" DESC`,
  );
  const now = new Date();
  let needsCurrentKey = true;
  for (const [index, row] of result.rows.entries()) {
    const expected = jwkMetadata(row.publicKey);
    const metadataMatches =
      row.alg === expected.alg && (row.crv || null) === expected.crv;
    const mustRetire = !expected.current && (!row.expiresAt || row.expiresAt > now);
    if (checkOnly && (!metadataMatches || mustRetire)) {
      throw new Error(
        `Better Auth JWKS metadata migration pending for key ${row.id}`,
      );
    }
    if (!checkOnly && (!metadataMatches || mustRetire)) {
      await pool.query(
        `UPDATE "jwks"
         SET alg = $2, crv = $3,
             "expiresAt" = CASE
               WHEN $4::boolean AND ("expiresAt" IS NULL OR "expiresAt" > now()) THEN now()
               ELSE "expiresAt"
             END,
             "createdAt" = CASE
               WHEN $4::boolean THEN LEAST("createdAt", now() - interval '1 millisecond')
               ELSE "createdAt"
             END
         WHERE id = $1`,
        [row.id, expected.alg, expected.crv, !expected.current],
      );
    }
    if (
      expected.current &&
      index === 0 &&
      (!row.expiresAt || row.expiresAt > now) &&
      (metadataMatches || !checkOnly)
    ) {
      needsCurrentKey = false;
    }
  }

  if (checkOnly && needsCurrentKey)
    throw new Error("Better Auth JWKS has no active ES256/P-256 signing key");
  if (!checkOnly && needsCurrentKey) {
    // Signing once exercises private-key encryption/decryption and creates a
    // fresh ES256 key when the newest legacy key was retired.  The token is
    // intentionally discarded; this is migration evidence, not a session.
    await auth.api.signJWT({
      body: { payload: { sub: "omi-auth-migration-probe" } },
      headers: { "content-type": "application/json" },
    });
  }

  const latest = await pool.query(
    `SELECT alg, crv, "expiresAt" FROM "jwks" ORDER BY "createdAt" DESC LIMIT 1`,
  );
  const key = latest.rows[0];
  if (
    !key ||
    key.alg !== "ES256" ||
    (key.crv && key.crv !== "P-256") ||
    (key.expiresAt && key.expiresAt <= new Date())
  ) {
    throw new Error("Better Auth JWKS migration did not converge to an active ES256 key");
  }
}

try {
  const checkOnly = process.argv.includes("--check");
  const before = await pendingMigrations();
  if (checkOnly) {
    if (before.pending) {
      throw new Error(
        `Better Auth schema is not current: ${before.migrations.toBeCreated.length} tables and ${before.migrations.toBeAdded.length} columns pending`,
      );
    }
    await checkJwksMetadataColumns();
    await reconcileJwksRows({ checkOnly: true });
    console.log("Better Auth schema/JWKS check OK: no pending migrations");
  } else {
    await before.migrations.runMigrations();
    const after = await pendingMigrations();
    if (after.pending) {
      throw new Error(
        `Better Auth migration did not converge: ${after.migrations.toBeCreated.length} tables and ${after.migrations.toBeAdded.length} columns remain`,
      );
    }
    await ensureJwksMetadataColumns();
    await reconcileJwksRows({ checkOnly: false });
    await reconcileJwksRows({ checkOnly: true });
    console.log("Better Auth schema/JWKS migration OK: schema and signing keys are current");
  }
} finally {
  await pool.end();
}
