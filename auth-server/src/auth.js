import { betterAuth } from "better-auth";
import { bearer, jwt } from "better-auth/plugins";
import crypto from "node:crypto";
import pg from "pg";

export const PORT = process.env.PORT || 3000;
const IS_PRODUCTION = process.env.NODE_ENV === "production";
const DATABASE_URL =
  process.env.DATABASE_URL ||
  "postgresql://omi:omi-dev-password@localhost:5434/omi";
const SECRET =
  process.env.BETTER_AUTH_SECRET ||
  "dev-only-better-auth-secret-change-me-32bytes-min";
export const BASE_URL =
  process.env.BETTER_AUTH_URL || `http://127.0.0.1:${PORT}`;
export const DEV_ISSUER_SECRET = process.env.AUTH_DEV_ISSUER_SECRET || "";
export const INTERNAL_ADMIN_SECRET =
  process.env.AUTH_INTERNAL_ADMIN_SECRET || "";
const TRUSTED_ORIGINS = (process.env.BETTER_AUTH_TRUSTED_ORIGINS || "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);
const IP_ADDRESS_HEADERS = (
  process.env.BETTER_AUTH_IP_HEADERS || "x-forwarded-for"
)
  .split(",")
  .map((header) => header.trim().toLowerCase())
  .filter(Boolean);
const JWT_ISSUER = process.env.AUTH_JWT_ISSUER || new URL(BASE_URL).origin;
const JWT_AUDIENCE = process.env.AUTH_JWT_AUDIENCE || JWT_ISSUER;
const DEV_SECRET = "dev-only-better-auth-secret-change-me-32bytes-min";

if (IS_PRODUCTION) {
  const required = [
    "DATABASE_URL",
    "BETTER_AUTH_SECRET",
    "BETTER_AUTH_URL",
    "AUTH_INTERNAL_ADMIN_SECRET",
    "BETTER_AUTH_TRUSTED_ORIGINS",
    "BETTER_AUTH_IP_HEADERS",
  ].filter((name) => !process.env[name]?.trim());
  if (required.length)
    throw new Error(
      `Production auth configuration missing: ${required.join(", ")}`,
    );
  if (SECRET === DEV_SECRET || SECRET.length < 32)
    throw new Error(
      "BETTER_AUTH_SECRET must be a non-development secret of at least 32 characters",
    );
  if (DEV_ISSUER_SECRET)
    throw new Error("AUTH_DEV_ISSUER_SECRET must be unset in production");
  if (new URL(BASE_URL).protocol !== "https:")
    throw new Error("BETTER_AUTH_URL must use https in production");
  if (
    TRUSTED_ORIGINS.some(
      (origin) =>
        new URL(origin).hostname === "localhost" ||
        new URL(origin).hostname === "127.0.0.1",
    )
  ) {
    throw new Error(
      "Production trusted origins must not contain loopback hosts",
    );
  }
}

const { Pool } = pg;
export const pool = new Pool({ connectionString: DATABASE_URL });

const jwksAdapter = {
  async getJwks() {
    const result = await pool.query(
      `SELECT id, "publicKey", "privateKey", "createdAt", "expiresAt", alg, crv
       FROM "jwks"`,
    );
    return result.rows;
  },
  async createJwk(webKey) {
    const id = crypto.randomUUID();
    const publicJwk = JSON.parse(webKey.publicKey);
    const result = await pool.query(
      `INSERT INTO "jwks"
         (id, "publicKey", "privateKey", "createdAt", "expiresAt", alg, crv)
       VALUES ($1, $2, $3, $4, $5, $6, $7)
       RETURNING id, "publicKey", "privateKey", "createdAt", "expiresAt", alg, crv`,
      [
        id,
        webKey.publicKey,
        webKey.privateKey,
        webKey.createdAt,
        webKey.expiresAt || null,
        webKey.alg || null,
        webKey.crv || publicJwk.crv || null,
      ],
    );
    return result.rows[0];
  },
};

export const authOptions = {
  secret: SECRET,
  baseURL: BASE_URL,
  trustedOrigins: TRUSTED_ORIGINS,
  database: pool,
  emailAndPassword: {
    enabled: true,
  },
  user: {
    deleteUser: { enabled: true },
  },
  advanced: {
    useSecureCookies: IS_PRODUCTION,
    ipAddress: {
      ipAddressHeaders: IP_ADDRESS_HEADERS,
    },
  },
  plugins: [
    bearer({ requireSignature: true }),
    jwt({
      adapter: jwksAdapter,
      jwks: {
        keyPairConfig: { alg: "ES256" },
        rotationInterval: Number(
          process.env.AUTH_JWKS_ROTATION_SECONDS || 2592000,
        ),
        gracePeriod: Number(process.env.AUTH_JWKS_GRACE_SECONDS || 2592000),
      },
      jwt: {
        issuer: JWT_ISSUER,
        audience: JWT_AUDIENCE,
        expirationTime: process.env.AUTH_JWT_EXPIRATION || "15m",
      },
    }),
  ],
};

export const auth = betterAuth(authOptions);
