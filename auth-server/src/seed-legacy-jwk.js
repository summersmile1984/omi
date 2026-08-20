// Regression fixture for a pre-ES256 Better Auth database.  This is used only
// by the disposable migration gate and deliberately inserts a legacy Ed25519
// row without alg/crv metadata, matching the schema that made ES256 signing
// select an incompatible newest key.
import fs from "node:fs";
import { symmetricEncrypt } from "better-auth/crypto";
import { SignJWT, exportJWK, generateKeyPair } from "jose";
import { auth, pool } from "./auth.js";

const outputIndex = process.argv.indexOf("--output");
if (outputIndex < 0 || !process.argv[outputIndex + 1])
  throw new Error("usage: node src/seed-legacy-jwk.js --output PATH");

try {
  const { publicKey, privateKey } = await generateKeyPair("EdDSA", {
    crv: "Ed25519",
    extractable: true,
  });
  const publicJwk = await exportJWK(publicKey);
  const privateJwk = await exportJWK(privateKey);
  const context = await auth.$context;
  const encryptedPrivateKey = await symmetricEncrypt({
    key: context.secretConfig,
    data: JSON.stringify(privateJwk),
  });
  const id = `legacy-eddsa-${Date.now()}`;
  await pool.query(
    `INSERT INTO "jwks"
       (id, "publicKey", "privateKey", "createdAt", "expiresAt", alg, crv)
     VALUES ($1, $2, $3, now(), NULL, NULL, NULL)`,
    [id, JSON.stringify(publicJwk), JSON.stringify(encryptedPrivateKey)],
  );

  let rejected = false;
  try {
    await auth.api.signJWT({
      body: { payload: { sub: "legacy-signing-regression" } },
      headers: { "content-type": "application/json" },
    });
  } catch (error) {
    rejected = /JWK|key|decrypt|alg/i.test(String(error?.message || error));
  }
  if (!rejected)
    throw new Error("legacy JWKS fixture did not reproduce incompatible signing");

  const issuer = process.env.AUTH_JWT_ISSUER;
  const audience = process.env.AUTH_JWT_AUDIENCE;
  const now = Math.floor(Date.now() / 1000);
  const token = await new SignJWT({ uid: "legacy-jwks-user" })
    .setProtectedHeader({ alg: "EdDSA", kid: id })
    .setSubject("legacy-jwks-user")
    .setIssuer(issuer)
    .setAudience(audience)
    .setIssuedAt(now)
    .setExpirationTime(now + 900)
    .sign(privateKey);
  fs.writeFileSync(
    process.argv[outputIndex + 1],
    `${JSON.stringify({ id, token })}\n`,
    { mode: 0o600 },
  );
  console.log("Legacy EdDSA JWKS regression fixture inserted and reproduced");
} finally {
  await pool.end();
}
