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
  /** Firebase Admin service-account JSON; secret, never exposed to clients. */
  FIREBASE_SERVICE_ACCOUNT_JSON?: string;
  /** Firebase Web API key used only for the optional REST token exchange. */
  FIREBASE_API_KEY?: string;
  /** Optional assertion that the configured service account belongs to this project. */
  FIREBASE_PROJECT_ID?: string;
  /** Explicit staging gate; exact legacy auth routes remain untouched. */
  FIREBASE_CUSTOM_TOKEN_BRIDGE_STAGING_ENABLED?: string;
  /** Optional 60..3600-second custom-token lifetime (default 300). */
  FIREBASE_CUSTOM_TOKEN_TTL_SECONDS?: string;
  /** Optional staging-only bridge for the Flutter Better Auth dev sign-in. */
  AUTH_DEV_ISSUER_SECRET?: string;
  /** Explicit gate for the namespaced native-auth compatibility seam. */
  LEGACY_AUTH_COMPAT_STAGING_ENABLED?: string;
  /** Explicit gate for the exact /v1/auth/* staging owner. */
  LEGACY_AUTH_EXACT_STAGING_ENABLED?: string;
  /** Secret used to derive the AES-GCM key for native-auth transaction envelopes. */
  LEGACY_AUTH_TRANSACTION_ENCRYPTION_SECRET?: string;
  /** Public HTTPS origin used to construct provider callback URLs. */
  NATIVE_AUTH_PUBLIC_BASE_URL?: string;
};
