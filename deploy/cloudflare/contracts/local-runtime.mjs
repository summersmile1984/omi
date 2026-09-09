import { existsSync, lstatSync, readFileSync, readdirSync } from "node:fs";
import { dirname, extname, matchesGlob, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { unstable_getMiniflareWorkerOptions } from "wrangler";
import { assertInstalledRuntime } from "../scripts/python-worker.mjs";

function files(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = resolve(directory, name),
      stat = lstatSync(path);
    if (stat.isDirectory()) return files(path);
    if (!stat.isFile())
      throw new Error("frozen runtime requires ordinary files");
    return [path];
  });
}

// Consume the exact no-bundle upload tree. Wrangler's binding converter remains
// the owner of D1/R2/Queues/DO/assets configuration. Miniflare serves HTTP/SSE/WS
// directly, excluding the extra development ProxyWorker hop (workers-sdk#14641).
export function frozenRuntimeWorker(configPath) {
  const config = JSON.parse(readFileSync(configPath, "utf8"));
  const modulesRoot = resolve(dirname(configPath), "modules");
  // The disposable projection makes release paths absolute. Both forms must
  // still identify this artifact's exact module directory.
  if (
    config.no_bundle !== true ||
    config.find_additional_modules !== true ||
    typeof config.base_dir !== "string" ||
    resolve(dirname(configPath), config.base_dir) !== modulesRoot
  )
    throw new Error("local runtime requires a frozen upload configuration");
  const { main, workerOptions, externalWorkers } =
    unstable_getMiniflareWorkerOptions(configPath);
  if (dirname(main) !== modulesRoot)
    throw new Error("unexpected frozen entrypoint");
  const typeOf = (path) => {
    const name = relative(modulesRoot, path);
    const rule = workerOptions.modulesRules.find((rule) =>
      rule.include.some((glob) => matchesGlob(name, glob))
    );
    const type =
      rule?.type ??
      { ".js": "ESModule", ".mjs": "ESModule", ".cjs": "CommonJS" }[
        extname(path)
      ];
    if (!type) throw new Error("frozen module has no declared runtime type");
    return type;
  };
  const modules = [
    main,
    ...files(modulesRoot).filter(
      (path) =>
        path !== main &&
        relative(modulesRoot, path) !== "README.md" &&
        !(dirname(path) === modulesRoot && path.endsWith(".map"))
    ),
  ].map((path) => ({ type: typeOf(path), path }));
  const vendor = resolve(dirname(configPath), "python_modules");
  if (existsSync(vendor)) {
    for (const path of files(vendor)) {
      const name = relative(vendor, path);
      if (
        (config.python_modules?.exclude ?? ["**/*.pyc"]).some((glob) =>
          matchesGlob(name, glob)
        )
      )
        continue;
      modules.push({
        // Same vendored SDK JavaScript rule as Wrangler's module collector.
        type:
          name.startsWith("workers/") && [".js", ".mjs"].includes(extname(path))
            ? "ESModule"
            : "Data",
        path: resolve(modulesRoot, "python_modules", name),
        contents: readFileSync(path),
      });
    }
  }
  return [
    { ...workerOptions, name: config.name, modulesRoot, modules },
    ...externalWorkers,
  ];
}

export async function startFrozenRuntime({ configs, port, state, web }) {
  assertInstalledRuntime();
  const workers = configs.flatMap(frozenRuntimeWorker);
  if (web) {
    const [primary, ...external] = frozenRuntimeWorker(web.config);
    primary.unsafeDirectSockets = [{ host: "127.0.0.1", port: web.port }];
    workers.push(primary, ...external);
  }
  const runtime = new Miniflare(
    convertV4MiniflareOptions({
      host: "127.0.0.1",
      port,
      cf: false,
      resourcePersistencePath: resolve(state, "v3"),
      workers,
    })
  );
  try {
    await runtime.ready;
    return runtime;
  } catch (error) {
    await runtime.dispose();
    throw error;
  }
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  const { values } = parseArgs({
    options: {
      config: { type: "string", multiple: true },
      port: { type: "string" },
      "persist-to": { type: "string" },
      "web-config": { type: "string" },
      "web-port": { type: "string" },
    },
  });
  const port = Number(values.port);
  if (
    !values.config?.length ||
    !values["persist-to"] ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65535
  )
    throw new Error("expected --config, --port and --persist-to");
  const runtime = await startFrozenRuntime({
    configs: values.config,
    port,
    state: values["persist-to"],
    web: values["web-config"]
      ? { config: values["web-config"], port: Number(values["web-port"]) }
      : undefined,
  });
  console.log(`Ready on http://localhost:${port} (direct Miniflare/workerd)`);
  if (values["web-config"])
    console.log(`Ready on http://localhost:${values["web-port"]} (frozen Web)`);
  for (const signal of ["SIGINT", "SIGTERM"])
    process.once(signal, async () => {
      await runtime.dispose();
      process.exit(0);
    });
}
