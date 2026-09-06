import {
  copyFileSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseJsonc } from "./resource-configs.mjs";

// Ordinary canonical sources -> isolated ordinary module files. No source tree
// writes, source symlinks, dependency upgrade or release-source guard exception.
export function preparePythonSource(projectDirectory, args) {
  let configPath = resolve(projectDirectory, "wrangler.jsonc");
  const commandArgs = [];
  let explicit = false;
  for (let i = 0; i < args.length; i++) {
    const value = args[i];
    if (
      value === "--config" ||
      value === "-c" ||
      value.startsWith("--config=")
    ) {
      if (explicit) throw new Error("Python source requires one configuration");
      explicit = true;
      const path = value.startsWith("--config=") ? value.slice(9) : args[++i];
      if (!path)
        throw new Error("Python source configuration path is required");
      configPath = resolve(projectDirectory, path);
    } else commandArgs.push(value);
  }
  const config = parseJsonc(readFileSync(configPath, "utf8"));
  const originalMain = resolve(dirname(configPath), config.main);
  if (originalMain !== resolve(projectDirectory, "src/entry.py"))
    throw new Error("Python source must use the project's owned entry.py");
  if (config.base_dir || config.build || config.assets || config.env)
    throw new Error(
      "Python source needs an explicit adapter for alternate build roots"
    );
  const stage = mkdtempSync(resolve(tmpdir(), "memweft-python-source-"));
  const close = () => rmSync(stage, { recursive: true, force: true });
  function copy(source, destination) {
    const entry = lstatSync(source);
    if (entry.isDirectory()) {
      mkdirSync(destination);
      for (const name of readdirSync(source)) {
        if (name === "__pycache__") continue;
        copy(resolve(source, name), resolve(destination, name));
      }
    } else if (entry.isFile()) copyFileSync(source, destination);
    else throw new Error("Python source must consist of ordinary owned files");
  }
  try {
    copy(resolve(projectDirectory, "src"), resolve(stage, "src"));
    const shared = resolve(dirname(projectDirectory), "shared");
    if (!lstatSync(shared).isDirectory())
      throw new Error(
        "Python shared source must be an ordinary owned directory"
      );
    for (const name of readdirSync(shared)) {
      if (!name.endsWith(".py")) continue;
      const destination = resolve(stage, "src", name);
      if (lstatSync(destination, { throwIfNoEntry: false }))
        throw new Error("Python shared module collides with project owner");
      copy(resolve(shared, name), destination);
    }
    if (basename(projectDirectory) === "api-core") {
      const repository = resolve(
        dirname(fileURLToPath(import.meta.url)),
        "../../.."
      );
      const projection = spawnSync(
        resolve(repository, "backend/.venv/bin/python"),
        [
          resolve(
            repository,
            "deploy/cloudflare/scripts/screen_frame_sources.py"
          ),
          "--output",
          resolve(stage, "src"),
        ],
        { encoding: "utf8" }
      );
      if (projection.status !== 0)
        throw new Error(
          "screenshot source projection failed; inspect the upstream contract owners"
        );
    }
    // Vendored immutable dependencies retain the existing staging link contract;
    // Wrangler's compiled/frozen payload is checked to contain regular files.
    symlinkSync(
      resolve(projectDirectory, "python_modules"),
      resolve(stage, "python_modules"),
      "dir"
    );
    for (const name of readdirSync(dirname(configPath))) {
      if (name === ".dev.vars" || name.startsWith(".dev.vars.")) {
        const source = resolve(dirname(configPath), name);
        if (!lstatSync(source).isFile())
          throw new Error("Python local secrets require an ordinary file");
        writeFileSync(resolve(stage, name), readFileSync(source), {
          mode: 0o600,
        });
      }
    }
    config.main = resolve(stage, "src", basename(originalMain));
    delete config.$schema;
    for (const db of config.d1_databases ?? [])
      if (db.migrations_dir)
        db.migrations_dir = resolve(dirname(configPath), db.migrations_dir);
    const projected = resolve(stage, "wrangler.json");
    writeFileSync(projected, JSON.stringify(config), { mode: 0o600 });
    return {
      args: [...commandArgs, "--config", projected],
      directory: stage,
      close,
    };
  } catch (error) {
    close();
    throw error;
  }
}
