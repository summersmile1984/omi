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
import { betterAuth } from "better-auth";
import { jwt } from "better-auth/plugins";
import express from "express";
import pg from "pg";

const PORT = process.env.PORT || 3000;
const DATABASE_URL =
  process.env.DATABASE_URL ||
  "postgresql://omi:omi-dev-password@localhost:5434/omi";
const SECRET =
  process.env.BETTER_AUTH_SECRET ||
  "dev-only-better-auth-secret-change-me-32bytes-min";
const BASE_URL = process.env.BETTER_AUTH_URL || `http://127.0.0.1:${PORT}`;
const DEV_ISSUER_SECRET = process.env.AUTH_DEV_ISSUER_SECRET || "";

const { Pool } = pg;

export const auth = betterAuth({
  secret: SECRET,
  baseURL: BASE_URL,
  trustHost: true,
  // Better Auth's built-in Kysely adapter accepts a pg.Pool directly (docs:
  // https://better-auth.com/docs/adapters/postgresql). It auto-migrates the
  // schema (user/session/etc. tables) on first start.
  database: new Pool({ connectionString: DATABASE_URL }),
  emailAndPassword: {
    enabled: true,
  },
  plugins: [
    jwt({
      jwt: {
        // ES256 asymmetric signing so the Python shim can verify with the
        // public key from /api/auth/jwks (no shared secret in the backend).
        jwks: {
          keyPairConfig: { alg: "ES256" },
        },
        // Long-lived JWT for the desktop app session (default Better Auth is
        // 15m). Override with AUTH_JWT_EXPIRATION (e.g. "24h").
        expirationTime: process.env.AUTH_JWT_EXPIRATION || "24h",
      },
    }),
  ],
});

const app = express();
app.use(express.json());

// Bridge express req/res -> Web Request (Better Auth handler takes a single
// Request with an absolute URL).
app.all("/api/auth/*", async (req, res) => {
  const url = new URL(req.originalUrl, BASE_URL).toString();
  const headers = new Headers();
  for (const [k, v] of Object.entries(req.headers)) {
    if (v !== undefined) headers.set(k, Array.isArray(v) ? v.join(", ") : String(v));
  }
  let body = null;
  if (["POST", "PUT", "PATCH"].includes(req.method) && req.body !== undefined) {
    body = JSON.stringify(req.body);
    headers.set("Content-Type", "application/json");
  }
  const request = new Request(url, { method: req.method, headers, body });
  const response = await auth.handler(request);
  res.status(response.status);
  response.headers.forEach((v, k) => res.setHeader(k, v));
  res.send(await response.text());
});

// Local development bridge for clients that cannot complete an OAuth flow.
// It is absent unless explicitly enabled and requires a separate bearer secret;
// production clients must use Better Auth's session-authenticated JWT endpoint.
if (DEV_ISSUER_SECRET) {
  app.post("/auth-issue", async (req, res) => {
    const authorization = req.get("authorization") || "";
    const presented = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
    const presentedBuffer = Buffer.from(presented);
    const expectedBuffer = Buffer.from(DEV_ISSUER_SECRET);
    const authorized =
      presentedBuffer.length === expectedBuffer.length && crypto.timingSafeEqual(presentedBuffer, expectedBuffer);
    if (!authorized) return res.status(401).json({ error: "unauthorized" });

    const uid = req.body?.uid;
    if (typeof uid !== "string" || !uid.trim()) return res.status(400).json({ error: "uid required" });
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

app.listen(PORT, () => {
  console.log(`omi-auth-server listening on :${PORT} (JWKS at ${BASE_URL}/api/auth/jwks)`);
});
