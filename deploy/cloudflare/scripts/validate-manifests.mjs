import { readFile, readdir } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import YAML from "yaml";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(root, "../..");

const REQUIRED_ROUTE_FIELDS = [
  "method",
  "path",
  "owner",
  "runtime",
  "target_runtime",
  "auth_authority",
  "protocol",
];
const ALLOWED_TARGET_RUNTIMES = new Set([
  "typescript-worker",
  "python-worker",
  "realtime-do",
  "external-api",
  "legacy",
  "blocked",
]);
const ALLOWED_PRIMITIVE_TARGETS = new Set([
  "kv",
  "durable-object",
  "d1",
  "queue",
  "workflow",
  "none",
]);
const ALLOWED_PRIMITIVE_STATES = new Set([
  "planned",
  "staging-partial",
  "staging-owned",
  "blocked",
  "retired",
  "exempt-tooling",
]);
const ALLOWED_VECTOR_STATES = new Set([
  "qualification-required",
  "shadowing",
  "staging-owned",
  "production-owned",
  "blocked",
]);
const ALLOWED_R2_STATES = new Set([
  "inventory-only",
  "copying",
  "staging-owned",
  "production-owned",
  "blocked",
]);
const SKIPPED_SOURCE_DIRECTORIES = new Set([
  "node_modules",
  ".venv",
  ".venv-workers",
  "python_modules",
  "__pycache__",
  ".wrangler",
]);

function requiredString(value, message) {
  if (typeof value !== "string" || value.length === 0) throw new Error(message);
}

function requiredStringArray(value, message) {
  if (
    !Array.isArray(value) ||
    value.length === 0 ||
    value.some((item) => typeof item !== "string" || !item)
  ) {
    throw new Error(message);
  }
}

function assertUnique(items, label) {
  const seen = new Set();
  for (const item of items) {
    if (seen.has(item)) throw new Error(`duplicate ${label}: ${item}`);
    seen.add(item);
  }
}

export function validateRouteManifest(routeManifest, edgeSource) {
  const routes = routeManifest?.routes;
  if (!Array.isArray(routes) || routes.length === 0)
    throw new Error("routes.yaml must contain at least one route");

  const duplicateKeys = new Set();
  for (const route of routes) {
    for (const field of REQUIRED_ROUTE_FIELDS) {
      requiredString(
        route[field],
        `route is missing required string field: ${field}`,
      );
    }
    const key = `${route.method} ${route.path}`;
    if (duplicateKeys.has(key)) throw new Error(`duplicate route: ${key}`);
    duplicateKeys.add(key);
    if (route.path === "/v1/*" && route.target_runtime !== "legacy") {
      throw new Error(
        "broad /v1/* ownership is forbidden; add each migrated route explicitly",
      );
    }
    if (!ALLOWED_TARGET_RUNTIMES.has(route.target_runtime)) {
      throw new Error(
        `unsupported target_runtime for ${key}: ${route.target_runtime}`,
      );
    }
    const dependencies = Array.isArray(route.dependencies)
      ? route.dependencies
      : [];
    const dependsOnRateLimit = dependencies.includes("rate-limit-do");
    if (route.rate_limit_policy !== undefined) {
      requiredString(
        route.rate_limit_policy,
        `rate-limited route ${key} must declare rate_limit_policy`,
      );
      if (!dependsOnRateLimit) {
        throw new Error(
          `route ${key} declares rate_limit_policy without rate-limit-do dependency`,
        );
      }
      if (route.auth_authority === "public") {
        throw new Error(`public route ${key} may not use a UID rate limit`);
      }
    } else if (dependsOnRateLimit && route.auth_authority !== "public") {
      throw new Error(
        `rate-limited route ${key} must declare rate_limit_policy`,
      );
    }
    const routeHint = route.path === "/*" ? "/" : route.path.replace(/\*$/, "");
    const edgeRouteHint = routeHint.replace(/\{([^}]+)\}/g, ":$1");
    const pathSegments = route.path.split("/").filter(Boolean);
    const edgeHints = [edgeRouteHint];
    if (!route.path.endsWith("*") && pathSegments.length > 1) {
      edgeHints.push(`/${pathSegments.slice(0, -1).join("/")}/`);
    }
    if (!edgeHints.some((hint) => edgeSource.includes(hint))) {
      throw new Error(`route is not represented in edge routing code: ${key}`);
    }
  }
  return routes.length;
}

