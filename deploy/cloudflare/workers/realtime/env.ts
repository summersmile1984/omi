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
  // Shared rate-limit Durable Object; admission control fails open with
  // fallback telemetry when the binding is absent.
  RATE_LIMITS?: DurableObjectNamespace;
  INTERNAL_ASSERTION_SECRET?: string;
  ASR_WS_URL?: string;
  ASR_API_KEY?: string;
};
