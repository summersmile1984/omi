export type ManifestValidationCounts = {
  routes: number;
  resources: number;
  redisFamilies: number;
  vectorNamespaces: number;
  r2Namespaces: number;
};

export function validateRouteManifest(
  routeManifest: unknown,
  edgeSource: string,
): number;
export function validateResourceManifest(resourceManifest: unknown): number;
export function discoverRedisSourceSymbols(redisSource: string): string[];
export function validateRedisPrimitiveManifest(
  manifest: unknown,
  context: {
    redisSource: string;
    directCallerPaths?: string[];
    workerSources?: Array<{ path: string; source: string }>;
  },
): number;
export function discoverVectorNamespaces(sourceTexts: string[]): string[];
export function validateVectorNamespaceManifest(
  manifest: unknown,
  sourceTexts: string[],
): number;
export function discoverR2LegacyEnvs(storageSource: string): string[];
export function validateR2NamespaceManifest(
  manifest: unknown,
  storageSource: string,
): number;
export function validateManifests(): Promise<ManifestValidationCounts>;
