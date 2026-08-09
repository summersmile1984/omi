// Better Auth config (used by the CLI to generate the schema).
// The runtime entrypoint is src/index.js; this mirrors the same auth options.
import { betterAuth } from "better-auth";
import { jwt } from "better-auth/plugins";
import { Pool } from "pg";

const DATABASE_URL =
  process.env.DATABASE_URL ||
  "postgresql://omi:omi-dev-password@localhost:5434/omi";
const SECRET =
  process.env.BETTER_AUTH_SECRET ||
  "dev-only-better-auth-secret-change-me-32bytes-min";
const BASE_URL = process.env.BETTER_AUTH_URL || "http://127.0.0.1:3000";

export const auth = betterAuth({
  secret: SECRET,
  baseURL: BASE_URL,
  trustHost: true,
  database: new Pool({ connectionString: DATABASE_URL }),
  emailAndPassword: {
    enabled: true,
  },
  plugins: [
    jwt({
      jwt: {
        jwks: {
          keyPairConfig: { alg: "ES256" },
        },
      },
    }),
  ],
});
