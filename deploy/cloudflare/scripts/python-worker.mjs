import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// workers-py 1.17.1 requires Wrangler >=4.127.1, above the existing npm lock.
// 1.16.7 retains its native Wrangler >=4.109.0 check and consumes our pylock.
export const PYTHON_TOOLS = Object.freeze({
  uv: "0.12.3",
  workersPy: "1.16.7",
});
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export function assertInstalledRuntime(directory = root) {
  const read = (path) =>
    JSON.parse(readFileSync(resolve(directory, path), "utf8"));
  const manifest = read("package.json");
  const lock = read("package-lock.json");
  for (const name of ["wrangler", "workerd"]) {
    const expected = lock.packages[`node_modules/${name}`]?.version;
    const installed = read(`node_modules/${name}/package.json`).version;
    if (!expected || installed !== expected) {
      throw new Error(`${name} differs from package-lock.json; run npm ci`);
    }
    if (name === "wrangler" && manifest.devDependencies.wrangler !== expected) {
      throw new Error(
        "Wrangler manifest and lock disagree; review the toolchain together",
      );
    }
  }
}

export function pythonWorkerInvocation(project, args, options = {}) {
  const directory = options.root ?? root;
  const inherited = options.env ?? process.env;
  if (!["api-core", "api-ai"].includes(project)) {
    throw new Error("Python Worker must be api-core or api-ai");
  }
  if (!["sync", "dev", "deploy"].includes(args[0])) {
    throw new Error("Expected sync, dev, or deploy");
  }
  if (args.includes("--upgrade")) {
    throw new Error(
      "This entry consumes the committed pylock; upgrades require a separate reviewed change",
    );
  }
  const env = { ...inherited, NPM_CONFIG_OFFLINE: "true" };
  let prefix = [`uv==${PYTHON_TOOLS.uv}`];
  const executable = inherited.CLOUDFLARE_PYWRANGLER_EXECUTABLE;
  if (executable) {
    // An already-installed tool is useful with an offline package cache. Its
    // actual version is checked below; the override cannot relax the pin.
    prefix.push("run", "--no-project", resolve(executable));
  } else {
    prefix.push(
      "tool",
      "run",
      "--from",
      `workers-py==${PYTHON_TOOLS.workersPy}`,
      "--with",
      `uv==${PYTHON_TOOLS.uv}`,
      "pywrangler",
    );
  }
  const commandArgs = [...args];
  if (args[0] === "dev") {
    if (
      args.some((arg) => arg.startsWith("--remote")) ||
      args.includes("--local=false")
    ) {
      throw new Error(
        "Python dev is local; use an explicitly authorized deploy for remote resources",
      );
    }
    if (!args.includes("--local")) commandArgs.push("--local");
    env.CLOUDFLARE_WORKERD_BINARY = resolve(
      directory,
      "node_modules/.bin/workerd",
    );
    env.CLOUDFLARE_PYODIDE_CACHE_DIR = resolve(
      inherited.CLOUDFLARE_PYODIDE_CACHE_DIR ??
        resolve(directory, ".wrangler/pyodide"),
    );
    env.MINIFLARE_WORKERD_PATH = resolve(
      directory,
      "scripts/workerd-python-cache.sh",
    );
  }
  return {
    command: "uvx",
    prefix,
    args: [...prefix, ...commandArgs],
    cwd: resolve(directory, "python", project),
    env,
  };
}

export function assertToolVersion(output) {
  if (output.trim() !== `pywrangler, version ${PYTHON_TOOLS.workersPy}`) {
    throw new Error(
      `Expected workers-py ${PYTHON_TOOLS.workersPy}; refusing a different tool version`,
    );
  }
}

export function assertLockUnchanged(path, original) {
  if (!existsSync(path) || !readFileSync(path).equals(original)) {
    throw new Error(
      "pylock.toml changed during Python preparation; do not deploy this candidate",
    );
  }
}

export function runPythonWorker(project, args, options = {}) {
  const execute = options.spawn ?? spawnSync;
  assertInstalledRuntime(options.root ?? root);
  const invocation = pythonWorkerInvocation(project, args, options);
  const lockPath = resolve(invocation.cwd, "pylock.toml");
  const original = readFileSync(lockPath);
  const version = execute(
    invocation.command,
    [...invocation.prefix, "--version"],
    {
      cwd: invocation.cwd,
      env: invocation.env,
      encoding: "utf8",
    },
  );
  if (version.status !== 0) {
    // Package resolution messages contain no application secrets. Do not dump
    // the inherited environment or configuration (which can contain secrets).
    process.stderr.write(version.stderr ?? "");
    throw new Error(
      "Could not install/read the pinned Python tool; check the package cache or registry connection",
    );
  }
  assertToolVersion(version.stdout);
  if (args[0] === "dev") {
    if (process.platform === "win32")
      throw new Error(
        "The local workerd cache launcher requires Linux or macOS",
      );
    mkdirSync(invocation.env.CLOUDFLARE_PYODIDE_CACHE_DIR, { recursive: true });
  }
  if (args[0] !== "sync") {
    const prepared = execute(
      invocation.command,
      [...invocation.prefix, "sync"],
      {
        cwd: invocation.cwd,
        env: invocation.env,
        stdio: "inherit",
      },
    );
    assertLockUnchanged(lockPath, original);
    if (prepared.status !== 0)
      throw new Error(
        "Python dependency preparation failed before runtime/deploy",
      );
  }
  const result = execute(invocation.command, invocation.args, {
    cwd: invocation.cwd,
    env: invocation.env,
    stdio: "inherit",
  });
  assertLockUnchanged(lockPath, original);
  if (result.status !== 0)
    throw new Error(
      `Python Worker command failed (${result.status ?? result.signal})`,
    );
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  try {
    runPythonWorker(process.argv[2], process.argv.slice(3));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