export function validateResourceManifest(resourceManifest) {
  const resources = resourceManifest?.resources;
  if (!Array.isArray(resources) || resources.length === 0)
    throw new Error("resources.yaml must contain resources");
  for (const resource of resources) {
    if (
      !resource.kind ||
      !resource.name ||
      resource.environment !== "staging"
    ) {
      throw new Error(
        `resource must have kind/name and staging environment: ${JSON.stringify(resource)}`,
      );
    }
    if (!resource.name.startsWith("omi-cf-")) {
      throw new Error(
        `resource is outside the Cloudflare namespace: ${resource.name}`,
      );
    }
  }
  return resources.length;
}

export function discoverRedisSourceSymbols(redisSource) {
  return [...redisSource.matchAll(/^def ([A-Za-z][A-Za-z0-9_]*)\(/gm)].map(
    (match) => match[1],
  );
}

export function validateRedisPrimitiveManifest(
  manifest,
  {
    redisSource,
    directCallerPaths = [],
    workerSources = [],
    routeManifest,
  },
) {
  if (manifest?.policy?.workers_may_connect_to_redis !== false) {
    throw new Error(
      "redis-primitives.yaml must forbid Workers from connecting to Redis",
    );
  }
  const families = manifest?.families;
  if (!Array.isArray(families) || families.length === 0) {
    throw new Error("redis-primitives.yaml must contain key families");
  }
  assertUnique(
    families.map((family) => family.id),
    "Redis key family id",
  );

  const classifiedSymbols = [];
  for (const family of families) {
    requiredString(family.id, "Redis key family is missing id");
    requiredStringArray(
      family.source_symbols,
      `Redis family ${family.id} must list source_symbols`,
    );
    requiredStringArray(
      family.key_patterns,
      `Redis family ${family.id} must list key_patterns`,
    );
    requiredStringArray(
      family.legacy_features,
      `Redis family ${family.id} must list legacy_features`,
    );
    requiredString(
      family.consistency,
      `Redis family ${family.id} is missing consistency`,
    );
    requiredString(
      family.target_owner,
      `Redis family ${family.id} is missing target_owner`,
    );
    if (!ALLOWED_PRIMITIVE_TARGETS.has(family.target)) {
      throw new Error(
        `Redis family ${family.id} has unsupported target: ${family.target}`,
      );
    }
    if (!ALLOWED_PRIMITIVE_STATES.has(family.migration_state)) {
      throw new Error(
        `Redis family ${family.id} has unsupported migration_state: ${family.migration_state}`,
      );
    }
    if (
      family.target === "kv" &&
      !new Set(["stale-ok", "bounded-stale"]).has(family.consistency)
    ) {
      throw new Error(
        `Redis family ${family.id} may target KV only with stale-tolerant consistency`,
      );
    }
    if (
      family.target === "none" &&
      family.migration_state !== "exempt-tooling"
    ) {
      throw new Error(
        `Redis family ${family.id} may target none only as exempt tooling`,
      );
    }
    if (family.migration_state === "staging-partial") {
      requiredStringArray(
        family.migrated_policies,
        `Redis family ${family.id} must list migrated_policies while staging-partial`,
      );
      requiredStringArray(
        family.migrated_routes,
        `Redis family ${family.id} must list migrated_routes while staging-partial`,
      );
    }
    classifiedSymbols.push(...family.source_symbols);
  }
  assertUnique(classifiedSymbols, "Redis source symbol classification");

  const ignored = manifest.ignored_source_symbols || [];
  requiredStringArray(
    ignored,
    "redis-primitives.yaml must explicitly list ignored_source_symbols",
  );
  const discoveredSymbols = discoverRedisSourceSymbols(redisSource);
  const missingSymbols = discoveredSymbols.filter(
    (symbol) =>
      !classifiedSymbols.includes(symbol) && !ignored.includes(symbol),
  );
  const staleSymbols = classifiedSymbols.filter(
    (symbol) => !discoveredSymbols.includes(symbol),
  );
  if (missingSymbols.length > 0) {
    throw new Error(
      `unclassified Redis source symbols: ${missingSymbols.join(", ")}`,
    );
  }
  if (staleSymbols.length > 0) {
    throw new Error(
      `stale Redis source symbols in manifest: ${staleSymbols.join(", ")}`,
    );
  }

  const callers = manifest.direct_legacy_callers;
  if (!Array.isArray(callers))
    throw new Error("redis-primitives.yaml must contain direct_legacy_callers");
  assertUnique(
    callers.map((caller) => caller.path),
    "direct Redis caller path",
  );
  for (const caller of callers) {
    requiredString(caller.path, "direct Redis caller is missing path");
    requiredString(
      caller.target_owner,
      `direct Redis caller ${caller.path} is missing target_owner`,
    );
    if (!ALLOWED_PRIMITIVE_TARGETS.has(caller.target)) {
      throw new Error(
        `direct Redis caller ${caller.path} has unsupported target: ${caller.target}`,
      );
    }
    if (!ALLOWED_PRIMITIVE_STATES.has(caller.migration_state)) {
      throw new Error(
        `direct Redis caller ${caller.path} has unsupported migration_state: ${caller.migration_state}`,
      );
    }
  }
  const declaredCallers = callers.map((caller) => caller.path).sort();
  const discoveredCallers = [...directCallerPaths].sort();
  const missingCallers = discoveredCallers.filter(
    (path) => !declaredCallers.includes(path),
  );
  const staleCallers = declaredCallers.filter(
    (path) => !discoveredCallers.includes(path),
  );
  if (missingCallers.length > 0)
    throw new Error(
      `unclassified direct Redis callers: ${missingCallers.join(", ")}`,
    );
  if (staleCallers.length > 0)
    throw new Error(
      `stale direct Redis callers in manifest: ${staleCallers.join(", ")}`,
    );

  const redisWorkerSources = workerSources.filter(({ source }) =>
    /(^|\n)\s*(?:import\s+redis\b|from\s+redis\b)|redis:\/\/|REDIS_DB_/m.test(
      source,
    ),
  );
  if (redisWorkerSources.length > 0) {
    throw new Error(
      `Cloudflare Worker source must not connect to Redis: ${redisWorkerSources.map(({ path }) => path).join(", ")}`,
    );
  }
  if (routeManifest) {
    const requestRateLimits = families.find(
      (family) => family.id === "request-rate-limits",
    );
    if (!requestRateLimits) {
      throw new Error("Redis manifest must classify request-rate-limits");
    }
    const rateLimitedRoutes = routeManifest.routes.filter(
      (route) => route.rate_limit_policy,
    );
    const routeKeys = rateLimitedRoutes
      .map((route) => `${route.method} ${route.path}`)
      .sort();
    const declaredRouteKeys = [...requestRateLimits.migrated_routes].sort();
    if (JSON.stringify(routeKeys) !== JSON.stringify(declaredRouteKeys)) {
      throw new Error(
        "request-rate-limits migrated_routes must equal routes.yaml rate_limit_policy entries",
      );
    }
    const routePolicies = [
      ...new Set(rateLimitedRoutes.map((route) => route.rate_limit_policy)),
    ].sort();
    const declaredPolicies = [...requestRateLimits.migrated_policies].sort();
    if (JSON.stringify(routePolicies) !== JSON.stringify(declaredPolicies)) {
      throw new Error(
        "request-rate-limits migrated_policies must equal routes.yaml rate_limit_policy values",
      );
    }
  }
  return families.length;
}

export function discoverVectorNamespaces(sourceTexts) {
  const namespaces = new Set();
  for (const source of sourceTexts) {
    for (const match of source.matchAll(
      /\b[A-Z][A-Z0-9_]*NAMESPACE[A-Z0-9_]*\s*=\s*["']([^"']+)["']/g,
    )) {
      namespaces.add(match[1]);
    }
    for (const match of source.matchAll(/namespace\s*=\s*["']([^"']+)["']/g))
      namespaces.add(match[1]);
  }
  return [...namespaces].sort();
}

export function validateVectorNamespaceManifest(manifest, sourceTexts) {
  if (manifest?.policy?.vectorize_is_authoritative !== false) {
    throw new Error(
      "vector-namespaces.yaml must keep Vectorize non-authoritative",
    );
  }
  const maxDimensions = manifest?.policy?.max_vectorize_dimensions;
  if (!Number.isInteger(maxDimensions) || maxDimensions <= 0) {
    throw new Error(
      "vector-namespaces.yaml must define max_vectorize_dimensions",
    );
  }
  const namespaces = manifest?.namespaces;
  if (!Array.isArray(namespaces) || namespaces.length === 0) {
    throw new Error("vector-namespaces.yaml must contain namespaces");
  }
  assertUnique(
    namespaces.map((namespace) => namespace.id),
    "vector namespace id",
  );
  assertUnique(
    namespaces.map((namespace) => namespace.legacy_namespace),
    "legacy vector namespace",
  );
  for (const namespace of namespaces) {
    requiredString(namespace.id, "vector namespace is missing id");
    requiredString(
      namespace.legacy_namespace,
      `vector namespace ${namespace.id} is missing legacy_namespace`,
    );
    requiredString(
      namespace.authoritative_source,
      `vector namespace ${namespace.id} is missing authoritative_source`,
    );
    requiredString(
      namespace.source_model,
      `vector namespace ${namespace.id} is missing source_model`,
    );
    requiredString(
      namespace.target_index,
      `vector namespace ${namespace.id} is missing target_index`,
    );
    requiredString(
      namespace.target_model,
      `vector namespace ${namespace.id} is missing target_model`,
    );
    if (namespace.target !== "vectorize")
      throw new Error(`vector namespace ${namespace.id} must target Vectorize`);
    if (namespace.candidate_only !== true) {
      throw new Error(
        `vector namespace ${namespace.id} must be candidate_only`,
      );
    }
    if (
      !Number.isInteger(namespace.source_dimensions) ||
      !Number.isInteger(namespace.target_dimensions)
    ) {
      throw new Error(
        `vector namespace ${namespace.id} must declare integer dimensions`,
      );
    }
    if (namespace.target_dimensions > maxDimensions) {
      throw new Error(
        `vector namespace ${namespace.id} exceeds Vectorize dimension limit`,
      );
    }
    if (
      namespace.source_dimensions !== namespace.target_dimensions &&
      namespace.migration_mode !== "reembed"
    ) {
      throw new Error(
        `vector namespace ${namespace.id} must reembed when dimensions change`,
      );
    }
    if (!ALLOWED_VECTOR_STATES.has(namespace.migration_state)) {
      throw new Error(
        `vector namespace ${namespace.id} has unsupported migration_state: ${namespace.migration_state}`,
      );
    }
  }
  const declared = namespaces
    .map((namespace) => namespace.legacy_namespace)
    .sort();
  const discovered = discoverVectorNamespaces(sourceTexts);
  const missing = discovered.filter(
    (namespace) => !declared.includes(namespace),
  );
  const stale = declared.filter((namespace) => !discovered.includes(namespace));
  if (missing.length > 0)
    throw new Error(`unclassified vector namespaces: ${missing.join(", ")}`);
  if (stale.length > 0)
    throw new Error(`stale vector namespaces in manifest: ${stale.join(", ")}`);
  return namespaces.length;
}

export function discoverR2LegacyEnvs(storageSource) {
  return [
    ...storageSource.matchAll(/os\.getenv\(\s*["'](BUCKET_[A-Z0-9_]+)["']/g),
  ]
    .map((match) => match[1])
    .sort();
}

export function validateR2NamespaceManifest(manifest, storageSource) {
  if (manifest?.policy?.dual_write_allowed !== false) {
    throw new Error("r2-namespaces.yaml must forbid dual write");
  }
  const namespaces = manifest?.namespaces;
  if (!Array.isArray(namespaces) || namespaces.length === 0) {
    throw new Error("r2-namespaces.yaml must contain namespaces");
  }
  assertUnique(
    namespaces.map((namespace) => namespace.id),
    "R2 namespace id",
  );
  assertUnique(
    namespaces.map((namespace) => namespace.legacy_env),
    "R2 legacy bucket env",
  );
  assertUnique(
    namespaces.map((namespace) => namespace.target_binding),
    "R2 target binding",
  );
  for (const namespace of namespaces) {
    requiredString(namespace.id, "R2 namespace is missing id");
    requiredString(
      namespace.legacy_env,
      `R2 namespace ${namespace.id} is missing legacy_env`,
    );
    requiredString(
      namespace.target_binding,
      `R2 namespace ${namespace.id} is missing target_binding`,
    );
    requiredString(
      namespace.target_bucket_pattern,
      `R2 namespace ${namespace.id} is missing target_bucket_pattern`,
    );
    requiredStringArray(
      namespace.object_patterns,
      `R2 namespace ${namespace.id} must list object_patterns`,
    );
    requiredString(
      namespace.data_classification,
      `R2 namespace ${namespace.id} is missing data_classification`,
    );
    requiredString(
      namespace.lifecycle,
      `R2 namespace ${namespace.id} is missing lifecycle`,
    );
    if (
      !namespace.target_bucket_pattern.startsWith("omi-cf-") ||
      !namespace.target_bucket_pattern.includes("{environment}")
    ) {
      throw new Error(
        `R2 namespace ${namespace.id} must use an isolated environment bucket pattern`,
      );
    }
    if (!ALLOWED_R2_STATES.has(namespace.migration_state)) {
      throw new Error(
        `R2 namespace ${namespace.id} has unsupported migration_state: ${namespace.migration_state}`,
      );
    }
  }
  const declared = namespaces.map((namespace) => namespace.legacy_env).sort();
  const discovered = discoverR2LegacyEnvs(storageSource);
  const missing = discovered.filter((name) => !declared.includes(name));
  const stale = declared.filter((name) => !discovered.includes(name));
  if (missing.length > 0)
    throw new Error(
      `unclassified object-storage bucket envs: ${missing.join(", ")}`,
    );
  if (stale.length > 0)
    throw new Error(
      `stale object-storage bucket envs in manifest: ${stale.join(", ")}`,
    );
  return namespaces.length;
}

async function walkFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    if (entry.isDirectory() && SKIPPED_SOURCE_DIRECTORIES.has(entry.name)) {
      continue;
    }
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await walkFiles(path)));
    else files.push(path);
  }
  return files;
}

