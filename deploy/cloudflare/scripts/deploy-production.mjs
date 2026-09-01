import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  assertNoPendingD1Migrations,
  createD1MigrationConfig,
  resolveProductionD1Migrations,
} from "./d1-migrations.mjs";
import { verifyStagingHealth } from "./deploy-health.mjs";
import {
  PRODUCTION_DEPLOYMENTS,
  PRODUCTION_RESOURCES,
  PRODUCTION_WORKERS,
  assertProductionConfirmation,
  assertValidProductionDeploymentOrder,
  createInitialProductionSecrets,
  createProductionDeploymentSnapshot,
  productionRollbackPlan,
  productionUrls,
  renderProductionConfig,
  resolveWorkersSubdomain,
} from "./production-environment.mjs";
import { runPreservingGeneratedFile } from "./preserve-generated-file.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const webRoot = resolve(root, "../../web/app");
const releaseDirectory = resolve(root, ".wrangler/releases");
const secretStorePath = resolve(root, ".wrangler/production-operator-secrets.json");
const subdomain = resolveWorkersSubdomain();
const urls = productionUrls(subdomain);

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || root,
    stdio: "inherit",
    env: { ...process.env, ...(options.env || {}) },
  });
  if (result.status !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} failed with status ${result.status}`,
    );
  }
}

function runQuiet(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, ...(options.env || {}) },
  });
  return {
    ok: result.status === 0,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
  };
}

function qualifyRelease() {
  run("npm", ["run", "typecheck"]);
  run("npm", ["test"]);
  for (const config of [
    "workers/auth/wrangler.jsonc",
    "workers/rate-limit/wrangler.jsonc",
    "workers/realtime/wrangler.jsonc",
    "workers/jobs/wrangler.jsonc",
    "workers/edge/wrangler.jsonc",
  ]) {
    run("npx", ["wrangler", "deploy", "--dry-run", "--config", config]);
  }
  for (const directory of ["python/api-core", "python/api-ai"]) {
    run("uvx", ["uv==0.12.3", "run", "pytest", "-q"], {
      cwd: resolve(root, directory),
    });
    run("uvx", ["uv==0.12.3", "run", "pywrangler", "deploy", "--dry-run"], {
      cwd: resolve(root, directory),
    });
  }
  runPreservingGeneratedFile(resolve(webRoot, "next-env.d.ts"), () =>
    run("npx", ["next", "typegen"], { cwd: webRoot }),
  );
  run("npx", ["tsc", "--noEmit"], { cwd: webRoot });
  run("npm", ["test"], { cwd: webRoot });
}

function d1Exists(name) {
  return runQuiet("npx", ["wrangler", "d1", "info", name]).ok;
}

function r2Exists(name) {
  const result = runQuiet("npx", ["wrangler", "r2", "bucket", "list"]);
  return result.ok && result.stdout.split(/\s+/).includes(name);
}

function queueExists(name) {
  const result = runQuiet("npx", ["wrangler", "queues", "list"]);
  return result.ok && result.stdout.split(/\s+/).includes(name);
}

function vectorizeExists(name) {
  return runQuiet("npx", ["wrangler", "vectorize", "get", name, "--json"]).ok;
}

function vectorizeMetadataIndexState(name, property) {
  const result = runQuiet("npx", [
    "wrangler",
    "vectorize",
    "list-metadata-index",
    name,
    "--json",
  ]);
  if (!result.ok) return { exists: false, ready: false };
  try {
    const payload = JSON.parse(result.stdout);
    const entries = Array.isArray(payload)
      ? payload
      : Array.isArray(payload?.metadataIndexes)
        ? payload.metadataIndexes
        : Array.isArray(payload?.indexes)
          ? payload.indexes
          : [];
    const match = entries.find(
      (entry) => (entry?.propertyName ?? entry?.property_name) === property,
    );
    if (!match) return { exists: false, ready: false };
    const status = String(match?.status ?? match?.state ?? "ready").toLowerCase();
    return { exists: true, ready: status === "ready" };
  } catch {
    return { exists: false, ready: false };
  }
}

function waitForVectorizeMetadataIndex(name, property) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (vectorizeMetadataIndexState(name, property).ready) return;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2_000);
  }
  throw new Error(
    `Vectorize metadata index did not become ready: ${name}.${property}`,
  );
}

function lifecycleExists(bucket, id) {
  const result = runQuiet("npx", [
    "wrangler",
    "r2",
    "bucket",
    "lifecycle",
    "list",
    bucket,
  ]);
  return result.ok && result.stdout.includes(id);
}

function ensureResources() {
  for (const name of PRODUCTION_RESOURCES.d1) {
    if (!d1Exists(name)) run("npx", ["wrangler", "d1", "create", name]);
  }
  for (const name of PRODUCTION_RESOURCES.r2) {
    if (!r2Exists(name)) {
      run("npx", ["wrangler", "r2", "bucket", "create", name]);
    }
  }
  for (const name of PRODUCTION_RESOURCES.queues) {
    if (!queueExists(name)) {
      run("npx", ["wrangler", "queues", "create", name]);
    }
  }
  for (const name of PRODUCTION_RESOURCES.vectorize) {
    if (!vectorizeExists(name)) {
      run("npx", [
        "wrangler",
        "vectorize",
        "create",
        name,
        "--dimensions",
        "1024",
        "--metric",
        "cosine",
        "--description",
        "Omi multilingual rebuildable D1-hydrated production search projection",
      ]);
    }
  }
  for (const name of [
    "omi-cf-conversations-production-v1",
    "omi-cf-transcript-chunks-production-v1",
  ]) {
    if (!vectorizeMetadataIndexState(name, "created_at").exists) {
      run("npx", [
        "wrangler",
        "vectorize",
        "create-metadata-index",
        name,
        "--propertyName",
        "created_at",
        "--type",
        "number",
      ]);
    }
    waitForVectorizeMetadataIndex(name, "created_at");
  }
  if (!lifecycleExists("omi-cf-production", "expire-production-transcriptions")) {
    run("npx", [
      "wrangler",
      "r2",
      "bucket",
      "lifecycle",
      "add",
      "omi-cf-production",
      "expire-production-transcriptions",
      "cf-transcriptions/",
      "--expire-days",
      "1",
    ]);
  }
  if (!lifecycleExists("omi-cf-production", "expire-production-sync")) {
    run("npx", [
      "wrangler",
      "r2",
      "bucket",
      "lifecycle",
      "add",
      "omi-cf-production",
      "expire-production-sync",
      "cf-sync/",
      "--expire-days",
      "1",
    ]);
  }
}

function resolveMigrations() {
  const listed = runQuiet("npx", ["wrangler", "d1", "list", "--json"]);
  if (!listed.ok) {
    throw new Error(
      `unable to resolve production D1 UUIDs: ${listed.stderr.trim() || "wrangler failed"}`,
    );
  }
  return resolveProductionD1Migrations(listed.stdout, (directory) =>
    resolve(root, directory),
  );
}

function applyMigrations(migrations) {
  for (const migration of migrations) {
    const temporaryDirectory = mkdtempSync(
      join(tmpdir(), "omi-cf-production-migrations-"),
    );
    const configPath = resolve(temporaryDirectory, "wrangler.jsonc");
    writeFileSync(
      configPath,
      `${JSON.stringify(createD1MigrationConfig(migration), null, 2)}\n`,
      { mode: 0o600, flag: "wx" },
    );
    try {
      run("npx", [
        "wrangler",
        "d1",
        "migrations",
        "apply",
        migration.binding,
        "--remote",
        "--config",
        configPath,
      ]);
      const verification = runQuiet("npx", [
        "wrangler",
        "d1",
        "migrations",
        "list",
        migration.binding,
        "--remote",
        "--config",
        configPath,
      ]);
      if (!verification.ok) {
        throw new Error(
          `unable to verify ${migration.databaseName} migrations: ${verification.stderr.trim() || "wrangler failed"}`,
        );
      }
      assertNoPendingD1Migrations(
        `${verification.stdout}\n${verification.stderr}`,
        migration.databaseName,
      );
    } finally {
      rmSync(temporaryDirectory, { recursive: true, force: true });
    }
  }
}

function configOptions(migrations) {
  const byName = Object.fromEntries(
    migrations.map((migration) => [migration.databaseName, migration.databaseId]),
  );
  return {
    appDatabaseId: byName["omi-cf-app-production"],
    authDatabaseId: byName["omi-cf-auth-production"],
    subdomain,
  };
}

function withProductionConfig(relativePath, options, callback) {
  const sourcePath = resolve(root, relativePath);
  const temporaryPath = resolve(
    dirname(sourcePath),
    `.wrangler-production-${process.pid}.jsonc`,
  );
  const rendered = renderProductionConfig(
    readFileSync(sourcePath, "utf8"),
    options,
  );
  writeFileSync(temporaryPath, rendered, { mode: 0o600, flag: "wx" });
  try {
    return callback(temporaryPath);
  } finally {
    rmSync(temporaryPath, { force: true });
  }
}

function productionStatuses() {
  const statuses = {};
  for (const workerName of PRODUCTION_WORKERS) {
    const result = runQuiet("npx", [
      "wrangler",
      "deployments",
      "status",
      "--name",
      workerName,
      "--json",
    ]);
    if (result.ok) {
      statuses[workerName] = result.stdout;
    } else if (result.stderr.includes("[code: 10007]")) {
      statuses[workerName] = null;
    } else {
      throw new Error(
        `unable to capture active version for ${workerName}: ${result.stderr.trim() || "wrangler failed"}`,
      );
    }
  }
  return statuses;
}

function captureDeploymentSnapshot(statuses) {
  const snapshot = createProductionDeploymentSnapshot(statuses);
  mkdirSync(releaseDirectory, { recursive: true });
  const stamp = snapshot.createdAt.replace(/[:.]/g, "-");
  const snapshotPath = resolve(
    releaseDirectory,
    `production-before-${stamp}.json`,
  );
  writeFileSync(snapshotPath, `${JSON.stringify(snapshot, null, 2)}\n`, {
    mode: 0o600,
    flag: "wx",
  });
  return { snapshot, snapshotPath };
}

function validateSecretStore(secretStore) {
  if (
    secretStore?.version !== 1 ||
    secretStore?.environment !== "production" ||
    !secretStore.workers ||
    typeof secretStore.workers !== "object"
  ) {
    throw new Error("invalid Cloudflare production secret store");
  }
  return secretStore;
}

function loadOrCreateSecretStore(statuses) {
  mkdirSync(dirname(secretStorePath), { recursive: true });
  if (existsSync(secretStorePath)) {
    if ((statSync(secretStorePath).mode & 0o077) !== 0) {
      throw new Error("Cloudflare production secret store must use mode 0600");
    }
    return validateSecretStore(JSON.parse(readFileSync(secretStorePath, "utf8")));
  }
  if (Object.values(statuses).some((status) => status !== null)) {
    throw new Error(
      "production Workers already exist but the local mode-0600 secret store is missing",
    );
  }
  const secretStore = createInitialProductionSecrets(subdomain);
  writeFileSync(secretStorePath, `${JSON.stringify(secretStore, null, 2)}\n`, {
    mode: 0o600,
    flag: "wx",
  });
  return secretStore;
}

function withSecretsFile(workerName, secretStore, callback) {
  const secrets = secretStore.workers[workerName];
  if (!secrets) return callback(null);
  const temporaryDirectory = mkdtempSync(
    join(tmpdir(), "omi-cf-production-secrets-"),
  );
  const secretPath = resolve(temporaryDirectory, "secrets.json");
  writeFileSync(secretPath, `${JSON.stringify(secrets)}\n`, {
    mode: 0o600,
    flag: "wx",
  });
  try {
    return callback(secretPath);
  } finally {
    rmSync(temporaryDirectory, { recursive: true, force: true });
  }
}

function dryRunProductionConfigs(options) {
  for (const deployment of PRODUCTION_DEPLOYMENTS) {
    withProductionConfig(deployment.target, options, (configPath) => {
      if (deployment.runtime === "python") {
        run(
          "uvx",
          [
            "uv==0.12.3",
            "run",
            "pywrangler",
            "deploy",
            "--dry-run",
            "--config",
            configPath,
          ],
          { cwd: dirname(resolve(root, deployment.target)) },
        );
      } else {
        run("npx", ["wrangler", "deploy", "--dry-run", "--config", configPath]);
      }
    });
  }
}

function deployServices(options, statuses, secretStore, deployedWorkers) {
  assertValidProductionDeploymentOrder();
  for (const deployment of PRODUCTION_DEPLOYMENTS) {
    withProductionConfig(deployment.target, options, (configPath) => {
      withSecretsFile(deployment.workerName, secretStore, (secretPath) => {
        const initialSecrets = statuses[deployment.workerName] === null && secretPath;
        if (deployment.runtime === "python") {
          const args = [
            "uv==0.12.3",
            "run",
            "pywrangler",
            "deploy",
            "--config",
            configPath,
          ];
          if (initialSecrets) args.push("--secrets-file", secretPath);
          run("uvx", args, {
            cwd: dirname(resolve(root, deployment.target)),
          });
        } else {
          const args = ["wrangler", "deploy", "--config", configPath];
          if (initialSecrets) args.push("--secrets-file", secretPath);
          run("npx", args);
        }
      });
    });
    deployedWorkers.add(deployment.workerName);
  }
}

function deployWeb(options, deployedWorkers) {
  run("npm", ["run", "build:vinext:production"], {
    cwd: webRoot,
    env: { CLOUDFLARE_WORKERS_SUBDOMAIN: subdomain },
  });
  const generatedConfig = resolve(webRoot, "dist/server/wrangler.json");
  writeFileSync(
    generatedConfig,
    renderProductionConfig(readFileSync(generatedConfig, "utf8"), options),
  );
  run("npx", [
    "wrangler",
    "deploy",
    "--dry-run",
    "--config",
    generatedConfig,
  ], { cwd: webRoot });
  run(
    "npx",
    [
      "vinext-cloudflare",
      "deploy",
      "--config",
      generatedConfig,
      "--skip-build",
    ],
    { cwd: webRoot },
  );
  deployedWorkers.add("omi-web-app-production");
}

function rollbackDeployment(snapshot, snapshotPath, deployedWorkers) {
  const failures = [];
  for (const item of productionRollbackPlan(snapshot)) {
    if (!deployedWorkers.has(item.workerName)) continue;
    let result;
    if (item.action === "delete") {
      result = spawnSync(
        "npx",
        ["wrangler", "delete", "--name", item.workerName, "--force"],
        { cwd: root, stdio: ["ignore", "inherit", "inherit"], env: process.env },
      );
    } else {
      result = spawnSync(
        "npx",
        [
          "wrangler",
          "rollback",
          item.versionId,
          "--name",
          item.workerName,
          "--message",
          `automatic production rollback: ${snapshotPath.split("/").pop()}`.slice(0, 120),
          "--yes",
        ],
        { cwd: root, stdio: ["ignore", "inherit", "inherit"], env: process.env },
      );
    }
    if (result.status !== 0) failures.push(item.workerName);
  }
  if (failures.length) {
    throw new Error(`automatic production rollback failed for: ${failures.join(", ")}`);
  }
}

assertProductionConfirmation();
console.log(
  "Cloudflare production release: independent production resources; no api.omi.me/app.omi.me DNS changes.",
);
qualifyRelease();
ensureResources();
const migrations = resolveMigrations();
applyMigrations(migrations);
const options = configOptions(migrations);
dryRunProductionConfigs(options);
const statuses = productionStatuses();
const secretStore = loadOrCreateSecretStore(statuses);
const { snapshot, snapshotPath } = captureDeploymentSnapshot(statuses);
const deployedWorkers = new Set();
try {
  deployServices(options, statuses, secretStore, deployedWorkers);
  await verifyStagingHealth({
    targets: [{ name: "edge", url: `${urls.edge}/ready` }],
  });
  deployWeb(options, deployedWorkers);
  const health = await verifyStagingHealth({
    targets: [
      { name: "edge", url: `${urls.edge}/ready` },
      { name: "web", url: `${urls.web}/api/worker-ready` },
    ],
  });
  console.log(
    `Production release complete. Health checks passed: ${JSON.stringify(health)}.`,
  );
  console.log(`Rollback snapshot: ${snapshotPath}`);
} catch (error) {
  if (deployedWorkers.size > 0) {
    console.error(
      `Production release failed; restoring the deployment snapshot at ${snapshotPath}.`,
    );
    rollbackDeployment(snapshot, snapshotPath, deployedWorkers);
  }
  throw error;
}
