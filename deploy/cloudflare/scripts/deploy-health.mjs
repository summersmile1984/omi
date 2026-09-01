const DEFAULT_STAGING_HEALTH_TARGETS = [
  {
    name: "edge",
    url: "https://omi-cf-edge-staging.summersmile1984.workers.dev/ready",
  },
  {
    name: "web",
    url: "https://omi-web-app-staging.summersmile1984.workers.dev/api/worker-ready",
  },
];

export function stagingHealthTargets(overrides = {}) {
  return DEFAULT_STAGING_HEALTH_TARGETS.map((target) => ({
    ...target,
    url: overrides[target.name] || target.url,
  }));
}

export async function verifyStagingHealth({
  fetchImpl = fetch,
  targets = stagingHealthTargets(),
  timeoutMs = 15_000,
  attempts = 6,
  retryDelayMs = 2_000,
  sleep = (delayMs) =>
    new Promise((resolve) => globalThis.setTimeout(resolve, delayMs)),
} = {}) {
  if (!Number.isInteger(attempts) || attempts < 1) {
    throw new Error("health attempts must be a positive integer");
  }
  const statuses = {};
  for (const target of targets) {
    let lastError;
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      try {
        const response = await fetchImpl(target.url, {
          signal: AbortSignal.timeout(timeoutMs),
        });
        statuses[target.name] = response.status;
        if (response.status !== 200) {
          throw new Error(
            `${target.name} expected HTTP 200, received HTTP ${response.status}`,
          );
        }
        let body;
        try {
          body = await response.json();
        } catch {
          throw new Error(
            `${target.name} readiness response was not valid JSON`,
          );
        }
        if (!body || typeof body !== "object" || body.status !== "ready") {
          throw new Error(
            `${target.name} readiness response did not report status=ready`,
          );
        }
        lastError = undefined;
        break;
      } catch (error) {
        lastError =
          error instanceof Error
            ? error
            : new Error(`${target.name} health request failed: unknown error`);
        if (attempt < attempts) await sleep(retryDelayMs);
      }
    }
    if (lastError) throw lastError;
  }
  return statuses;
}
