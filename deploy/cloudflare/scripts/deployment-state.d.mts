export const STAGING_WORKERS: readonly string[];

export type DeploymentSnapshot = {
  version: 1;
  environment: "staging";
  createdAt: string;
  workers: Record<string, string>;
};

export function activeVersionFromStatus(
  raw: string | { versions?: unknown[] },
  workerName: string,
): string;

export function createDeploymentSnapshot(
  statuses: Record<string, string | { versions?: unknown[] }>,
  createdAt?: Date,
): DeploymentSnapshot;

export function rollbackPlan(
  snapshot: DeploymentSnapshot | unknown,
): Array<{ workerName: string; versionId: string }>;