async function loadYaml(path) {
  return YAML.parse(await readFile(path, "utf8"));
}

async function discoverDirectRedisCallers() {
  const backendRoot = resolve(repoRoot, "backend");
  const files = (await walkFiles(backendRoot)).filter((path) => {
    const repoPath = relative(repoRoot, path).replaceAll("\\", "/");
    return (
      path.endsWith(".py") &&
      repoPath !== "backend/database/redis_db.py" &&
      !repoPath.startsWith("backend/tests/") &&
      !repoPath.startsWith("backend/testing/")
    );
  });
  const callers = [];
  for (const path of files) {
    const source = await readFile(path, "utf8");
    const importsDirectClient =
      /from database\.redis_db import r(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?\b/.test(
        source,
      ) ||
      /from database\.redis_db import \([\s\S]*?\br(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?\s*(?:,|\))/.test(
        source,
      );
    const importsRedisPackage =
      /(^|\n)\s*import\s+redis(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?\s*($|\n)/m.test(
        source,
      ) ||
      /(^|\n)\s*from\s+redis(?:\.[A-Za-z_][A-Za-z0-9_]*)?\s+import\s+/m.test(
        source,
      );
    if (
      importsDirectClient ||
      importsRedisPackage ||
      /\bredis_db\.r\b/.test(source)
    ) {
      callers.push(relative(repoRoot, path).replaceAll("\\", "/"));
    }
  }
  return callers.sort();
}

async function loadWorkerSources() {
  const roots = [resolve(root, "workers"), resolve(root, "python")];
  const sources = [];
  for (const sourceRoot of roots) {
    for (const path of await walkFiles(sourceRoot)) {
      if (!path.endsWith(".ts") && !path.endsWith(".py")) continue;
      sources.push({
        path: relative(repoRoot, path).replaceAll("\\", "/"),
        source: await readFile(path, "utf8"),
      });
    }
  }
  return sources;
}

export async function validateManifests() {
  const [
    routeManifest,
    resourceManifest,
    redisManifest,
    vectorManifest,
    r2Manifest,
    edgeSource,
    redisSource,
    storageSource,
  ] = await Promise.all([
    loadYaml(resolve(root, "manifests/routes.yaml")),
    loadYaml(resolve(root, "manifests/resources.yaml")),
    loadYaml(resolve(root, "manifests/redis-primitives.yaml")),
    loadYaml(resolve(root, "manifests/vector-namespaces.yaml")),
    loadYaml(resolve(root, "manifests/r2-namespaces.yaml")),
    readFile(resolve(root, "workers/edge/index.ts"), "utf8"),
    readFile(resolve(repoRoot, "backend/database/redis_db.py"), "utf8"),
    readFile(resolve(repoRoot, "backend/utils/other/storage.py"), "utf8"),
  ]);
  const vectorSources = await Promise.all(
    vectorManifest.sources.map((path) =>
      readFile(resolve(repoRoot, path), "utf8"),
    ),
  );
  const [directCallerPaths, workerSources] = await Promise.all([
    discoverDirectRedisCallers(),
    loadWorkerSources(),
  ]);

  const counts = {
    routes: validateRouteManifest(routeManifest, edgeSource),
    resources: validateResourceManifest(resourceManifest),
    redisFamilies: validateRedisPrimitiveManifest(redisManifest, {
      redisSource,
      directCallerPaths,
      workerSources,
      routeManifest,
    }),
    vectorNamespaces: validateVectorNamespaceManifest(
      vectorManifest,
      vectorSources,
    ),
    r2Namespaces: validateR2NamespaceManifest(r2Manifest, storageSource),
  };
  console.log(
    `Manifest validation passed: ${counts.routes} routes, ${counts.resources} staging resources, ` +
      `${counts.redisFamilies} Redis families, ${counts.vectorNamespaces} vector namespaces, ` +
      `${counts.r2Namespaces} R2 namespaces.`,
  );
  return counts;
}

if (resolve(process.argv[1] || "") === fileURLToPath(import.meta.url)) {
  await validateManifests();
}
