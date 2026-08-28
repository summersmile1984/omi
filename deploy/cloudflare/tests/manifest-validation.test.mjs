import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import YAML from "yaml";
import {
  validateManifests,
  validateR2NamespaceManifest,
  validateRedisPrimitiveManifest,
  validateVectorNamespaceManifest,
} from "../scripts/validate-manifests.mjs";

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

    await expect(validateManifests()).resolves.toEqual({
      routes: routes.routes.length,
      resources: resources.resources.length,
      redisFamilies: redis.families.length,
      vectorNamespaces: vector.namespaces.length,
      r2Namespaces: r2.namespaces.length,
    });
  });

  it("fails when a Redis source symbol or a direct Worker Redis dependency is introduced", async () => {
    const manifest = await loadYaml("redis-primitives.yaml");
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
