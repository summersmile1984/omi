export type AuthEnv = {
  AUTH_DB: D1Database;
  BETTER_AUTH_SECRET: string;
  BETTER_AUTH_URL?: string;
  ALLOWED_ORIGINS?: string;
  INTERNAL_ASSERTION_SECRET?: string;
  /** Optional staging-only bridge for the Flutter Better Auth dev sign-in. */
  AUTH_DEV_ISSUER_SECRET?: string;
};
