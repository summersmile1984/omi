export const PRODUCTION_CONFIRMATION: string;
export const PRODUCTION_DEPLOYMENTS: ReadonlyArray<{
  workerName: string;
  runtime: "typescript" | "python";
  target: string;
}>;
export const PRODUCTION_WORKERS: ReadonlyArray<string>;
export const PRODUCTION_RESOURCES: {
  d1: ReadonlyArray<string>;
  r2: ReadonlyArray<string>;
  queues: ReadonlyArray<string>;
  vectorize: ReadonlyArray<string>;
};

export function assertProductionConfirmation(env?: Record<string, string | undefined>): void;
export function resolveWorkersSubdomain(raw?: string): string;
export function productionUrls(subdomain?: string): {
  auth: string;
  edge: string;
  web: string;
};
export function assertValidProductionDeploymentOrder(
  deployments?: ReadonlyArray<{
    workerName: string;
    runtime: "typescript" | "python";
    target: string;
  }>,
): void;
export function renderProductionConfig(
  source: string,
  options: {
    appDatabaseId: string;
    authDatabaseId: string;
    subdomain?: string;
  },
): string;
export function createInitialProductionSecrets(
  subdomain?: string,
  random?: () => string,
): {
  version: number;
  environment: string;
  workers: Record<string, Record<string, string>>;
};
export function createProductionDeploymentSnapshot(
  statuses: Record<string, string | object | null>,
  createdAt?: Date,
): {
  version: number;
  environment: string;
  createdAt: string;
  workers: Record<string, string | null>;
};
export function productionRollbackPlan(snapshot: object): Array<{
  action: "delete" | "rollback";
  workerName: string;
  versionId: string | null;
}>;
