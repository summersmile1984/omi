import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, resolve } from "node:path";
import {
  assertInstalledRuntime,
  PYTHON_TOOLS,
  runPythonWorker,
} from "./python-worker.mjs";
import { renderResourcePlan } from "./resource-configs.mjs";
import { materializeResourceBundle } from "./resource-bundle.mjs";
import { digest, WORKERS } from "./resource-input.mjs";
import {
  fileTree,
  readJson,
  sourceIdentity,
  writeJson,
} from "./release-files.mjs";

export function runLocal(root, command, args, { input, log, cwd = root } = {}) {
  const result = spawnSync(command, args, {
    cwd,
    input,
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
    env: {
      ...process.env,
      CI: "true",
      WRANGLER_SEND_METRICS: "false",
      NPM_CONFIG_OFFLINE: "true",
    },
  });
  if (log) {
    // Qualification commands do not receive application credentials or responses.
    writeFileSync(log, `${result.stdout ?? ""}\n${result.stderr ?? ""}`);
  }
  if (result.status !== 0)
    throw new Error(
      `local ${basename(command)} ${args[0] ?? ""} failed (exit ${
        result.status ?? "unknown"
      }); see qualification log`,
    );
  return result.stdout;
}

export function qualifyLocal(root, output) {
  const cf = resolve(root, "deploy/cloudflare");
  const commands = [
    ["routes", "npm", ["run", "validate:backend-routes"], cf],
    ["types", "npm", ["run", "typecheck"], cf],
    ["workers", "npm", ["test"], cf],
    [
      "core",
      "uvx",
      ["uv==0.12.3", "run", "pytest", "-q"],
      resolve(cf, "python/api-core"),
    ],
    [
      "ai",
      "uvx",
      ["uv==0.12.3", "run", "pytest", "-q"],
      resolve(cf, "python/api-ai"),
    ],
    ["web-upstream", "bun", ["run", "check"], resolve(root, "web/app")],
    ["web-client", "bash", ["web/app/fork/test.sh"], root],
    ["web-builder", "bash", ["deploy/web/ci.sh"], root],
  ];
  return commands.map(([id, command, args, cwd]) => {
    const log = `logs/${id}.log`;
    runLocal(root, command, args, { cwd, log: resolve(output, log) });
    return {
      id,
      command: [
        command.startsWith(root) ? command.slice(root.length + 1) : command,
        ...args,
      ],
      cwd: cwd.slice(root.length + 1),
      exit: 0,
      log,
      log_sha256: digest(readFileSync(resolve(output, log))),
    };
  });
}

export function dryRunConfig(root, config, output, log) {
  assertInstalledRuntime(resolve(root, "deploy/cloudflare"));
  return runLocal(
    root,
    process.execPath,
    [
      resolve(root, "deploy/cloudflare/node_modules/wrangler/bin/wrangler.js"),
      "deploy",
      "--config",
      config,
      "--dry-run",
      "--no-bundle",
      "--outdir",
      output,
    ],
    { log },
  );
}

export function freezeWorkerConfig(original, role, bundle) {
  const config = structuredClone(original),
    modules = resolve(bundle, "modules");
  const entry = role.startsWith("api-")
    ? "entry.py"
    : basename(config.main).replace(/\.[cm]?tsx?$/, ".js");
  if (!existsSync(resolve(modules, entry)))
    throw new Error(`compiled entry missing for ${role}`);
  if (role.startsWith("api-") && existsSync(resolve(modules, "python_modules")))
    renameSync(
      resolve(modules, "python_modules"),
      resolve(bundle, "python_modules"),
    );
  for (const key of ["$schema", "alias", "tsconfig", "build", "minify"])
    delete config[key];
  config.main = `modules/${entry}`;
  config.base_dir = "modules";
  config.no_bundle = true;
  config.find_additional_modules = true;
  if (config.assets) {
    cpSync(config.assets.directory, resolve(bundle, "assets"), {
      recursive: true,
    });
    config.assets.directory = "assets";
  }
  for (const db of config.d1_databases ?? []) delete db.migrations_dir;
  return config;
}

