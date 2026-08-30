import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  createD1MigrationConfig,
  resolveD1DatabaseId,
} from "./d1-migrations.mjs";
import { stagingHealthTargets, verifyStagingHealth } from "./deploy-health.mjs";
import {
  assertValidDeploymentOrder,
  createDeploymentSnapshot,
  rollbackCommandArgs,
  rollbackPlan,
  ROLLBACK_STDIO,
  STAGING_DEPLOYMENTS,
  STAGING_WORKERS,
} from "./deployment-state.mjs";
import { assertAuthenticatedSmokeConfigured } from "./smoke-staging.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const webRoot = resolve(root, "../../web/app");
const envName = process.argv[2] || "staging";
if (envName !== "staging") {
  throw new Error(
    "Only the isolated staging environment is implemented; production requires a separate approval gate.",
  );
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || root,
    stdio: "inherit",
    env: { ...process.env, ...(options.env || {}) },
  });
  if (result.status !== 0)
    throw new Error(
      `${command} ${args.join(" ")} failed with status ${result.status}`,
    );
}

function runQuiet(command, args) {
  const result = spawnSync(command, args, {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
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
  run("npx", ["next", "typegen"], { cwd: webRoot });
  run("npx", ["tsc", "--noEmit"], { cwd: webRoot });
  run("npm", ["test"], { cwd: webRoot });
  run("npm", ["run", "build:vinext:staging"], { cwd: webRoot });
  run(
    "npx",
    [
      "wrangler",
      "deploy",
      "--dry-run",
      "--config",
      "dist/server/wrangler.json",
    ],
    { cwd: webRoot },
  );
}

function captureDeploymentSnapshot() {
  const statuses = {};
  for (const workerName of STAGING_WORKERS) {
    const result = runQuiet("npx", [
      "wrangler",
      "deployments",
      "status",
      "--name",
      workerName,
      "--json",
    ]);
    if (!result.ok) {
      throw new Error(
        `unable to capture active version for ${workerName}: ${result.stderr.trim() || "wrangler failed"}`,
      );
    }
    statuses[workerName] = result.stdout;
  }
  const snapshot = createDeploymentSnapshot(statuses);
  const releaseDirectory = resolve(root, ".wrangler/releases");
  mkdirSync(releaseDirectory, { recursive: true });
  const stamp = snapshot.createdAt.replace(/[:.]/g, "-");
  const snapshotPath = resolve(
    releaseDirectory,
    `staging-before-${stamp}.json`,
  );
  writeFileSync(snapshotPath, `${JSON.stringify(snapshot, null, 2)}\n`, {
    mode: 0o600,
  });
  return { snapshot, snapshotPath };
}

function rollbackDeployment(snapshot, snapshotPath) {
  const failures = [];
  for (const { workerName, versionId } of rollbackPlan(snapshot)) {
    const result = spawnSync(
      "npx",
      rollbackCommandArgs({ workerName, versionId }, snapshotPath),
      {
        cwd: root,
        stdio: ROLLBACK_STDIO,
        env: process.env,
      },
    );
    if (result.status !== 0) failures.push(workerName);
  }
  if (failures.length) {
    throw new Error(`automatic rollback failed for: ${failures.join(", ")}`);
  }
}

function d1Exists(name) {
  return runQuiet("npx", ["wrangler", "d1", "info", name]).ok;
}

function r2Exists(name) {
  const result = runQuiet("npx", ["wrangler", "r2", "bucket", "list"]);
  if (!result.ok) return false;
  return result.stdout.split(/\s+/).includes(name);
}

function queueExists(name) {
  const result = runQuiet("npx", ["wrangler", "queues", "list"]);
  if (!result.ok) return false;
  return result.stdout.split(/\s+/).includes(name);
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
    const match = entries.find((entry) => {
      const propertyName = entry?.propertyName ?? entry?.property_name;
      return propertyName === property;
    });
    if (!match) return { exists: false, ready: false };
    const status = String(
      match?.status ?? match?.state ?? "ready",
    ).toLowerCase();
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
  if (!d1Exists("omi-cf-auth-staging"))
    run("npx", ["wrangler", "d1", "create", "omi-cf-auth-staging"]);
  if (!d1Exists("omi-cf-app-staging"))
    run("npx", ["wrangler", "d1", "create", "omi-cf-app-staging"]);
  if (!r2Exists("omi-cf-staging"))
    run("npx", ["wrangler", "r2", "bucket", "create", "omi-cf-staging"]);
  if (!r2Exists("omi-cf-conversation-recordings-staging"))
    run("npx", [
      "wrangler",
      "r2",
      "bucket",
      "create",
      "omi-cf-conversation-recordings-staging",
    ]);
  if (!r2Exists("omi-cf-speech-profiles-staging"))
    run("npx", [
      "wrangler",
      "r2",
      "bucket",
      "create",
      "omi-cf-speech-profiles-staging",
    ]);
  if (!queueExists("omi-cf-jobs-staging"))
    run("npx", ["wrangler", "queues", "create", "omi-cf-jobs-staging"]);
  if (!queueExists("omi-cf-jobs-dlq-staging"))
    run("npx", ["wrangler", "queues", "create", "omi-cf-jobs-dlq-staging"]);
  if (!queueExists("omi-cf-sync-fresh-staging"))
    run("npx", ["wrangler", "queues", "create", "omi-cf-sync-fresh-staging"]);
  if (!queueExists("omi-cf-sync-backfill-staging"))
    run("npx", [
      "wrangler",
      "queues",
      "create",
      "omi-cf-sync-backfill-staging",
    ]);
  for (const name of [
    "omi-cf-conversations-v2",
    "omi-cf-memories-v2",
    "omi-cf-action-items-v2",
    "omi-cf-transcript-chunks-v2",
    "omi-cf-x-posts-v2",
  ]) {
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
        "Omi multilingual rebuildable D1-hydrated staging search projection",
      ]);
    }
  }
  for (const name of [
    "omi-cf-conversations-v2",
    "omi-cf-transcript-chunks-v2",
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
  if (!lifecycleExists("omi-cf-staging", "expire-staged-transcriptions")) {
    run("npx", [
      "wrangler",
      "r2",
      "bucket",
      "lifecycle",
      "add",
      "omi-cf-staging",
      "expire-staged-transcriptions",
      "cf-transcriptions/",
      "--expire-days",
      "1",
    ]);
  }
  if (!lifecycleExists("omi-cf-staging", "expire-staged-sync")) {
    run("npx", [
      "wrangler",
      "r2",
      "bucket",
      "lifecycle",
      "add",
      "omi-cf-staging",
      "expire-staged-sync",
      "cf-sync/",
      "--expire-days",
      "1",
    ]);
  }
}

function applyMigrations() {
  const listed = runQuiet("npx", ["wrangler", "d1", "list", "--json"]);
  if (!listed.ok) {
    throw new Error(
      `unable to resolve staging D1 UUIDs: ${listed.stderr.trim() || "wrangler failed"}`,
    );
  }
  const migrations = [
    {
      databaseName: "omi-cf-auth-staging",
      binding: "AUTH_DB",
      migrationsDir: resolve(root, "migrations/auth"),
    },
    {
      databaseName: "omi-cf-app-staging",
      binding: "APP_DB",
      migrationsDir: resolve(root, "migrations/app"),
    },
  ];
  for (const migration of migrations) {
    const databaseId = resolveD1DatabaseId(
      listed.stdout,
      migration.databaseName,
    );
    const temporaryDirectory = mkdtempSync(
      join(tmpdir(), "omi-cf-d1-migrations-"),
    );
    const configPath = resolve(temporaryDirectory, "wrangler.jsonc");
    const config = createD1MigrationConfig({ ...migration, databaseId });
    writeFileSync(configPath, `${JSON.stringify(config, null, 2)}\n`, {
      mode: 0o600,
      flag: "wx",
    });
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
    } finally {
      rmSync(temporaryDirectory, { recursive: true, force: true });
    }
  }
}

