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
} = {}) {
  const statuses = {};
  for (const target of targets) {
    let response;
    try {
      response = await fetchImpl(target.url, {
        signal: AbortSignal.timeout(timeoutMs),
      });
      await response.arrayBuffer();
    } catch (error) {
      throw new Error(
        `${target.name} health request failed: ${error instanceof Error ? error.message : "unknown error"}`,
      );
    }
    statuses[target.name] = response.status;
    if (response.status !== 200) {
      throw new Error(
        `${target.name} expected HTTP 200, received HTTP ${response.status}`,
      );
    }
  }
  return statuses;
}
