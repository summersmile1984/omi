export const STAGING_WORKERS = [
  "omi-cf-auth-staging",
  "omi-cf-api-core-staging",
  "omi-cf-api-ai-staging",
  "omi-cf-realtime-staging",
  "omi-cf-jobs-staging",
  "omi-cf-edge-staging",
  "omi-web-app-staging",
];

const ROLLBACK_ORDER = [
  "omi-web-app-staging",
  "omi-cf-edge-staging",
  "omi-cf-jobs-staging",
  "omi-cf-realtime-staging",
  "omi-cf-api-ai-staging",
  "omi-cf-api-core-staging",
  "omi-cf-auth-staging",
];

export function activeVersionFromStatus(raw, workerName) {
  const status = typeof raw === "string" ? JSON.parse(raw) : raw;
  const versions = Array.isArray(status?.versions) ? status.versions : [];
  const active = versions.filter(
    (version) =>
      version &&
      typeof version.version_id === "string" &&
      version.version_id.length > 0 &&
      version.percentage === 100,
  );
  if (active.length !== 1 || versions.length !== 1) {
    throw new Error(
      `${workerName} must have exactly one 100% active version before a staging release`,
    );
  }
  return active[0].version_id;
}

export function createDeploymentSnapshot(statuses, createdAt = new Date()) {
  const workers = {};
  for (const workerName of STAGING_WORKERS) {
    if (!(workerName in statuses)) {
      throw new Error(`missing deployment status for ${workerName}`);
    }
    workers[workerName] = activeVersionFromStatus(
      statuses[workerName],
      workerName,
    );
  }
  return {
    version: 1,
    environment: "staging",
    createdAt: createdAt.toISOString(),
    workers,
  };
}

export function rollbackPlan(snapshot) {
  if (
    snapshot?.version !== 1 ||
    snapshot?.environment !== "staging" ||
    !snapshot.workers ||
    typeof snapshot.workers !== "object"
  ) {
    throw new Error("invalid Cloudflare staging deployment snapshot");
  }
  return ROLLBACK_ORDER.map((workerName) => {
    const versionId = snapshot.workers[workerName];
    if (typeof versionId !== "string" || !/^[A-Za-z0-9-]+$/.test(versionId)) {
      throw new Error(`invalid rollback version for ${workerName}`);
    }
    return { workerName, versionId };
  });
}
