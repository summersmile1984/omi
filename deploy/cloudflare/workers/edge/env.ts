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
  /** Explicit opt-in for the exact native Firebase auth routes in Auth Worker. */
  AUTH_EXACT_NATIVE_STAGING_ENABLED?: string;
  /** Explicit opt-in for the exact Firebase app-consent OAuth routes in Jobs. */
  AUTH_EXACT_OAUTH_STAGING_ENABLED?: string;
  /** Staging-only deny switch for legacy Twilio phone-call paths. */
  PHONE_TWILIO_STAGING_FAIL_CLOSED?: string;
  /** Staging-only deny switch for the legacy Gemini desktop proxy paths. */
  GEMINI_PROXY_STAGING_FAIL_CLOSED?: string;
  /** Explicit opt-in for the Cloudflare-owned AI Studio Gemini adapter. */
  GEMINI_PROXY_CLOUDFLARE_ENABLED?: string;
  /** Staging-only deny switch for legacy chat completion/materialization paths. */
  CHAT_COMPAT_STAGING_FAIL_CLOSED?: string;
  /** Cloudflare Jobs owner for the text/materialization compatibility routes. */
  CHAT_COMPATIBILITY_CLOUDFLARE_ENABLED?: string;
  /** Staging-only deny switch for legacy staged-task/task-intelligence paths. */
  TASK_INTELLIGENCE_STAGING_FAIL_CLOSED?: string;
  /** Staging-only deny switch for legacy Persona and app/MCP mutation paths. */
  PERSONA_APPS_STAGING_FAIL_CLOSED?: string;
  /** Explicit staging opt-in for the guarded canonical chat-file aliases. */
  LEGACY_CHAT_FILES_STAGING_ENABLED?: string;
  /** Explicit staging opt-in for legacy-shaped attachment SSE/JSON envelopes. */
  CHAT_ATTACHMENT_ENVELOPE_STAGING_ENABLED?: string;
  BYOK_FINGERPRINT_PEPPER?: string;
  ALLOWED_ORIGINS?: string;
};

export type EdgeVariables = {
  authContext?: AuthContext;
};
