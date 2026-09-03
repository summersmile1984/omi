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
    expect(
      redis.families.find((family) => family.id === "speech-profile-duration"),
    ).toMatchObject({
      target: "r2-object-metadata",
      target_owner: "api-core",
      migration_state: "staging-owned",
    });
    expect(
      r2.namespaces.find((namespace) => namespace.id === "speech-profiles"),
    ).toMatchObject({
      target_binding: "SPEECH_PROFILES",
      migration_state: "staging-owned",
      data_classification: "biometric-sensitive",
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
    expect(counts.legacyOwned).toBe(0);

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

  it("assigns the closed D1/provider chat compatibility group to the Jobs owner", async () => {
    const [routes, backendRoutes] = await Promise.all([
      loadYaml("routes.yaml"),
      readFile(
        resolve(cloudflareRoot, "manifests/backend-routes.json"),
        "utf8",
      ).then(JSON.parse),
    ]);
    const legacyChatPaths = [
      "/v1/chat/materialize-prompts",
      "/v2/chat/materialize-prompts",
      "/v2/chat/completions",
    ];
    const inventoryByPath = new Map(
      backendRoutes.routes.map((route) => [route.path, route]),
    );

    for (const path of legacyChatPaths) {
      const inventory = inventoryByPath.get(path);
      expect(inventory, path).toMatchObject({
        migration_state: "staging-owned",
        owner: "jobs",
        target_runtime: "typescript-worker",
      });
      expect(
        routes.routes.some(
          (route) => route.method === "POST" && route.path === path,
        ),
        `${path} must have a Cloudflare owner route`,
      ).toBe(true);
    }
  });

  it("does not make OpenAI a dependency of canonical Workers AI routes", async () => {
    const routes = await loadYaml("routes.yaml");
    const nativePaths = new Set([
      "POST /v2/messages",
      "POST /v1/cf/chat-files",
      "POST /v1/files",
      "POST /v2/files",
      "DELETE /v1/cf/chat-files/:fileId",
      "DELETE /v2/cf/chat-sessions/:sessionId/assistant",
      "POST /v2/cf/messages/attachments",
      "GET /v2/cf/chat-sessions/:sessionId/assistant-runs/:runId",
      "POST /v2/cf/chat-sessions/:sessionId/assistant-runs",
      "POST /v2/cf/chat/completions",
      "POST /v2/chat/completions",
    ]);
    for (const route of routes.routes) {
      const key = `${route.method} ${route.path}`;
      if (!nativePaths.has(key)) continue;
      expect(route.dependencies || [], key).not.toContain("external-openai-api");
    }
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

  it("allows target none only for exempt tooling or retired Redis families", async () => {
    const manifest = await loadYaml("redis-primitives.yaml");
    const retired = manifest.families.find(
      (family) => family.migration_state === "retired",
    );
    expect(retired?.target).toBe("none");

    const invalid = structuredClone(manifest);
    invalid.families.find(
      (family) => family.migration_state === "retired",
    ).migration_state = "planned";
    expect(() =>
      validateRedisPrimitiveManifest(invalid, {
        redisSource: null,
        directCallerPaths: null,
        workerSources: [],
        routeManifest: null,
      }),
    ).toThrow("may target none only as exempt tooling or retired");
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

  it("requires shared-binding R2 namespaces to declare disjoint key prefixes", async () => {
    const manifest = await loadYaml("r2-namespaces.yaml");
    const storageSource = await readFile(
      resolve(repoRoot, manifest.source),
      "utf8",
    );

    const missingPrefixes = structuredClone(manifest);
    const temporalSync = missingPrefixes.namespaces.find(
      (namespace) => namespace.id === "temporal-sync",
    );
    delete temporalSync.target_key_prefixes;
    expect(() =>
      validateR2NamespaceManifest(missingPrefixes, storageSource),
    ).toThrow("must declare target_key_prefixes");

    const overlapping = structuredClone(manifest);
    const overlappingThumbnails = overlapping.namespaces.find(
      (namespace) => namespace.id === "app-thumbnails",
    );
    overlappingThumbnails.target_key_prefixes = ["cf-app-logos/"];
    overlappingThumbnails.object_patterns = [
      "cf-app-logos/{thumbnail_id}.jpg",
    ];
    expect(() =>
      validateR2NamespaceManifest(overlapping, storageSource),
    ).toThrow("overlapping key prefixes");

    const strayPattern = structuredClone(manifest);
    strayPattern.namespaces
      .find((namespace) => namespace.id === "app-logos")
      .object_patterns.push("logos/{app_id}.png");
    expect(() =>
      validateR2NamespaceManifest(strayPattern, storageSource),
    ).toThrow("outside its declared key prefixes");
  });

  it("rejects active R2 namespaces without a provisioned bucket or declared binding", async () => {
    const [manifest, resources] = await Promise.all([
      loadYaml("r2-namespaces.yaml"),
      loadYaml("resources.yaml"),
    ]);
    const storageSource = await readFile(
      resolve(repoRoot, manifest.source),
      "utf8",
    );

    const unprovisioned = structuredClone(resources);
    unprovisioned.resources = unprovisioned.resources.filter(
      (resource) => resource.name !== "omi-cf-speech-profiles-staging",
    );
    expect(() =>
      validateR2NamespaceManifest(manifest, storageSource, {
        resourceManifest: unprovisioned,
      }),
    ).toThrow("speech-profiles is active without a provisioned resource");

    expect(() =>
      validateR2NamespaceManifest(manifest, storageSource, {
        wranglerSources: ['"r2_buckets": [\n  { "binding": "ASSETS" }\n  ]'],
      }),
    ).toThrow("is not declared in any wrangler config");

    expect(() =>
      validateR2NamespaceManifest(manifest, storageSource, {
        resourceManifest: resources,
      }),
    ).not.toThrow();
  });
});
