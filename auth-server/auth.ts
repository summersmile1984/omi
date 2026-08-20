// Better Auth config (used by the CLI to generate the schema).
// The runtime entrypoint is src/index.js; this mirrors the same auth options.
import { betterAuth } from "better-auth";
import { bearer, jwt } from "better-auth/plugins";
import { Pool } from "pg";

const DATABASE_URL =
  process.env.DATABASE_URL ||
  "postgresql://omi:omi-dev-password@localhost:5434/omi";
const SECRET =
  process.env.BETTER_AUTH_SECRET ||
  "dev-only-better-auth-secret-change-me-32bytes-min";
const BASE_URL = process.env.BETTER_AUTH_URL || "http://127.0.0.1:3000";
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

export const auth = betterAuth({
  secret: SECRET,
  baseURL: BASE_URL,
  trustedOrigins: TRUSTED_ORIGINS,
  database: new Pool({ connectionString: DATABASE_URL }),
  emailAndPassword: {
    enabled: true,
  },
  user: {
    deleteUser: { enabled: true },
  },
  advanced: {
    useSecureCookies: process.env.NODE_ENV === "production",
    ipAddress: {
      ipAddressHeaders: IP_ADDRESS_HEADERS,
    },
  },
  plugins: [
    bearer({ requireSignature: true }),
    jwt({
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
});
