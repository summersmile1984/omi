export type FallbackOutcome = "recovered" | "degraded" | "exhausted";

export type WorkerFallbackEvent = {
  component: "rate_limit" | "other";
  from: "durable_object" | "none";
  to: "unlimited" | "none";
  reason: "dependency_unavailable" | "invalid_response" | "other";
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
