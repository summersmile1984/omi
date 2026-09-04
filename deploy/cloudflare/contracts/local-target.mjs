import { randomBytes } from "node:crypto";
import {
  closeSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { WebSocketServer } from "ws";
import { localConfigs } from "./local-config.mjs";
import {
  assertInstalledRuntime,
  pythonWorkerInvocation,
  PYTHON_TOOLS,
} from "../scripts/python-worker.mjs";
import { freezeWorkerConfig } from "../scripts/release-build.mjs";
import { LocalProcesses } from "./local-process.mjs";
import { startInferenceControl } from "./inference-control.mjs";
import { digest, REQUIRED_SECRETS } from "../scripts/resource-input.mjs";
import { fileTree, git } from "../scripts/release-files.mjs";

const componentRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function freePort() {
  const socket = createServer();
  await new Promise((resolve, reject) => {
    socket.once("error", reject);
    socket.listen(0, "127.0.0.1", resolve);
  });
  const port = socket.address().port;
  await new Promise((resolve) => socket.close(resolve));
  return port;
}
function privateJson(path, value) {
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
}

export function prepareLocalCache(output, env) {
  const explicit = env.CLOUDFLARE_PYODIDE_CACHE_DIR;
  if (
    explicit !== undefined &&
    (typeof explicit !== "string" || !explicit.trim())
  )
    throw new Error("explicit local Pyodide cache must be a nonempty path");
  const directory = resolve(explicit ?? resolve(output, "pyodide"));
  if (explicit === undefined) mkdirSync(directory, { mode: 0o700 });
  const stat = lstatSync(directory, { throwIfNoEntry: false });
  if (!stat?.isDirectory() || stat.isSymbolicLink())
    throw new Error(
      "local Pyodide cache must be an existing ordinary directory",
    );
  env.CLOUDFLARE_PYODIDE_CACHE_DIR = directory;
  return {
    directory,
    owner: explicit === undefined ? "fixture" : "explicit-reuse",
  };
}

export async function startLocalTarget({
  output,
  brandId = "contract",
  port,
  root = componentRoot,
  signal,
}) {
  root = resolve(root);
  output = resolve(output);
  if (lstatSync(output, { throwIfNoEntry: false }))
    throw new Error("local target requires a fresh output owner");
  if (typeof brandId !== "string" || !/^[a-z][a-z0-9-]{0,23}$/.test(brandId))
    throw new Error("invalid local brand");
  assertInstalledRuntime(root);
  mkdirSync(output, { mode: 0o700 });
  const logs = resolve(output, "logs");
  mkdirSync(logs);
  const env = {
    ...process.env,
    CI: "true",
    WRANGLER_HIDE_BANNER: "true",
    WRANGLER_SEND_METRICS: "false",
    CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV: "false",
  };
  for (const key of [
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_EMAIL",
  ])
    delete env[key];
  const cache = prepareLocalCache(output, env);
  privateJson(resolve(output, "cache-owner.json"), cache);
  const processes = new LocalProcesses();
  let cancellationFailure;
  const cancel = () => {
    void processes.close().catch((error) => {
      cancellationFailure = error;
    });
  };
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  const command = async (log, executable, args, options = {}) => {
    const fd = openSync(resolve(logs, `${log}.log`), "a", 0o600);
    try {
      const result = await processes.run(executable, args, {
        ...options,
        cwd: options.cwd ?? root,
        env: options.env ?? env,
        timeout: 300000,
        stdio: ["ignore", fd, fd],
      });
      if (result.status !== 0)
        throw new Error(`local ${log} failed; inspect its private log`);
      return result;
    } finally {
      closeSync(fd);
    }
  };
  const asr = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await new Promise((resolve, reject) => {
    asr.once("listening", resolve);
    asr.once("error", reject);
  });
  asr.on("connection", (socket) => {
    let time = 0;
    socket.on("message", (_data, binary) => {
      if (binary)
        socket.send(
          JSON.stringify([
            {
              text: "I prefer concise updates. Send the synthetic follow-up.",
              start: time,
              end: ++time,
              speaker: "SPEAKER_00",
              is_user: true,
            },
          ]),
        );
    });
  });
  let closing, inferenceControl;
  const close = () =>
    (closing ??= (async () => {
      signal?.removeEventListener("abort", cancel);
      try {
        await processes.close();
        if (cancellationFailure) throw cancellationFailure;
      } finally {
        for (const client of asr.clients) client.terminate();
        await new Promise((resolve) => asr.close(resolve));
        await inferenceControl?.close();
      }
    })());
  try {
    inferenceControl = await startInferenceControl();
    port ??= await freePort();
    const namespace = `cf-${brandId}-${randomBytes(4).toString("hex")}`;
    const brandRuntime = {
      brand_id: brandId,
      display_name: "Local Atlas",
      ai_persona_name: "Mira",
    };
    const { origin, configs } = localConfigs({
      root,
      brandId,
      brandRuntime,
      namespace,
      port,
      asrPort: asr.address().port,
    });
    configs.provider.vars = {
      INFERENCE_CONTROL_ORIGIN: inferenceControl.origin,
    };
    const assertionSecret = randomBytes(32).toString("hex"),
      frozen = {},
      artifacts = {};
    const wrangler = resolve(root, "node_modules/wrangler/bin/wrangler.js");
    for (const [role, config] of Object.entries(configs)) {
      const source = resolve(output, "configs", role),
        bundle = resolve(output, "workers", role);
      mkdirSync(source, { recursive: true });
      mkdirSync(bundle, { recursive: true });
      const configPath = resolve(source, "wrangler.json");
      privateJson(configPath, config);
      const secrets = Object.fromEntries(
        (REQUIRED_SECRETS[role] ?? []).map((name) => [
          name,
          name === "INTERNAL_ASSERTION_SECRET"
            ? assertionSecret
            : randomBytes(32).toString("hex"),
        ]),
      );
      const values =
        Object.entries(secrets)
          .map(([name, value]) => `${name}=${value}`)
          .join("\n") + "\n";
      writeFileSync(resolve(source, ".dev.vars"), values, { mode: 0o600 });
      writeFileSync(resolve(bundle, ".dev.vars"), values, { mode: 0o600 });
      const args = [
        "deploy",
        "--config",
        configPath,
        "--dry-run",
        "--outdir",
        resolve(bundle, "modules"),
      ];
      if (role.startsWith("api-")) {
        symlinkSync(
          resolve(root, "python", role, "python_modules"),
          resolve(source, "python_modules"),
          "dir",
        );
        await command(`${role}-compile`, process.execPath, [
          resolve(root, "scripts/python-worker.mjs"),
          role,
          ...args,
        ]);
      } else
        await command(`${role}-compile`, process.execPath, [wrangler, ...args]);
      const compiled = freezeWorkerConfig(config, role, bundle);
      frozen[role] = resolve(bundle, "wrangler.json");
      privateJson(frozen[role], compiled);
      artifacts[role] = {
        name: config.name,
        config_sha256: digest(compiled),
        modules: fileTree(resolve(bundle, "modules")),
        ...(role.startsWith("api-")
          ? { python_modules: fileTree(resolve(bundle, "python_modules")) }
          : {}),
        secret_names: Object.keys(secrets),
      };
    }
    for (const [role, binding] of [
      ["auth", "AUTH_DB"],
      ["api-core", "APP_DB"],
    ])
      await command(`${role}-migrations`, process.execPath, [
        wrangler,
        "d1",
        "migrations",
        "apply",
        binding,
        "--local",
        "--config",
        resolve(output, "configs", role, "wrangler.json"),
        "--persist-to",
        resolve(output, "state"),
      ]);
    const invocation = pythonWorkerInvocation("api-core", ["dev"], {
      root,
      env,
    });
    const runtimeLog = resolve(logs, "runtime.log");
    const runtimeFd = openSync(runtimeLog, "a", 0o600);
    const args = [
      wrangler,
      "dev",
      "--local",
      "--port",
      String(port),
      "--inspector-port",
      String(await freePort()),
      "--persist-to",
      resolve(output, "state"),
      "--config",
      frozen.edge,
      ...Object.entries(frozen)
        .filter(([role]) => role !== "edge")
        .flatMap(([, path]) => ["--config", path]),
    ];
    const { child: runtime, completion: runtimeDone } = processes.start(
      process.execPath,
      args,
      {
        cwd: root,
        env: invocation.env,
        timeout: 3600000,
        stdio: ["ignore", runtimeFd, runtimeFd],
      },
    );
    closeSync(runtimeFd);
    let spawnError;
    runtime.once("error", (error) => (spawnError = error));
    const deadline = Date.now() + 120000;
    while (true) {
      if (
        signal?.aborted ||
        spawnError ||
        runtime.exitCode !== null ||
        runtime.signalCode !== null
      )
        throw new Error("local workerd exited before readiness");
      if (Date.now() > deadline)
        throw new Error("local workerd readiness deadline exceeded");
      if (
        readFileSync(runtimeLog, "utf8").includes(
          `Ready on http://localhost:${port}`,
        )
      ) {
        try {
          const response = await fetch(`${origin}/health`, {
            signal: AbortSignal.timeout(2000),
          });
          if (response.status === 200) break;
        } catch {}
      }
      await sleep(100);
    }
    const metadata = {
      api_origin: origin,
      auth_origin: origin,
      target: "cloudflare",
      brand_id: brandId,
      trace_dir: resolve(output, "trace"),
    };
    privateJson(resolve(output, "metadata.json"), metadata);
    privateJson(resolve(output, "fixture.json"), {
      brand_runtime: brandRuntime,
      schema_version: 1,
      source_commit: git(resolve(root, "../.."), ["rev-parse", "HEAD"]),
      source_status: git(resolve(root, "../.."), ["status", "--porcelain"]),
      migration_files: {
        auth: fileTree(resolve(root, "migrations/auth")),
        app: fileTree(resolve(root, "migrations/app")),
      },
      tools: {
        python: PYTHON_TOOLS,
        ...Object.fromEntries(
          ["wrangler", "workerd"].map((name) => [
            name,
            JSON.parse(
              readFileSync(resolve(root, `node_modules/${name}/package.json`)),
            ).version,
          ]),
        ),
      },
      locks: Object.fromEntries(
        [
          "package-lock.json",
          "python/api-core/pylock.toml",
          "python/api-ai/pylock.toml",
        ].map((path) => [path, digest(readFileSync(resolve(root, path)))]),
      ),
      artifacts,
      pyodide_cache: cache,
      inference_control_origin: inferenceControl.origin,
      command: [process.execPath, ...args],
      provider_boundary:
        "synthetic ASR and structured/text inference; actual application Workers, D1, R2, DO, Queue",
      unproved: [
        "hosted-model quality",
        "remote Vectorize/Images",
        "full product behavior",
        "release qualification",
      ],
      release_qualified: false,
    });
    return { metadata, close, command, runtimeDone };
  } catch (error) {
    try {
      await close();
    } catch (cleanup) {
      throw new AggregateError(
        [error, cleanup],
        `${error.message}; local cleanup also failed`,
      );
    }
    throw error;
  }
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  let target;
  const controller = new AbortController();
  const stop = () => controller.abort();
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  try {
    const { values } = parseArgs({
      options: {
        output: { type: "string" },
        "brand-id": { type: "string", default: "contract" },
        port: { type: "string" },
        "run-core": { type: "boolean", default: false },
        "run-recording": { type: "boolean", default: false },
        "run-chat": { type: "boolean", default: false },
      },
    });
    if (!values.output) throw new Error("--output is required");
    target = await startLocalTarget({
      output: values.output,
      brandId: values["brand-id"],
      port: values.port === undefined ? undefined : Number(values.port),
      signal: controller.signal,
    });
    process.stdout.write(JSON.stringify(target.metadata) + "\n");
    if (values["run-core"]) {
      await target.command("core", process.env.PYTHON ?? "python3", [
        resolve(componentRoot, "../../contracts/deployment/core.py"),
        "--metadata",
        resolve(values.output, "metadata.json"),
      ]);
    }
    if (values["run-recording"]) {
      await target.command("recording", process.execPath, [
        resolve(componentRoot, "contracts/recording.mjs"),
        "--metadata",
        resolve(values.output, "metadata.json"),
      ]);
    }
    if (values["run-chat"]) {
      await target.command("chat", process.execPath, [
        resolve(componentRoot, "contracts/chat.mjs"),
        "--metadata",
        resolve(values.output, "metadata.json"),
      ]);
    }
    if (
      !values["run-core"] &&
      !values["run-recording"] &&
      !values["run-chat"] &&
      !controller.signal.aborted
    ) {
      await Promise.race([
        new Promise((resolve) =>
          controller.signal.addEventListener("abort", resolve, { once: true }),
        ),
        target.runtimeDone.then(() => {
          if (!controller.signal.aborted)
            throw new Error("local runtime stopped");
        }),
      ]);
    }
    process.exitCode = controller.signal.aborted ? 130 : 0;
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  } finally {
    await target?.close();
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
  }
}
