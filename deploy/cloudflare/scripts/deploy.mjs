import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const envName = process.argv[2] || "staging";
if (envName !== "staging") {
  throw new Error("Only the isolated staging environment is implemented; production requires a separate approval gate.");
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || root,
    stdio: "inherit",
    env: { ...process.env, ...(options.env || {}) },
  });
  if (result.status !== 0) throw new Error(`${command} ${args.join(" ")} failed with status ${result.status}`);
}

function runQuiet(command, args) {
  const result = spawnSync(command, args, { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  return { ok: result.status === 0, stdout: result.stdout || "" };
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

function ensureResources() {
  if (!d1Exists("omi-cf-auth-staging")) run("npx", ["wrangler", "d1", "create", "omi-cf-auth-staging"]);
  if (!d1Exists("omi-cf-app-staging")) run("npx", ["wrangler", "d1", "create", "omi-cf-app-staging"]);
  if (!r2Exists("omi-cf-staging")) run("npx", ["wrangler", "r2", "bucket", "create", "omi-cf-staging"]);
  if (!queueExists("omi-cf-jobs-staging")) run("npx", ["wrangler", "queues", "create", "omi-cf-jobs-staging"]);
}

function applyMigrations() {
  run("npx", ["wrangler", "d1", "migrations", "apply", "omi-cf-auth-staging", "--remote", "--config", "workers/auth/wrangler.jsonc"]);
  run("npx", ["wrangler", "d1", "migrations", "apply", "omi-cf-app-staging", "--remote", "--config", "python/api-core/wrangler.jsonc"]);
}

function deployTypeScript(config) {
  run("npx", ["wrangler", "deploy", "--config", config]);
}

function deployPython(directory) {
  // Python Workers currently require uv >= 0.12.3; keep the deploy path
  // reproducible even when the developer's globally installed uv is older.
  run("uvx", ["uv==0.12.3", "run", "pywrangler", "deploy"], { cwd: resolve(root, directory) });
}

console.log("Cloudflare staging release: resources are isolated under omi-cf-*; no production config is loaded.");
ensureResources();
applyMigrations();
deployTypeScript("workers/auth/wrangler.jsonc");
deployPython("python/api-core");
deployPython("python/api-ai");
deployTypeScript("workers/realtime/wrangler.jsonc");
deployTypeScript("workers/jobs/wrangler.jsonc");
deployTypeScript("workers/edge/wrangler.jsonc");
console.log("Staging release complete. Configure INTERNAL_ASSERTION_SECRET on auth, edge, api-core, api-ai and realtime before exercising authenticated routes.");
