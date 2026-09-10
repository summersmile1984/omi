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
import { copyFrozenTarget } from "./frozen-local.mjs";
import { qualificationContext } from "./qualification-context.mjs";
import {
  assertInstalledRuntime,
  pythonWorkerInvocation,
  PYTHON_TOOLS,
} from "../scripts/python-worker.mjs";
import { freezeWorkerConfig } from "../scripts/release-build.mjs";
import { LocalProcesses } from "./local-process.mjs";
import { startInferenceControl } from "./inference-control.mjs";
import { readDevLlmVars } from "./dev-llm-config.mjs";
import { attachDevAsr } from "./dev-asr-socket.mjs";
import { digest, REQUIRED_SECRETS } from "../scripts/resource-input.mjs";
import { fileTree, git } from "../scripts/release-files.mjs";
import { RELEASE_READINESS, isReleaseReady } from "../scripts/release-wrangler.mjs";

const componentRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function failedProductCases(metadata) {
  if (!metadata?.trace_dir) return [];
  const failures = [];
  const validId = (value) => typeof value === "string" && /^[a-zA-Z0-9_.:-]{1,100}$/.test(value);
  const publicReasons = new Set([
    "recording frame deadline exceeded",
    "recording upgrade deadline exceeded",
    "recording closed before expected event",
    "actual queue did not finalize recording",
  ]);
  for (const suite of ["core", "recording", "chat", "share"]) {
    try {
      const path = resolve(metadata.trace_dir, `${suite}-results.json`);
      const stat = lstatSync(path);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 1024 * 1024) continue;
      const report = JSON.parse(readFileSync(path, "utf8"));
      if (!Array.isArray(report.cases)) continue;
      let previous;
      for (const row of report.cases) {
        if (!validId(row?.id)) continue;
        if (row.result === "pass") previous = row.id;
        if (row.result !== "fail") continue;
        const status = typeof row.error === "string" ? row.error.match(
          /returned HTTP [1-5][0-9]{2}; expected [1-5][0-9]{2}|(?:auth|api) (?:GET|POST|PUT|PATCH|DELETE) expected [0-9,]+, received [1-5][0-9]{2}/
        )?.[0] : undefined;
        failures.push({
          id: row.id,
          ...(previous ? { after: previous } : {}),
          ...(status ? { status } : {}),
          ...(publicReasons.has(row.error) ? { reason: row.error } : {}),
        });
      }
    } catch {
      // One absent or malformed suite must not hide another suite's failure.
    }
  }
  return failures.slice(0, 50);
}

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
      "local Pyodide cache must be an existing ordinary directory"
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
  shareOrigin,
  signal,
  webOrigin,
  candidateContext,
  llmDevVars,
}) {
  const liveLlm = llmDevVars ? readDevLlmVars(llmDevVars) : undefined;
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
  const asrSecret = randomBytes(32).toString("hex");
  const asr = new WebSocketServer({
    host: "127.0.0.1",
    port: 0,
    maxPayload: 1000000,
    ...(liveLlm
      ? {
          verifyClient: ({ req }) =>
            req.headers.authorization === `Token ${asrSecret}`,
        }
      : {}),
  });
  await new Promise((resolve, reject) => {
    asr.once("listening", resolve);
    asr.once("error", reject);
  });
  asr.on("connection", (socket, request) => {
    if (liveLlm) {
      attachDevAsr(socket, request, liveLlm.secrets);
      return;
    }
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
          ])
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
    const candidateInput = candidateContext
      ? copyFrozenTarget(candidateContext, output)
      : undefined;
    if (candidateInput) brandId = candidateInput.brandId;
    inferenceControl = await startInferenceControl();
    port ??= await freePort();
    const webPort = candidateInput ? await freePort() : undefined;
    if (candidateInput) {
      if (webOrigin || shareOrigin)
        throw new Error("frozen local target owns its Web and share origins");
      webOrigin = shareOrigin = `http://127.0.0.1:${webPort}`;
    }
    const namespace = `cf-${brandId}-${randomBytes(4).toString("hex")}`;
    const brandRuntime = candidateInput?.brandRuntime ?? {
      brand_id: brandId,
      display_name: "Local Atlas",
      ai_persona_name: "Mira",
    };
    const firmwarePolicy = candidateInput?.firmwarePolicy ?? {
      schema_version: 1,
      brand_id: brandId,
      device_model: "Local CV1",
      device_model_aliases: ["nrf5340"],
      release_tag_prefix: "Local_CV1_v",
      release_asset_prefix: "Local_CV1_OTA_v",
      github_releases_url:
        "https://api.github.com/repos/local/firmware/releases",
    };
    const supportEmail =
      candidateInput?.supportEmail ?? "support@atlas.example.invalid";
    const { origin, configs } = localConfigs({
      root,
      brandId,
      brandRuntime,
      firmwarePolicy,
      supportEmail,
      namespace,
      port,
      asrPort: asr.address().port,
      shareOrigin,
      webOrigin,
      ...(candidateInput
        ? {
            templates: candidateInput.templates,
            migrationsRoot: candidateInput.migrationsRoot,
            preservePolicy: true,
          }
        : {}),
    });
    configs.provider.vars = {
      INFERENCE_CONTROL_ORIGIN: inferenceControl.origin,
    };
    if (liveLlm) {
      configs.provider.main = resolve(root, "contracts/provider-dev.ts");
      // The public embedding endpoint otherwise selects the legacy 768-wide
      // BGE base model; local Ollama and memory vectors share BGE-M3 (1024).
      configs["api-ai"].vars.WORKERS_AI_EMBEDDING_MODEL = "@cf/baai/bge-m3";
    }
    const assertionSecret = randomBytes(32).toString("hex"),
      screenFrameSecret = randomBytes(32).toString("hex"),
      memoryPrivacySecret = randomBytes(32).toString("hex"),
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
            : name === "SCREEN_FRAME_SIGNING_SECRET"
            ? screenFrameSecret
            : name === "MEMORY_PRIVACY_SECRET"
            ? memoryPrivacySecret
            : randomBytes(32).toString("hex"),
        ])
      );
      if (role === "provider" && liveLlm)
        Object.assign(secrets, liveLlm.secrets);
      if (role === "realtime" && liveLlm) secrets.ASR_API_KEY = asrSecret;
      const values =
        Object.entries(secrets)
          .map(([name, value]) => `${name}=${JSON.stringify(value)}`)
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
      if (candidateInput && role !== "provider") {
        frozen[role] = resolve(bundle, "wrangler.json");
        privateJson(frozen[role], config);
      } else {
        if (role.startsWith("api-")) {
          symlinkSync(
            resolve(root, "python", role, "python_modules"),
            resolve(source, "python_modules"),
            "dir"
          );
          await command(`${role}-compile`, process.execPath, [
            resolve(root, "scripts/python-worker.mjs"),
            role,
            ...args,
          ]);
        } else
          await command(`${role}-compile`, process.execPath, [
            wrangler,
            ...args,
          ]);
        const compiled = freezeWorkerConfig(config, role, bundle);
        frozen[role] = resolve(bundle, "wrangler.json");
        privateJson(frozen[role], compiled);
      }
      artifacts[role] = {
        name: config.name,
        config_sha256: digest(JSON.parse(readFileSync(frozen[role], "utf8"))),
        modules: fileTree(resolve(bundle, "modules")),
        ...(role.startsWith("api-")
          ? { python_modules: fileTree(resolve(bundle, "python_modules")) }
          : {}),
        ...(config.assets
          ? { assets: fileTree(resolve(bundle, "assets")) }
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
      resolve(root, "contracts/local-runtime.mjs"),
      "--port",
      String(port),
      "--persist-to",
      resolve(output, "state"),
      "--config",
      frozen.edge,
      ...Object.entries(frozen)
        .filter(([role]) => role !== "edge" && role !== "web")
        .flatMap(([, path]) => ["--config", path]),
      ...(candidateInput
        ? ["--web-config", frozen.web, "--web-port", String(webPort)]
        : []),
    ];
    const { child: runtime, completion: runtimeDone } = processes.start(
      process.execPath,
      args,
      {
        cwd: root,
        env: invocation.env,
        timeout: 3600000,
        stdio: ["ignore", runtimeFd, runtimeFd],
      }
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
          `Ready on http://localhost:${port}`
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
    if (candidateInput) {
      const deadline = Date.now() + 120000;
      while (true) {
        if (
          signal?.aborted ||
          runtime.exitCode !== null ||
          runtime.signalCode !== null
        )
          throw new Error("local frozen Web exited before readiness");
        if (Date.now() > deadline)
          throw new Error("local frozen Web readiness deadline exceeded");
        if (
          readFileSync(runtimeLog, "utf8").includes(
            `Ready on http://localhost:${webPort}`
          )
        ) {
          const response = await fetch(`${webOrigin}/login`, {
            redirect: "error",
            signal: AbortSignal.timeout(5000),
          });
          if (response.status !== 200)
            throw new Error(`local frozen Web returned ${response.status}`);
          await response.body?.cancel();
          const readiness = await fetch(`${webOrigin}${RELEASE_READINESS.web.path}`, {
            redirect: "error", signal: AbortSignal.timeout(15000),
          });
          if (!(await isReleaseReady(readiness)))
            throw new Error(`local frozen Web binding readiness returned ${readiness.status}`);
          break;
        }
        await sleep(100);
      }
      candidateInput.verifyPayload();
    }
    const metadata = {
      api_origin: origin,
      auth_origin: origin,
      target: "cloudflare",
      brand_id: brandId,
      ...(candidateInput ? { web_origin: webOrigin } : {}),
      trace_dir: resolve(output, "trace"),
    };
    privateJson(resolve(output, "metadata.json"), metadata);
    privateJson(resolve(output, "fixture.json"), {
      brand_runtime: brandRuntime,
      firmware_policy: firmwarePolicy,
      support_email: supportEmail,
      public_share_origin: shareOrigin ?? origin,
      schema_version: 1,
      source_commit: git(resolve(root, "../.."), ["rev-parse", "HEAD"]),
      source_status: git(resolve(root, "../.."), ["status", "--porcelain"]),
      migration_files: {
        auth: fileTree(
          resolve(
            candidateInput?.migrationsRoot ?? resolve(root, "migrations"),
            "auth"
          )
        ),
        app: fileTree(
          resolve(
            candidateInput?.migrationsRoot ?? resolve(root, "migrations"),
            "app"
          )
        ),
      },
      tools: {
        local_transport: "direct-miniflare-workerd",
        python: PYTHON_TOOLS,
        ...Object.fromEntries(
          ["wrangler", "workerd"].map((name) => [
            name,
            JSON.parse(
              readFileSync(resolve(root, `node_modules/${name}/package.json`))
            ).version,
          ])
        ),
      },
      locks: Object.fromEntries(
        [
          "package-lock.json",
          "python/api-core/pylock.toml",
          "python/api-ai/pylock.toml",
        ].map((path) => [path, digest(readFileSync(resolve(root, path)))])
      ),
      artifacts,
      inference: liveLlm?.evidence ?? { mode: "controlled" },
      ...(candidateContext
        ? {
            candidate_digest: candidateContext.candidate.candidate_digest,
            artifact_mode: "frozen-copy",
            web_boundary:
              "frozen SSR, assets and EDGE share binding; browser API/auth origins remain production-baked and require deployed verification",
          }
        : { artifact_mode: "source-build" }),
      pyodide_cache: cache,
      inference_control_origin: inferenceControl.origin,
      command: [process.execPath, ...args],
      provider_boundary: liveLlm
        ? "external LLM/MiMo ASR/TTS and local Ollama embedding; transient memory Vectorize IO; actual application Workers, D1, R2, DO, Queue"
        : "synthetic ASR, structured/text/embedding inference and transient memory Vectorize IO; actual application Workers, D1, R2, DO, Queue",
      unproved: [
        "hosted-model quality",
        "remote Vectorize/Images",
        "full product behavior",
        "release qualification",
      ],
      release_qualified: false,
    });
    return {
      metadata,
      close,
      command,
      runtimeDone,
      verifyPayload: candidateInput?.verifyPayload,
    };
  } catch (error) {
    try {
      await close();
    } catch (cleanup) {
      throw new AggregateError(
        [error, cleanup],
        `${error.message}; local cleanup also failed`
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
        "share-origin": { type: "string" },
        "web-origin": { type: "string" },
        candidate: { type: "string" },
        "llm-dev-vars": { type: "string" },
        "run-core": { type: "boolean", default: false },
        "run-recording": { type: "boolean", default: false },
        "run-chat": { type: "boolean", default: false },
        "run-share": { type: "boolean", default: false },
      },
    });
    if (!values.output) throw new Error("--output is required");
    if (
      values["llm-dev-vars"] &&
      (values["run-recording"] || values["run-share"])
    )
      throw new Error(
        "recording/share fault suites require the controlled provider; use --run-core --run-chat for live LLM"
      );
    const candidateDirectory = values.candidate && resolve(values.candidate);
    const candidate =
      candidateDirectory &&
      JSON.parse(
        readFileSync(resolve(candidateDirectory, "candidate.json"), "utf8")
      );
    target = await startLocalTarget({
      output: values.output,
      brandId: values["brand-id"],
      port: values.port === undefined ? undefined : Number(values.port),
      shareOrigin: values["share-origin"],
      signal: controller.signal,
      webOrigin: values["web-origin"],
      llmDevVars: values["llm-dev-vars"],
      candidateContext:
        candidate &&
        qualificationContext(resolve(componentRoot, "../.."), {
          candidate_directory: candidateDirectory,
          candidate,
          observations: { release_phase: "candidate" },
        }),
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
        resolve(
          componentRoot,
          values["llm-dev-vars"]
            ? "contracts/chat-live.mjs"
            : "contracts/chat.mjs"
        ),
        "--metadata",
        resolve(values.output, "metadata.json"),
      ]);
    }
    if (values["run-share"]) {
      await target.command("share", process.execPath, [
        resolve(componentRoot, "contracts/share.mjs"),
        "--metadata",
        resolve(values.output, "metadata.json"),
      ]);
    }
    if (
      !values["run-core"] &&
      !values["run-recording"] &&
      !values["run-chat"] &&
      !values["run-share"] &&
      !controller.signal.aborted
    ) {
      await Promise.race([
        new Promise((resolve) =>
          controller.signal.addEventListener("abort", resolve, { once: true })
        ),
        target.runtimeDone.then(() => {
          if (!controller.signal.aborted)
            throw new Error("local runtime stopped");
        }),
      ]);
    }
    target.verifyPayload?.();
    process.exitCode = controller.signal.aborted ? 130 : 0;
  } catch (error) {
    console.error(error.message);
    const failed = failedProductCases(target?.metadata);
    if (failed.length)
      console.error(JSON.stringify({ failed_product_cases: failed }));
    process.exitCode = 1;
  } finally {
    await target?.close();
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
  }
}
