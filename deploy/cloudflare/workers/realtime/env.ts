export type RealtimeEnv = {
  REALTIME_SESSIONS: DurableObjectNamespace;
  AUTH: Fetcher;
  INTERNAL_ASSERTION_SECRET?: string;
  ASR_WS_URL?: string;
  ASR_API_KEY?: string;
};
