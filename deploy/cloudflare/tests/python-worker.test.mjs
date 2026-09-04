import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  assertInstalledRuntime,
  assertLockUnchanged,
  assertToolVersion,
  pythonWorkerInvocation,
  runPythonWorker,
} from "../scripts/python-worker.mjs";

const temporary = [];
function fixture() {
  const root = mkdtempSync(resolve(tmpdir(), "python-tool-contract-"));
  temporary.push(root);
  mkdirSync(resolve(root, "node_modules/wrangler"), { recursive: true });
  mkdirSync(resolve(root, "node_modules/workerd"), { recursive: true });
  writeFileSync(
    resolve(root, "package.json"),
    JSON.stringify({ devDependencies: { wrangler: "4.127.0" } }),
  );
  writeFileSync(
    resolve(root, "package-lock.json"),
    JSON.stringify({
      packages: {
        "node_modules/wrangler": { version: "4.127.0" },
        "node_modules/workerd": { version: "1.20260826.1" },
      },
    }),
  );
  writeFileSync(
    resolve(root, "node_modules/wrangler/package.json"),
    '{"version":"4.127.0"}',
  );
  writeFileSync(
    resolve(root, "node_modules/workerd/package.json"),
    '{"version":"1.20260826.1"}',
  );
  return root;
}
afterEach(() =>
  temporary
    .splice(0)
    .forEach((path) => rmSync(path, { recursive: true, force: true })),
);

describe("Python Worker tool and runtime boundary", () => {
  it("builds the pinned tool command in either Python project without a project uv resolution", () => {
    for (const name of ["api-core", "api-ai"]) {
      const command = pythonWorkerInvocation(name, ["deploy", "--dry-run"], {
        root: "/fixture",
        env: {},
      });
      expect(command.cwd).toBe(`/fixture/python/${name}`);
      expect(command.args).toEqual([
        "uv==0.12.3",
        "tool",
        "run",
        "--from",
        "workers-py==1.16.7",
        "--with",
        "uv==0.12.3",
        "pywrangler",
        "deploy",
        "--dry-run",
      ]);
      expect(command.env.NPM_CONFIG_OFFLINE).toBe("true");
      expect(command.env.MINIFLARE_WORKERD_PATH).toBeUndefined();
    }
  });

  it("launches local workerd with one cache and refuses the old incompatible tool", () => {
    const command = pythonWorkerInvocation(
      "api-core",
      ["dev", "--port", "9123"],
      {
        root: "/fixture",
        env: { CLOUDFLARE_PYODIDE_CACHE_DIR: "/cache with spaces" },
      },
    );
    expect(command.args.at(-1)).toBe("--local");
    expect(command.env.CLOUDFLARE_WORKERD_BINARY).toBe(
      "/fixture/node_modules/.bin/workerd",
    );
    expect(command.env.CLOUDFLARE_PYODIDE_CACHE_DIR).toBe("/cache with spaces");
    expect(() =>
      assertToolVersion("pywrangler, version 1.16.7\n"),
    ).not.toThrow();
    expect(() => assertToolVersion("pywrangler, version 1.17.1\n")).toThrow(
      "refusing",
    );
  });

  it("allows a version-checked preinstalled tool while retaining pinned uv", () => {
    const command = pythonWorkerInvocation("api-ai", ["sync"], {
      root: "/fixture",
      env: { CLOUDFLARE_PYWRANGLER_EXECUTABLE: "/tools/bin/pywrangler" },
    });
    expect(command.prefix).toEqual([
      "uv==0.12.3",
      "run",
      "--no-project",
      "/tools/bin/pywrangler",
    ]);
    expect(() =>
      pythonWorkerInvocation("api-core", ["dev", "--remote"]),
    ).toThrow("local");
    expect(() =>
      pythonWorkerInvocation("api-core", ["sync", "--upgrade"]),
    ).toThrow("committed pylock");
    expect(() => pythonWorkerInvocation("unknown", ["deploy"])).toThrow(
      "api-core or api-ai",
    );
  });

  it("refuses installed runtime drift before invoking npx or resolving Python packages", () => {
    const root = fixture();
    expect(() => assertInstalledRuntime(root)).not.toThrow();
    writeFileSync(
      resolve(root, "node_modules/workerd/package.json"),
      '{"version":"1.0.0"}',
    );
    expect(() => assertInstalledRuntime(root)).toThrow("workerd differs");
    writeFileSync(
      resolve(root, "node_modules/workerd/package.json"),
      '{"version":"1.20260826.1"}',
    );
    writeFileSync(
      resolve(root, "package.json"),
      '{"devDependencies":{"wrangler":"4.127.1"}}',
    );
    expect(() => assertInstalledRuntime(root)).toThrow(
      "manifest and lock disagree",
    );
  });

  it("detects deleted or rewritten Python locks while accepting the unchanged legacy lock", () => {
    const root = fixture(),
      path = resolve(root, "pylock.toml");
    writeFileSync(path, 'lock-version = "1.0"\n');
    const original = readFileSync(path);
    expect(() => assertLockUnchanged(path, original)).not.toThrow();
    writeFileSync(path, 'lock-version = "2.0"\n');
    expect(() => assertLockUnchanged(path, original)).toThrow("changed");
    rmSync(path);
    expect(() => assertLockUnchanged(path, original)).toThrow("changed");
  });
  it("prepares dependencies and checks the existing lock before the deploy side effect", () => {
    const root = fixture();
    mkdirSync(resolve(root, "python/api-core"), { recursive: true });
    const lock = resolve(root, "python/api-core/pylock.toml");
    writeFileSync(lock, 'lock-version = "1.0"\n');
    const calls = [];
    let mutate = false;
    const spawn = (_command, args) => {
      const action = args.at(-1);
      calls.push(action);
      if (action === "--version")
        return { status: 0, stdout: "pywrangler, version 1.16.7\n" };
      if (action === "sync" && mutate) writeFileSync(lock, "changed");
      return { status: 0 };
    };
    runPythonWorker("api-core", ["deploy"], { root, env: {}, spawn });
    expect(calls).toEqual(["--version", "sync", "deploy"]);
    calls.length = 0;
    mutate = true;
    expect(() =>
      runPythonWorker("api-core", ["deploy"], { root, env: {}, spawn }),
    ).toThrow("pylock.toml changed");
    expect(calls).toEqual(["--version", "sync"]);
  });
});