export function verifyFrozenPayload(bundle, proof) {
  const expected = fileTree(resolve(bundle, "modules"));
  if (existsSync(resolve(bundle, "python_modules")))
    for (const [path, hash] of Object.entries(
      fileTree(resolve(bundle, "python_modules")),
    ))
      expected[`python_modules/${path}`] = hash;
  const actual = fileTree(proof);
  const names = [
    ...new Set([...Object.keys(expected), ...Object.keys(actual)]),
  ];
  // Wrangler's explanatory README and esbuild's root source map accompany a
  // dry-run; neither is an uploaded module with the current no-source-map config.
  for (const name of names.filter(
    (path) =>
      path !== "README.md" && !(path.endsWith(".map") && !path.includes("/")),
  ))
    if (expected[name] !== actual[name])
      throw new Error("frozen upload module differs from the qualified bundle");
  return names.filter(
    (path) =>
      path !== "README.md" && !(path.endsWith(".map") && !path.includes("/")),
  ).length;
}

export function prepareRelease({
  root,
  output,
  inventory,
  brand,
  manifest,
  stage,
}) {
  root = resolve(root);
  output = resolve(output);
  if (!["beta", "production"].includes(stage))
    throw new Error("release stage must be beta or production");
  if (existsSync(output))
    throw new Error(
      "prepare requires a new output directory; candidates are immutable",
    );
  if ((!brand && !manifest) || (brand && manifest))
    throw new Error("select exactly one brand or manifest");
  const input = readJson(resolve(inventory));
  if (input.stage !== stage)
    throw new Error("inventory stage differs from release command");
  const originalSource = sourceIdentity(root),
    cf = resolve(root, "deploy/cloudflare");
  assertInstalledRuntime(cf);
  mkdirSync(resolve(output, "logs"), { recursive: true });
  writeJson(output, "inputs/inventory.json", input);
  const checks = qualifyLocal(root, output);
  if (manifest) {
    cpSync(resolve(manifest), resolve(output, "inputs/manifest.json"));
    const projectedAssets = JSON.parse(
      runLocal(root, resolve(root, "backend/.venv/bin/python"), [
        "deploy/web/profile_input.py",
        "--target",
        "cloudflare",
        "--stage",
        stage,
        "--manifest",
        resolve(output, "inputs/manifest.json"),
      ]),
    );
    projectedAssets.asset_input.root = dirname(resolve(manifest));
    runLocal(
      root,
      process.execPath,
      ["deploy/web/brand-assets.mjs", "--snapshot", resolve(output, "inputs")],
      {
        input: JSON.stringify(projectedAssets),
      },
    );
  }
  const selection = manifest
    ? ["--manifest", resolve(output, "inputs/manifest.json")]
    : ["--brand", brand];
  for (const target of ["self_hosted", "cloudflare"])
    runLocal(
      root,
      "bun",
      [
        "deploy/web/build.ts",
        "--target",
        target,
        "--stage",
        stage,
        ...selection,
        "--output",
        resolve(output, "web", target),
      ],
      { log: resolve(output, `logs/web-${target}.log`) },
    );
  for (const role of ["api-core", "api-ai"])
    runPythonWorker(role, ["sync"], { root: cf });
  const projected = JSON.parse(
    runLocal(root, resolve(root, "backend/.venv/bin/python"), [
      "deploy/web/profile_input.py",
      "--target",
      "cloudflare",
      "--stage",
      stage,
      ...selection,
    ]),
  );
  const web = resolve(output, "web/cloudflare");
  const plan = renderResourcePlan({
    root: cf,
    input,
    projected,
    web: {
      manifest: readJson(resolve(web, "build-manifest.json")),
      config: readJson(resolve(web, "artifact/wrangler.json")),
    },
    sourceCommit: originalSource.commit,
  });
  materializeResourceBundle(plan, {
    root: cf,
    output: resolve(output, "resources"),
    webArtifact: resolve(web, "artifact"),
  });
  runLocal(
    root,
    resolve(root, "backend/.venv/bin/python"),
    ["deploy/cloudflare/scripts/verify-resource-migrations.py", "--root", cf],
    {
      input: JSON.stringify(plan),
      log: resolve(output, "logs/sql-fixtures.log"),
    },
  );
  const workers = {};
  for (const role of WORKERS) {
    const configPath = resolve(
        output,
        `resources/workers/${role}/wrangler.json`,
      ),
      config = readJson(configPath);
    const bundle = resolve(output, `workers/${role}`),
      modules = resolve(bundle, "modules");
    mkdirSync(bundle, { recursive: true });
    const args = [
      "deploy",
      "--config",
      configPath,
      "--dry-run",
      "--outdir",
      modules,
    ];
    if (role.startsWith("api-")) runPythonWorker(role, args, { root: cf });
    else
      runLocal(
        root,
        process.execPath,
        [resolve(cf, "node_modules/wrangler/bin/wrangler.js"), ...args],
        { log: resolve(output, `logs/${role}-compile.log`) },
      );
    writeJson(
      output,
      `workers/${role}/wrangler.json`,
      freezeWorkerConfig(config, role, bundle),
    );
    dryRunConfig(
      root,
      resolve(bundle, "wrangler.json"),
      resolve(output, `proof/${role}`),
      resolve(output, `logs/${role}-frozen-dry-run.log`),
    );
    const verifiedModules = verifyFrozenPayload(
      bundle,
      resolve(output, `proof/${role}`),
    );
    workers[role] = {
      name: config.name,
      config: `workers/${role}/wrangler.json`,
      artifact: `workers/${role}`,
      sha256: digest(fileTree(bundle)),
      verified_upload_modules: verifiedModules,
      previous_version: { status: "pending" },
      deployed_version: { status: "pending" },
    };
  }
  for (const authority of plan.migrations) {
    cpSync(
      resolve(cf, authority.directory),
      resolve(output, "sql", authority.authority),
      { recursive: true },
    );
    const config = readJson(
      resolve(output, `resources/migrations/${authority.authority}.json`),
    );
    config.d1_databases[0].migrations_dir = `../sql/${authority.authority}`;
    writeJson(output, `migrations/${authority.authority}.json`, config);
  }
  if (digest(sourceIdentity(root)) !== digest(originalSource))
    throw new Error("source changed while preparing candidate");
  const trees = [
    "inputs",
    "workers",
    "sql",
    "migrations",
    "logs",
    "web/cloudflare/artifact",
    "web/self_hosted/artifact",
  ];
  const candidate = {
    schema_version: 1,
    source: originalSource,
    stage,
    brand: plan.brand,
    account_id: plan.account_id,
    resource_plan: plan,
    inventory: input,
    profiles: Object.fromEntries(
      ["cloudflare", "self_hosted"].map((target) => [
        target,
        readJson(resolve(output, `web/${target}/build-manifest.json`)),
      ]),
    ),
    tools: {
      python: PYTHON_TOOLS,
      wrangler: readJson(resolve(cf, "node_modules/wrangler/package.json"))
        .version,
      workerd: readJson(resolve(cf, "node_modules/workerd/package.json"))
        .version,
      bun: runLocal(root, "bun", ["--version"]).trim(),
    },
    workers,
    checks,
    artifact_files: Object.fromEntries(
      trees.map((path) => [path, fileTree(resolve(output, path))]),
    ),
    local_verified: true,
    release_ready: false,
    pending: [
      "CF-4 complete route/provider contracts",
      "CI-1 dual-target product conformance",
      "remote resource and domain observations",
      "prior Worker versions with new-schema compatibility",
      "explicit deployment authorization",
    ],
  };
  writeJson(output, "candidate.json", {
    ...candidate,
    candidate_digest: digest(candidate),
  });
  return readJson(resolve(output, "candidate.json"));
}
