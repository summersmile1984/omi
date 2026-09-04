import crypto from "node:crypto";
import { createLocalJWKSet, jwtVerify } from "jose";
import { verifyAccessToken } from "../../auth/shared/access-verifier.mjs";

/** Express boundary; the database remains authoritative for session revocation. */
export function accessHandler({ auth, policy, pool, secret }) {
  return async (req, res) => {
    const presented = Buffer.from(req.get("x-internal-assertion-secret") || "");
    const expected = Buffer.from(secret);
    if (
      !expected.length ||
      presented.length !== expected.length ||
      !crypto.timingSafeEqual(presented, expected)
    ) {
      return res.status(401).json({ error: "unauthorized" });
    }
    const token = /^Bearer\s+(.+)$/i.exec(req.get("authorization") || "")?.[1];
    if (!token) return res.status(401).json({ error: "unauthorized" });
    try {
      const identity = await verifyAccessToken(token, policy, {
        getJwks: () => auth.api.getJwks(),
        verifySignature: (value, keys) =>
          jwtVerify(value, createLocalJWKSet(keys), {
            algorithms: ["ES256", "RS256", "EdDSA"],
            issuer: policy.issuer,
            audience: policy.audience,
          }),
      });
      if (!identity) return res.status(401).json({ error: "unauthorized" });
      const active = await pool.query(
        `SELECT s.id FROM "session" s JOIN "user" u ON u.id = s."userId"
         WHERE s.id = $1 AND s."userId" = $2 AND s."expiresAt" > NOW()`,
        [identity.sid, identity.uid],
      );
      if (!active.rows.length)
        return res.status(401).json({ error: "unauthorized" });
      res.set("cache-control", "no-store");
      return res.json({
        uid: identity.uid,
        sessionGeneration: identity.sid,
        authority: "better-auth",
      });
    } catch {
      return res.status(503).json({ error: "identity_store_unavailable" });
    }
  };
}
