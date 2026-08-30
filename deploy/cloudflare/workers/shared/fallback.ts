export type FallbackOutcome = "recovered" | "degraded" | "exhausted";

export type WorkerFallbackEvent = {
  component: "rate_limit" | "stt" | "other";
  from:
    | "durable_object"
    | "d1"
    | "fcm"
    | "workers_ai_streaming"
    | "workers_ai_classifier"
    | "restrict"
    | "none";
  to:
    | "durable_object_retry"
    | "deepgram_cloud"
    | "conservative_no_action"
    | "notification_outbox"
    | "throttle"
    | "unlimited"
    | "none";
  reason:
    | "dependency_unavailable"
    | "invalid_response"
    | "malformed_doc"
    | "malformed_response"
    | "other";
  outcome: FallbackOutcome;
  requestId?: string;
};

export function recordFallback(event: WorkerFallbackEvent): void {
  console.warn(
    JSON.stringify({
      event: "fallback",
      component: event.component,
      from: event.from,
      to: event.to,
      reason: event.reason,
      outcome: event.outcome,
      ...(event.requestId ? { request_id: event.requestId } : {}),
    }),
  );
}
