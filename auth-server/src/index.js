// Better Auth service — self-hosted auth for the 4C8G Omi deployment.
//
// Provides email+password signup/signin and a JWT plugin that signs ES256
// JWTs carrying a `uid` claim. The Python backend verifies these via
// utils/auth_shim.py (JWKS at /api/auth/jwks).
//
// User data is stored in PostgreSQL (reuses the same server as the shim).
// Docs: https://better-auth.com
//
// Env:
//   PORT            (default 3000)
//   DATABASE_URL    postgres://... (default localhost:5434 omi)
//   BETTER_AUTH_SECRET  signing secret for the session/JWT infrastructure
//   BETTER_AUTH_URL     public base URL of this service
//   AUTH_DEV_ISSUER_SECRET  enables the local-only /auth-issue bridge
import crypto from "node:crypto";
import express from "express";
import {
  auth,
  BASE_URL,
  DEV_ISSUER_SECRET,
  INTERNAL_ADMIN_SECRET,
  pool,
  PORT,
} from "./auth.js";
import { betterAuthBridge } from "./http.js";

const app = express();
app.use(express.json());

function internalAuthorized(req) {
  if (!INTERNAL_ADMIN_SECRET) return false;
  const authorization = req.get("authorization") || "";
  const presented = authorization.startsWith("Bearer ")
    ? authorization.slice(7)
    : "";
  const presentedBuffer = Buffer.from(presented);
  const expectedBuffer = Buffer.from(INTERNAL_ADMIN_SECRET);
  return (
    presentedBuffer.length === expectedBuffer.length &&
    crypto.timingSafeEqual(presentedBuffer, expectedBuffer)
  );
}

async function identityResiduals(uid) {
  const result = await pool.query(
    `SELECT
       (SELECT count(*)::int FROM "user" WHERE id = $1) AS users,
       (SELECT count(*)::int FROM "session" WHERE "userId" = $1) AS sessions,
       (SELECT count(*)::int FROM "account" WHERE "userId" = $1) AS accounts`,
    [uid],
  );
  return result.rows[0];
}

app.get("/internal/users/:uid", async (req, res) => {
  if (!internalAuthorized(req))
    return res.status(401).json({ error: "unauthorized" });
  try {
    const context = await auth.$context;
    const user = await context.internalAdapter.findUserById(req.params.uid);
    if (!user) return res.status(404).json({ error: "user_not_found" });
    return res.json({ user });
  } catch (err) {
    return res.status(503).json({ error: "identity_store_unavailable" });
  }
});

app.delete("/internal/users/:uid", async (req, res) => {
  if (!internalAuthorized(req))
    return res.status(401).json({ error: "unauthorized" });
  try {
    const context = await auth.$context;
    const user = await context.internalAdapter.findUserById(req.params.uid);
    if (!user) return res.status(404).json({ error: "user_not_found" });
    await context.internalAdapter.deleteUser(req.params.uid);
    const residuals = await identityResiduals(req.params.uid);
    if (Object.values(residuals).some((count) => count !== 0))
      return res.status(503).json({ error: "identity_deletion_incomplete" });
    return res.json({ success: true });
  } catch (err) {
    return res.status(503).json({ error: "identity_store_unavailable" });
  }
});

app.get("/internal/users/:uid/residuals", async (req, res) => {
  if (!internalAuthorized(req))
    return res.status(401).json({ error: "unauthorized" });
  try {
    // Better Auth's PostgreSQL adapter owns these three UID-bearing tables.
    // Query them directly so account-deletion reconciliation proves session
    // and linked-account cascade instead of inferring it from a missing user.
    return res.json(await identityResiduals(req.params.uid));
  } catch (_err) {
    return res.status(503).json({ error: "identity_store_unavailable" });
  }
});

// Express 4 does not automatically translate a rejected async handler into an
// HTTP response.  Keep the identity boundary fail-closed and explicitly
// retryable when Better Auth or PostgreSQL is unavailable.
app.all("/api/auth/*", betterAuthBridge(auth.handler, BASE_URL));

// Local development bridge for clients that cannot complete an OAuth flow.
// It is absent unless explicitly enabled and requires a separate bearer secret;
// production clients must use Better Auth's session-authenticated JWT endpoint.
if (DEV_ISSUER_SECRET) {
  app.post("/auth-issue", async (req, res) => {
    const authorization = req.get("authorization") || "";
    const presented = authorization.startsWith("Bearer ")
      ? authorization.slice(7)
      : "";
    const presentedBuffer = Buffer.from(presented);
    const expectedBuffer = Buffer.from(DEV_ISSUER_SECRET);
    const authorized =
      presentedBuffer.length === expectedBuffer.length &&
      crypto.timingSafeEqual(presentedBuffer, expectedBuffer);
    if (!authorized) return res.status(401).json({ error: "unauthorized" });

    const uid = req.body?.uid;
    if (typeof uid !== "string" || !uid.trim())
      return res.status(400).json({ error: "uid required" });
    try {
      const result = await auth.api.signJWT({
        body: { payload: { uid, sub: uid } },
        headers: { "content-type": "application/json" },
      });
      res.json({ ...result, uid });
    } catch (err) {
      res.status(500).json({ error: String(err?.message || err) });
    }
  });
}

// Health check
app.get("/health", (_req, res) => res.json({ status: "ok" }));
app.get("/ready", async (_req, res) => {
  try {
    await pool.query("SELECT 1");
    res.json({ status: "ready" });
  } catch (_err) {
    res.status(503).json({ status: "unavailable" });
  }
});

app.listen(PORT, () => {
  console.log(
    `omi-auth-server listening on :${PORT} (JWKS at ${BASE_URL}/api/auth/jwks)`,
  );
});