function deployTypeScript(config) {
  run("npx", ["wrangler", "deploy", "--config", config]);
}

function deployPython(directory) {
  // Python Workers currently require uv >= 0.12.3; keep the deploy path
  // reproducible even when the developer's globally installed uv is older.
  run("uvx", ["uv==0.12.3", "run", "pywrangler", "deploy"], {
    cwd: resolve(root, directory),
  });
}

function deployWeb() {
  run(
    "npx",
    [
      "vinext-cloudflare",
      "deploy",
      "--config",
      "dist/server/wrangler.json",
      "--skip-build",
    ],
    { cwd: webRoot },
  );
}

function deployServices() {
  assertValidDeploymentOrder();
  for (const deployment of STAGING_DEPLOYMENTS) {
    if (deployment.runtime === "python") {
      deployPython(deployment.target);
    } else {
      deployTypeScript(deployment.target);
    }
  }
}

console.log(
  "Cloudflare staging release: resources are isolated under omi-cf-*; no production config is loaded.",
);
assertAuthenticatedSmokeConfigured();
qualifyRelease();
const { snapshot, snapshotPath } = captureDeploymentSnapshot();
let deploymentStarted = false;
try {
  ensureResources();
  applyMigrations();
  deploymentStarted = true;
  deployServices();
  await verifyStagingHealth({
    targets: stagingHealthTargets().filter((target) => target.name === "edge"),
  });
  deployWeb();
  const health = await verifyStagingHealth();
  run("npm", ["run", "smoke:staging"]);
  console.log(
    `Staging release complete. Health checks passed: ${JSON.stringify(health)}.`,
  );
  console.log(`Rollback snapshot: ${snapshotPath}`);
} catch (error) {
  if (deploymentStarted) {
    console.error(
      `Staging release failed; restoring the deployment snapshot at ${snapshotPath}.`,
    );
    rollbackDeployment(snapshot, snapshotPath);
    const restoredHealth = await verifyStagingHealth({
      targets: [
        {
          name: "edge",
          url: "https://omi-cf-edge-staging.summersmile1984.workers.dev/health",
        },
        {
          name: "web",
          url: "https://omi-web-app-staging.summersmile1984.workers.dev/login",
        },
      ],
    });
    console.error(
      `Staging rollback health passed: ${JSON.stringify(restoredHealth)}.`,
    );
  }
  throw error;
}
