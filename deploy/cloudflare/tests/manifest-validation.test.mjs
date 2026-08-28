import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import YAML from "yaml";
import {
  validateManifests,
  validateBackendRouteInventory,
  validateR2NamespaceManifest,
  validateRedisPrimitiveManifest,
  validateRouteManifest,
  validateVectorNamespaceManifest,
} from "../scripts/validate-manifests.mjs";
import {
  EDGE_RATE_LIMIT_POLICIES,
  edgeRateLimitPolicyForRequest,
} from "../workers/edge/rate-limit";
import { TTS_FINE_RATE_LIMIT } from "../workers/rate-limit/index";

const cloudflareRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(cloudflareRoot, "../..");

async function loadYaml(name) {
  return YAML.parse(
    await readFile(resolve(cloudflareRoot, "manifests", name), "utf8"),
  );
}

describe("Cloudflare migration manifests", () => {
  it("classifies every current Redis primitive, vector namespace, and object bucket", async () => {
    const [routes, resources, redis, vector, r2] = await Promise.all([
      loadYaml("routes.yaml"),
      loadYaml("resources.yaml"),
      loadYaml("redis-primitives.yaml"),
      loadYaml("vector-namespaces.yaml"),
      loadYaml("r2-namespaces.yaml"),
    ]);
    const backendRoutes = JSON.parse(
      await readFile(
        resolve(cloudflareRoot, "manifests/backend-routes.json"),
        "utf8",
      ),
    );

    await expect(validateManifests()).resolves.toEqual({
      routes: routes.routes.length,
      backendRoutes: backendRoutes.routes.length,
      legacyBackendRoutes: backendRoutes.routes.filter(
        (route) => route.migration_state === "legacy-owned",
      ).length,
      resources: resources.resources.length,
      redisFamilies: redis.families.length,
      vectorNamespaces: vector.namespaces.length,
      r2Namespaces: r2.namespaces.length,
    });
  });

  it("requires every registered backend route to retain an explicit migration owner", async () => {
    const [routes, backendRoutes] = await Promise.all([
      loadYaml("routes.yaml"),
      readFile(
        resolve(cloudflareRoot, "manifests/backend-routes.json"),
        "utf8",
      ).then(JSON.parse),
    ]);

    const counts = validateBackendRouteInventory(backendRoutes, routes);
    expect(counts.total).toBe(backendRoutes.routes.length);
    expect(counts.stagingOwned).toBeGreaterThan(0);
    expect(counts.legacyOwned).toBeGreaterThan(0);

    const unclassified = structuredClone(backendRoutes);
    unclassified.routes[0].migration_state = "unclassified";
    expect(() => validateBackendRouteInventory(unclassified, routes)).toThrow(
      "unsupported migration_state",
    );

    const stale = structuredClone(backendRoutes);
    const owned = stale.routes.find(
      (route) => route.migration_state === "staging-owned",
    );
    owned.migration_state = "legacy-owned";
    owned.owner = "legacy";
    owned.target_runtime = "legacy";
    expect(() => validateBackendRouteInventory(stale, routes)).toThrow(
      "already owned in routes.yaml",
    );
  });

  it("projects prefix-owned Worker routes into the backend inventory", () => {
    const routeManifest = {
      routes: [
        {
          method: "ANY",
          path: "/v1/users/developer/webhook/*",
          owner: "api-core",
          target_runtime: "python-worker",
          protocol: "http",
        },
      ],
    };
    const inventory = {
      version: 1,
      source: "backend/main.py",
      routes: [
        {
          method: "POST",
          path: "/v1/users/developer/webhook/{wtype}/enable",
          protocol: "http",
          migration_state: "staging-owned",
          owner: "api-core",
          target_runtime: "python-worker",
        },
      ],
    };

    expect(validateBackendRouteInventory(inventory, routeManifest)).toEqual({
      total: 1,
      stagingOwned: 1,
      legacyOwned: 0,
      blocked: 0,
    });

    inventory.routes[0].migration_state = "legacy-owned";
    inventory.routes[0].owner = "legacy";
    inventory.routes[0].target_runtime = "legacy";
    expect(() =>
      validateBackendRouteInventory(inventory, routeManifest),
    ).toThrow("already owned in routes.yaml");
  });

  it("fails when a Redis source symbol or a direct Worker Redis dependency is introduced", async () => {
    const [manifest, routes] = await Promise.all([
      loadYaml("redis-primitives.yaml"),
      loadYaml("routes.yaml"),
    ]);
    const redisSource = await readFile(
      resolve(repoRoot, manifest.source),
      "utf8",
    );
    const directCallerPaths = manifest.direct_legacy_callers.map(
      (caller) => caller.path,
    );
    const missingClassification = structuredClone(manifest);
    missingClassification.families[0].source_symbols =
      missingClassification.families[0].source_symbols.slice(1);

    expect(() =>
      validateRedisPrimitiveManifest(missingClassification, {
        redisSource,
        directCallerPaths,
      }),
    ).toThrow("unclassified Redis source symbols");
    expect(() =>
      validateRedisPrimitiveManifest(manifest, {
        redisSource,
        directCallerPaths,
        workerSources: [
          {
            path: "workers/bad.ts",
            source: 'const endpoint = "redis://example";',
          },
        ],
      }),
    ).toThrow("must not connect to Redis");

    const unsafeKv = structuredClone(manifest);
    unsafeKv.families.find(
      (family) => family.id === "mcp-api-key-cache",
    ).target = "kv";
    expect(() =>
      validateRedisPrimitiveManifest(unsafeKv, {
        redisSource,
        directCallerPaths,
      }),
    ).toThrow("only with stale-tolerant consistency");

    const untrackedPartial = structuredClone(manifest);
    delete untrackedPartial.families.find(
      (family) => family.id === "request-rate-limits",
    ).migrated_policies;
    expect(() =>
      validateRedisPrimitiveManifest(untrackedPartial, {
        redisSource,
        directCallerPaths,
      }),
    ).toThrow("must list migrated_policies while staging-partial");

    const mismatchedRoutes = structuredClone(manifest);
    mismatchedRoutes.families
      .find((family) => family.id === "request-rate-limits")
      .migrated_routes.pop();
    expect(() =>
      validateRedisPrimitiveManifest(mismatchedRoutes, {
        redisSource,
        directCallerPaths,
        routeManifest: routes,
      }),
    ).toThrow("migrated_routes must equal routes.yaml");
  });

  it("keeps rate-limit dependencies, policy declarations, and Edge matching aligned", async () => {
    const routes = await loadYaml("routes.yaml");
    const edgeSource = await readFile(
      resolve(cloudflareRoot, "workers/edge/index.ts"),
      "utf8",
    );
    const invalid = structuredClone(routes);
    delete invalid.routes.find((route) => route.path === "/v1/tts/synthesize")
      .rate_limit_policy;
    expect(() => validateRouteManifest(invalid, edgeSource)).toThrow(
      "must declare rate_limit_policy",
    );

    const limitedRoutes = routes.routes.filter(
      (route) => route.rate_limit_policy,
    );
    for (const route of limitedRoutes) {
      const concretePath = route.path.replace(/:[^/]+/g, "sample-id");
      expect(
        edgeRateLimitPolicyForRequest(route.method, concretePath)?.name,
        `${route.method} ${route.path}`,
      ).toBe(route.rate_limit_policy);
    }
    expect(edgeRateLimitPolicyForRequest("GET", "/v3/memories")).toBeNull();
  });

  it("keeps migrated Edge limits equal to the backend policy source", async () => {
    const backendPolicies = await readFile(
      resolve(repoRoot, "backend/utils/rate_limit_config.py"),
      "utf8",
    );
    for (const [name, policy] of Object.entries(EDGE_RATE_LIMIT_POLICIES)) {
      const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const match = backendPolicies.match(
        new RegExp(`["]${escapedName}["]\\s*:\\s*\\((\\d+),\\s*(\\d+)\\)`),
      );
      expect(match, name).not.toBeNull();
      expect(Number(match?.[1]), `${name} max requests`).toBe(
        policy.maxRequests,
      );
      expect(Number(match?.[2]), `${name} window seconds`).toBe(
        policy.windowSeconds,
      );
    }
  });

  it("keeps desktop TTS fine limits and the Python DO binding aligned", async () => {
    const desktopTtsSource = await readFile(
      resolve(repoRoot, "backend/routers/desktop_tts_updates.py"),
      "utf8",
    );
    const constant = (name) => {
      const match = desktopTtsSource.match(
        new RegExp(`^${name} = ([\\d_]+)$`, "m"),
      );
      expect(match, name).not.toBeNull();
      return Number(match?.[1].replaceAll("_", ""));
    };
    expect(TTS_FINE_RATE_LIMIT).toEqual({
      burstRequests: constant("_TTS_BURST_PER_MINUTE"),
      burstWindowSeconds: 60,
      dailyChars: constant("_TTS_DAILY_CHARS"),
      maxRequestChars: constant("_MAX_TTS_CHARS"),
    });

    const [apiAiWrangler, edgeWrangler, rateLimitWrangler] = await Promise.all(
      [
        "python/api-ai/wrangler.jsonc",
        "workers/edge/wrangler.jsonc",
        "workers/rate-limit/wrangler.jsonc",
      ].map(async (path) =>
        YAML.parse(await readFile(resolve(cloudflareRoot, path), "utf8")),
      ),
    );
    const sharedBinding = {
      name: "RATE_LIMITS",
      class_name: "SharedRateLimitDurableObject",
      script_name: "omi-cf-rate-limit-staging",
    };
    expect(apiAiWrangler.durable_objects.bindings).toContainEqual(
      sharedBinding,
    );
    expect(edgeWrangler.durable_objects.bindings).toContainEqual(sharedBinding);
    expect(rateLimitWrangler.durable_objects.bindings).toContainEqual({
      name: "RATE_LIMITS",
      class_name: "SharedRateLimitDurableObject",
    });
    const redis = await loadYaml("redis-primitives.yaml");
    for (const familyId of ["request-rate-limits", "tts-rate-limits"]) {
      expect(
        redis.families.find((family) => family.id === familyId).target_owner,
      ).toBe("rate-limit");
    }
  });

  it("requires re-embedding when a vector projection changes dimensions", async () => {
    const manifest = await loadYaml("vector-namespaces.yaml");
    const invalid = structuredClone(manifest);
    invalid.namespaces[0].migration_mode = "reuse";

    expect(() => validateVectorNamespaceManifest(invalid, [])).toThrow(
      "must reembed when dimensions change",
    );
  });

  it("requires every R2 target bucket to be isolated by environment", async () => {
    const manifest = await loadYaml("r2-namespaces.yaml");
    const storageSource = await readFile(
      resolve(repoRoot, manifest.source),
      "utf8",
    );
    const invalid = structuredClone(manifest);
    invalid.namespaces[0].target_bucket_pattern = "shared-speech-profiles";

    expect(() => validateR2NamespaceManifest(invalid, storageSource)).toThrow(
      "must use an isolated environment bucket pattern",
    );
  });
});
