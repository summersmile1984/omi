import type { AuthContext } from "../shared/auth-context";

export type EdgeEnv = {
  AUTH: Fetcher;
  API_CORE: Fetcher;
  API_AI: Fetcher;
  REALTIME: Fetcher;
  JOBS: Fetcher;
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
  ALLOWED_ORIGINS?: string;
};

export type EdgeVariables = {
  authContext?: AuthContext;
};
