import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  existsSync,
  symlinkSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";
import { runReleaseProcess } from "../scripts/release-wrangler.mjs";
import { afterEach, describe, expect, it } from "vitest";
import {
  assertInstalledRuntime,
  assertLockUnchanged,
  assertToolVersion,
  pythonWorkerInvocation,
  runPythonWorker,
} from "../scripts/python-worker.mjs";

import { preparePythonSource } from "../scripts/python-source.mjs";

const temporary = [];
const componentRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
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
        root: componentRoot,
        env: { CLOUDFLARE_PYODIDE_CACHE_DIR: "/cache with spaces" },
      },
    );
    expect(command.args.at(-1)).toBe("--local");
    expect(command.env.CLOUDFLARE_WORKERD_BINARY).toBe(
      createRequire(resolve(componentRoot, "package.json"))("workerd").default,
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
    mkdirSync(resolve(root, "python/api-core/src"), { recursive: true });
    mkdirSync(resolve(root, "python/shared"));
    mkdirSync(resolve(root, "python/api-core/python_modules"));
    writeFileSync(
      resolve(root, "python/api-core/src/entry.py"),
      "import chat_target\n",
    );
    writeFileSync(resolve(root, "python/shared/chat_target.py"), "value=1\n");
    writeFileSync(
      resolve(root, "python/api-core/wrangler.jsonc"),
      '{"main":"src/entry.py"}',
    );
    const calls = [];
    let mutate = false,
      failDeploy = false,
      stagedDirectory;
    const spawn = (_command, args) => {
      const action = args.includes("deploy") ? "deploy" : args.at(-1);
      calls.push(action);
      if (action === "--version")
        return { status: 0, stdout: "pywrangler, version 1.16.7\n" };
      if (action === "sync" && mutate) writeFileSync(lock, "changed");
      if (action === "deploy") {
        stagedDirectory = dirname(args.at(-1));
        const config = JSON.parse(readFileSync(args.at(-1), "utf8"));
        expect(
          readFileSync(resolve(dirname(config.main), "chat_target.py"), "utf8"),
        ).toBe("value=1\n");
        expect(
          readFileSync(
            resolve(dirname(config.main), "_worker_application.py"),
            "utf8",
          ),
        ).toBe("import chat_target\n");
        expect(readFileSync(config.main, "utf8")).toBe(
          readFileSync(
            resolve(componentRoot, "python/core_entrypoint.py"),
            "utf8",
          ),
        );
        expect(
          readFileSync(resolve(root, "python/api-core/src/entry.py"), "utf8"),
        ).toBe("import chat_target\n");
      }
      return { status: action === "deploy" && failDeploy ? 1 : 0 };
    };
    runPythonWorker("api-core", ["deploy"], { root, env: {}, spawn });
    expect(calls).toEqual(["--version", "sync", "deploy"]);
    expect(existsSync(stagedDirectory)).toBe(false);
    failDeploy = true;
    expect(() =>
      runPythonWorker("api-core", ["deploy"], { root, env: {}, spawn }),
    ).toThrow("command failed");
    expect(existsSync(stagedDirectory)).toBe(false);
    failDeploy = false;
    calls.length = 0;
    mutate = true;
    expect(() =>
      runPythonWorker("api-core", ["deploy"], { root, env: {}, spawn }),
    ).toThrow("pylock.toml changed");
    expect(calls).toEqual(["--version", "sync"]);
  });
  it("projects shared ordinary modules without source writes and removes the owned stage", () => {
    const root = fixture(),
      project = resolve(root, "python/api-ai");
    mkdirSync(resolve(project, "src"), { recursive: true });
    mkdirSync(resolve(root, "python/shared"));
    mkdirSync(resolve(project, "python_modules"));
    writeFileSync(resolve(project, "src/entry.py"), "import chat_target\n");
    writeFileSync(resolve(root, "python/shared/chat_target.py"), "value=1\n");
    writeFileSync(
      resolve(project, "wrangler.jsonc"),
      '{"main":"src/entry.py","d1_databases":[{"migrations_dir":"../../migrations/app"}]}',
    );
    const prepared = preparePythonSource(project, ["deploy", "--dry-run"]);
    expect(
      readFileSync(resolve(prepared.directory, "src/chat_target.py"), "utf8"),
    ).toBe("value=1\n");
    expect(existsSync(resolve(project, "src/chat_target.py"))).toBe(false);
    writeFileSync(resolve(root, "python/shared/chat_target.py"), "value=2\n");
    expect(
      readFileSync(resolve(prepared.directory, "src/chat_target.py"), "utf8"),
    ).toBe("value=1\n");
    const config = JSON.parse(readFileSync(prepared.args.at(-1), "utf8"));
    expect(config.d1_databases[0].migrations_dir).toBe(
      resolve(root, "migrations/app"),
    );
    prepared.close();
    expect(existsSync(prepared.directory)).toBe(false);
    writeFileSync(resolve(project, "src/chat_target.py"), "duplicate owner");
    expect(() => preparePythonSource(project, ["deploy"])).toThrow("collides");
    rmSync(resolve(project, "src/chat_target.py"));
    symlinkSync(
      resolve(root, "python/shared/chat_target.py"),
      resolve(project, "src/linked.py"),
    );
    expect(() => preparePythonSource(project, ["deploy"])).toThrow(
      "ordinary owned files",
    );
    rmSync(resolve(project, "src/linked.py"));
    rmSync(resolve(root, "python/shared"), { recursive: true });
    const outside = resolve(root, "outside");
    mkdirSync(outside);
    writeFileSync(resolve(outside, "chat_target.py"), "wrong owner");
    symlinkSync(outside, resolve(root, "python/shared"));
    expect(() => preparePythonSource(project, ["deploy"])).toThrow(
      "ordinary owned directory",
    );
  });
  it("preserves the control descriptor until an actual local workerd is ready and answers HTTP", () => {
    const cache = mkdtempSync(resolve(tmpdir(), "workerd-control-contract-"));
    temporary.push(cache);
    const invocation = pythonWorkerInvocation("api-core", ["dev"], {
      root: componentRoot,
      env: { ...process.env, CLOUDFLARE_PYODIDE_CACHE_DIR: cache },
    });
    const source = `
      const { Miniflare, convertV4MiniflareOptions } = await import(${JSON.stringify(
        pathToFileURL(
          resolve(componentRoot, "node_modules/miniflare/dist/src/index.js"),
        ).href,
      )});
      const runtime = new Miniflare(convertV4MiniflareOptions({modules:true, script:"export default {fetch(){return new Response('runtime-ready')}}", compatibilityDate:"2026-08-27"}));
      try { const url=await runtime.ready; const response=await fetch(url); console.log(JSON.stringify({status:response.status,body:await response.text()})); }
      finally {await runtime.dispose();}
    `;
    const result = runReleaseProcess(
      process.execPath,
      ["--input-type=module", "-e", source],
      { env: invocation.env, encoding: "utf8", timeout: 10000 },
    );
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual({
      status: 200,
      body: "runtime-ready",
    });
  }, 15000);
});
