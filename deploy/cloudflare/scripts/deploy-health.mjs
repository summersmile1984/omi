const DEFAULT_STAGING_HEALTH_TARGETS = [
  { name: "edge", url: "https://omi-cf-edge-staging.summersmile1984.workers.dev/health" },
  { name: "auth", url: "https://omi-cf-auth-staging.summersmile1984.workers.dev/ready" },
  { name: "api-core", url: "https://omi-cf-api-core-staging.summersmile1984.workers.dev/health" },
  { name: "api-ai", url: "https://omi-cf-api-ai-staging.summersmile1984.workers.dev/health" },
  { name: "realtime", url: "https://omi-cf-realtime-staging.summersmile1984.workers.dev/health" },
  { name: "jobs", url: "https://omi-cf-jobs-staging.summersmile1984.workers.dev/health" },
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
} = {}) {
  const statuses = {};
  for (const target of targets) {
    let response;
    try {
      response = await fetchImpl(target.url, { signal: AbortSignal.timeout(timeoutMs) });
      await response.arrayBuffer();
    } catch (error) {
      throw new Error(`${target.name} health request failed: ${error instanceof Error ? error.message : "unknown error"}`);
    }
    statuses[target.name] = response.status;
    if (response.status !== 200) {
      throw new Error(`${target.name} expected HTTP 200, received HTTP ${response.status}`);
    }
  }
  return statuses;
}
