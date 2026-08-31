import type { AuthContext } from "../shared/auth-context";

export type EdgeEnv = {
  AUTH: Fetcher;
  API_CORE: Fetcher;
  API_AI: Fetcher;
  REALTIME: Fetcher;
  JOBS: Fetcher;
  APP_DB?: D1Database;
  RATE_LIMITS: DurableObjectNamespace;
  RATE_LIMIT_BOOST?: string;
  RATE_LIMIT_SHADOW_MODE?: string;
  LEGACY_BACKEND_URL?: string;
  BETTER_AUTH_JWKS_URL?: string;
  BETTER_AUTH_ISSUER?: string;
  BETTER_AUTH_AUDIENCE?: string;
  MCP_RESOURCE_URL?: string;
  MCP_AUTHORIZATION_SERVER_URL?: string;
  INTERNAL_ASSERTION_SECRET?: string;
  /** Staging-only deny switch for legacy Firebase/OAuth compatibility paths. */
  AUTH_OAUTH_STAGING_FAIL_CLOSED?: string;
  /** Staging-only deny switch for legacy Twilio phone-call paths. */
  PHONE_TWILIO_STAGING_FAIL_CLOSED?: string;
  /** Staging-only deny switch for the legacy Gemini desktop proxy paths. */
  GEMINI_PROXY_STAGING_FAIL_CLOSED?: string;
  /** Staging-only deny switch for legacy chat completion/materialization paths. */
  CHAT_COMPAT_STAGING_FAIL_CLOSED?: string;
  /** Staging-only deny switch for legacy staged-task/task-intelligence paths. */
  TASK_INTELLIGENCE_STAGING_FAIL_CLOSED?: string;
  /** Staging-only deny switch for legacy Persona and app/MCP mutation paths. */
  PERSONA_APPS_STAGING_FAIL_CLOSED?: string;
  BYOK_FINGERPRINT_PEPPER?: string;
  ALLOWED_ORIGINS?: string;
};

export type EdgeVariables = {
  authContext?: AuthContext;
};
