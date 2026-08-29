export type AuthEnv = {
  AUTH_DB: D1Database;
  BETTER_AUTH_SECRET: string;
  BETTER_AUTH_URL?: string;
  MCP_RESOURCE_URL?: string;
  MCP_ALLOW_UNAUTHENTICATED_DCR?: string;
  ALLOWED_ORIGINS?: string;
  INTERNAL_ASSERTION_SECRET?: string;
  GOOGLE_CLIENT_ID?: string;
  GOOGLE_CLIENT_SECRET?: string;
  APPLE_CLIENT_ID?: string;
  APPLE_CLIENT_SECRET?: string;
  APPLE_APP_BUNDLE_IDENTIFIER?: string;
  AUTH_FIREBASE_SCRYPT_SIGNER_KEY?: string;
  AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR?: string;
  AUTH_FIREBASE_SCRYPT_ROUNDS?: string;
  AUTH_FIREBASE_SCRYPT_MEM_COST?: string;
  /** Optional staging-only bridge for the Flutter Better Auth dev sign-in. */
  AUTH_DEV_ISSUER_SECRET?: string;
};
