import { cpSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { digest, WORKERS } from "../scripts/resource-input.mjs";
import { fileTree, readJson } from "../scripts/release-files.mjs";

// Only a verified release context may supply this projection. Application code,
// assets and Python dependencies are copied verbatim; only local bindings change.
export function copyFrozenTarget(context, output) {
  context.verify();
  if (
    digest(Object.keys(context.candidate.workers).sort()) !==
    digest([...WORKERS].sort())
  )
    throw new Error("frozen local target requires all eight Worker owners");
  const templates = {};
  for (const role of WORKERS) {
    const owner = context.candidate.workers[role];
    if (
      owner.artifact !== `workers/${role}` ||
      owner.config !== `workers/${role}/wrangler.json`
    )
      throw new Error("unexpected frozen Worker artifact owner");
    const source = resolve(context.directory, owner.artifact);
    const bundle = resolve(output, owner.artifact);
    cpSync(source, bundle, {
      recursive: true,
      errorOnExist: true,
      force: false,
    });
    const path = resolve(bundle, "wrangler.json"),
      config = readJson(path);
    if (
      config.name !== owner.name ||
      config.no_bundle !== true ||
      !config.find_additional_modules
    )
      throw new Error("frozen Worker configuration is not an upload artifact");
    templates[role] = { path, config };
  }
  const migrationsRoot = resolve(output, "sql");
  mkdirSync(migrationsRoot);
  for (const authority of ["auth", "app"])
    cpSync(
      resolve(context.directory, "sql", authority),
      resolve(migrationsRoot, authority),
      {
        recursive: true,
        errorOnExist: true,
        force: false,
      },
    );
  const verifyPayload = () => {
    context.verify();
    const subtree = (root, prefix) =>
      Object.fromEntries(
        Object.entries(context.candidate.artifact_files[root])
          .filter(([path]) => path.startsWith(`${prefix}/`))
          .map(([path, hash]) => [path.slice(prefix.length + 1), hash]),
      );
    for (const role of WORKERS) {
      const payload = (files) =>
        Object.fromEntries(
          Object.entries(files).filter(
            ([path]) => path !== "wrangler.json" && path !== ".dev.vars",
          ),
        );
      if (
        digest(payload(fileTree(resolve(output, "workers", role)))) !==
        digest(payload(subtree("workers", role)))
      )
        throw new Error(`local frozen payload changed: ${role}`);
    }
    for (const authority of ["auth", "app"])
      if (
        digest(fileTree(resolve(migrationsRoot, authority))) !==
        digest(subtree("sql", authority))
      )
        throw new Error(`local frozen SQL changed: ${authority}`);
  };
  verifyPayload();
  const core = templates["api-core"].config.vars;
  const brandRuntime = JSON.parse(core.BRAND_RUNTIME_JSON);
  if (
    digest(JSON.parse(templates["api-ai"].config.vars.BRAND_RUNTIME_JSON)) !==
    digest(brandRuntime)
  )
    throw new Error("frozen API brand owners disagree");
  return {
    templates,
    migrationsRoot,
    verifyPayload,
    brandId: context.candidate.brand,
    brandRuntime,
    firmwarePolicy: JSON.parse(core.FIRMWARE_BRAND_POLICY_JSON),
    supportEmail: core.BRAND_SUPPORT_EMAIL,
  };
}
