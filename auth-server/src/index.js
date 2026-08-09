// Better Auth service — self-hosted auth for the 4C8G Omi deployment.
//
// Provides email+password signup/signin and a JWT plugin that signs ES256
// JWTs carrying a `uid` claim. The Python backend verifies these via
// utils/auth_shim.py (JWKS at /jwks).
//
// User data is stored in PostgreSQL (reuses the same server as the shim).
// Docs: https://better-auth.com
//
// Env:
//   PORT            (default 3000)
//   DATABASE_URL    postgres://... (default localhost:5434 omi)
//   BETTER_AUTH_SECRET  signing secret for the session/JWT infrastructure
//   BETTER_AUTH_URL     public base URL of this service
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
        // public key from /jwks (no shared secret in the backend).
        jwks: {
          keyPairConfig: { alg: "ES256" },
        },
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

// Issue a JWT for a uid (for services that call the backend with Bearer tokens).
// Uses Better Auth's server-only signJWT so the ES256 key from the jwt plugin
// signs it; the Python shim verifies via /api/auth/jwks.
// NOTE: registered BEFORE the /api/auth/* wildcard so it is not swallowed.
app.post("/auth-issue", async (req, res) => {
  const uid = req.body?.uid;
  if (!uid) return res.status(400).json({ error: "uid required" });
  try {
    const result = await auth.api.signJWT({
      body: { payload: { uid, sub: uid } },
      headers: { "content-type": "application/json" },
    });
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: String(err?.message || err) });
  }
});

// Health check
app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.listen(PORT, () => {
  console.log(`omi-auth-server listening on :${PORT} (JWKS at ${BASE_URL}/jwks)`);
});
