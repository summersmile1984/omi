/** @param {import("./fallback.mjs").FallbackEvent} event */
export function recordFallback(event) {
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
