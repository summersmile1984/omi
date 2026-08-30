export type StreamingAiBinding = {
  run(
    model: string,
    inputs: Record<string, unknown>,
    options: { websocket: true; tags?: string[] },
  ): Promise<Response>;
};

export type RealtimeEnv = {
  REALTIME_SESSIONS: DurableObjectNamespace;
  APP_DB: D1Database;
  AI?: StreamingAiBinding;
  INTERNAL_ASSERTION_SECRET?: string;
  ASR_WS_URL?: string;
  ASR_API_KEY?: string;
};
