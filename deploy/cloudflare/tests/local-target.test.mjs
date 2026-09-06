import { execFileSync } from "node:child_process";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { localConfigs } from "../contracts/local-config.mjs";
import { copyFrozenTarget } from "../contracts/frozen-local.mjs";
import { LocalProcesses } from "../contracts/local-process.mjs";
import {
  prepareLocalCache,
  startLocalTarget,
} from "../contracts/local-target.mjs";
import { readWorkerTemplates } from "../scripts/resource-configs.mjs";
import { WORKERS } from "../scripts/resource-input.mjs";
import { fileTree } from "../scripts/release-files.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const directories = [];
afterEach(() =>
  directories
    .splice(0)
    .forEach((path) => rmSync(path, { recursive: true, force: true }))
);
const inputs = {
  root,
  brandId: "fixture",
  brandRuntime: {
    brand_id: "fixture",
    display_name: "Atlas",
    ai_persona_name: "Mira",
  },
  firmwarePolicy: {
    schema_version: 1,
    brand_id: "fixture",
    device_model: "Atlas CV1",
    device_model_aliases: ["nrf5340"],
    release_tag_prefix: "Atlas_CV1_v",
    release_asset_prefix: "Atlas_CV1_OTA_v",
    github_releases_url: "https://api.github.com/repos/atlas/firmware/releases",
  },
  supportEmail: "support@atlas.example.invalid",
  shareOrigin: "http://127.0.0.1:34002",
  webOrigin: "http://127.0.0.1:34002",
  namespace: "local-fixture-123",
  port: 34000,
  asrPort: 34001,
};

describe("disposable actual Cloudflare target", () => {
  it("runs copied nine-owner artifacts with frozen SQL and rejects later payload drift", () => {
    const directory = mkdtempSync(resolve(tmpdir(), "cf-frozen-local-"));
    directories.push(directory);
    const source = resolve(directory, "candidate"),
      output = resolve(directory, "local");
    const templates = readWorkerTemplates(root);
    templates.web = {
      config: {
        name: "fixture-web",
        main: "worker.ts",
        compatibility_date: "2026-08-27",
        services: [{ binding: "EDGE", service: templates.edge.config.name }],
        assets: { directory: "assets", binding: "ASSETS" },
      },
    };
    const workers = {};
    for (const role of WORKERS) {
      const bundle = resolve(source, "workers", role);
      mkdirSync(resolve(bundle, "modules"), { recursive: true });
      const config = structuredClone(templates[role].config);
      config.main = "modules/frozen.js";
      config.base_dir = "modules";
      config.no_bundle = config.find_additional_modules = true;
      config.vars ??= {};
      if (role.startsWith("api-"))
        config.vars.BRAND_RUNTIME_JSON = JSON.stringify(inputs.brandRuntime);
      if (role === "api-core") {
        config.vars.BRAND_SUPPORT_EMAIL = inputs.supportEmail;
        config.vars.FIRMWARE_BRAND_POLICY_JSON = JSON.stringify(
          inputs.firmwarePolicy
        );
      }
      if (role === "web") {
        mkdirSync(resolve(bundle, "assets"));
        writeFileSync(resolve(bundle, "assets/brand.svg"), "frozen-asset");
      }
      writeFileSync(
        resolve(bundle, "modules/frozen.js"),
        `export default ${JSON.stringify(role)};`
      );
      writeFileSync(resolve(bundle, "wrangler.json"), JSON.stringify(config));
      workers[role] = {
        artifact: `workers/${role}`,
        config: `workers/${role}/wrangler.json`,
        name: config.name,
      };
    }
    for (const authority of ["auth", "app"]) {
      mkdirSync(resolve(source, "sql", authority), { recursive: true });
      writeFileSync(
        resolve(source, "sql", authority, "0001.sql"),
        "CREATE TABLE frozen (id TEXT);"
      );
    }
    let verifyFailure;
    const context = {
      directory: source,
      candidate: {
        brand: "fixture",
        workers,
        artifact_files: {
          workers: fileTree(resolve(source, "workers")),
          sql: fileTree(resolve(source, "sql")),
        },
      },
      verify() {
        if (verifyFailure) throw new Error(verifyFailure);
      },
    };
    const copied = copyFrozenTarget(context, output);
    const projected = localConfigs({
      ...inputs,
      ...copied,
      preservePolicy: false,
    });
    expect(copied.brandRuntime).toEqual(inputs.brandRuntime);
    expect(Object.keys(projected.configs)).toHaveLength(WORKERS.length + 1);
    expect(projected.configs.web.assets.directory).toBe(
      resolve(output, "workers/web/assets")
    );
    expect(projected.configs.edge.main).toBe(
      resolve(output, "workers/edge/modules/frozen.js")
    );
    expect(projected.configs.edge.base_dir).toBe(
      resolve(output, "workers/edge/modules")
    );
    expect(projected.configs["api-core"].d1_databases[0].migrations_dir).toBe(
      resolve(output, "sql/app")
    );
    expect(projected.configs.web.services[0].service).toBe(
      projected.configs.edge.name
    );
    writeFileSync(
      resolve(output, "workers/edge/wrangler.json"),
      JSON.stringify(projected.configs.edge)
    );
    writeFileSync(resolve(output, "workers/edge/.dev.vars"), "LOCAL=value\n");
    copied.verifyPayload();
    writeFileSync(resolve(output, "workers/web/assets/brand.svg"), "changed");
    expect(() => copied.verifyPayload()).toThrow(
      "local frozen payload changed: web"
    );
    writeFileSync(
      resolve(output, "workers/web/assets/brand.svg"),
      "frozen-asset"
    );
    writeFileSync(resolve(output, "sql/app/0001.sql"), "SELECT 1;");
    expect(() => copied.verifyPayload()).toThrow(
      "local frozen SQL changed: app"
    );
    verifyFailure = "candidate source changed";
    expect(() => copied.verifyPayload()).toThrow(verifyFailure);
  });
  it("refuses enabled migration or anonymous-client policies instead of weakening a frozen candidate", () => {
    for (const key of [
      "ACCOUNT_ACTIVATION_FENCE_ENABLED",
      "MCP_ALLOW_UNAUTHENTICATED_DCR",
      "CHAT_STAGING_ENABLED",
      "ORIGIN_BACKEND_URL",
    ]) {
      const templates = readWorkerTemplates(root);
      for (const entry of Object.values(templates)) {
        delete entry.config.vars?.ORIGIN_BACKEND_URL;
        if (entry.config.vars)
          entry.config.vars.MCP_ALLOW_UNAUTHENTICATED_DCR = "false";
        for (const name of Object.keys(entry.config.vars ?? {}))
          if (name.endsWith("_ENABLED")) entry.config.vars[name] = "false";
      }
      localConfigs({ ...inputs, templates, preservePolicy: true });
      templates.edge.config.vars[key] = "true";
      expect(() =>
        localConfigs({ ...inputs, templates, preservePolicy: true })
      ).toThrow(`policy adapter required for ${key}`);
    }
  });
  it("preserves native ownership policy in a frozen candidate", () => {
    for (const enabled of ["false", "true"]) {
      const templates = readWorkerTemplates(root);
      for (const entry of Object.values(templates)) {
        delete entry.config.vars?.ORIGIN_BACKEND_URL;
        for (const name of Object.keys(entry.config.vars ?? {}))
          if (
            name.endsWith("_ENABLED") ||
            name === "MCP_ALLOW_UNAUTHENTICATED_DCR"
          )
            entry.config.vars[name] = "false";
      }
      for (const role of ["edge", "realtime", "api-core"])
        templates[role].config.vars.ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED = enabled;
      const { configs } = localConfigs({
        ...inputs,
        templates,
        preservePolicy: true,
      });
      for (const role of ["edge", "realtime", "api-core"])
        expect(configs[role].vars.ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED).toBe(
          enabled
        );
    }
  });
  it("creates a private default cache and only reuses an explicitly existing ordinary cache", () => {
    const directory = mkdtempSync(resolve(tmpdir(), "cf-cache-"));
    directories.push(directory);
    for (const path of ["", " \t", null, true])
      expect(() =>
        prepareLocalCache(directory, { CLOUDFLARE_PYODIDE_CACHE_DIR: path })
      ).toThrow("nonempty path");
    const env = {};
    const cache = prepareLocalCache(directory, env);
    expect(cache).toEqual({
      directory: resolve(directory, "pyodide"),
      owner: "fixture",
    });
    expect(env.CLOUDFLARE_PYODIDE_CACHE_DIR).toBe(cache.directory);
    expect(prepareLocalCache(directory, env).owner).toBe("explicit-reuse");
    const broken = resolve(directory, "broken");
    symlinkSync(resolve(directory, "missing"), broken);
    for (const path of [broken, resolve(directory, "missing")])
      expect(() =>
        prepareLocalCache(directory, { CLOUDFLARE_PYODIDE_CACHE_DIR: path })
      ).toThrow("ordinary directory");
  });
  it("projects all eight backend owners and isolates every storage and queue binding", () => {
    const { configs, origin } = localConfigs(inputs);
    expect(configs["api-core"].vars.BRAND_SUPPORT_EMAIL).toBe(
      inputs.supportEmail
    );
    expect(configs["api-core"].vars.PUBLIC_WEB_BASE_URL).toBe(inputs.webOrigin);
    expect(configs["api-core"].vars.PUBLIC_SHARE_BASE_URL).toBe(
      inputs.shareOrigin
    );
    expect(
      JSON.parse(configs["api-core"].vars.FIRMWARE_BRAND_POLICY_JSON)
    ).toEqual(inputs.firmwarePolicy);
    expect(configs.auth.vars.ALLOWED_ORIGINS).toBe(inputs.webOrigin);
    expect(() => localConfigs({ ...inputs, supportEmail: undefined })).toThrow(
      /support contact/
    );
    expect(() =>
      localConfigs({
        ...inputs,
        shareOrigin: "https://remote.example.invalid",
      })
    ).toThrow(/share origin/);
    for (const role of ["api-core", "api-ai"])
      expect(JSON.parse(configs[role].vars.BRAND_RUNTIME_JSON)).toEqual(
        inputs.brandRuntime
      );
    expect(() => localConfigs({ ...inputs, brandRuntime: undefined })).toThrow(
      /brand runtime/
    );
    expect(() =>
      localConfigs({ ...inputs, firmwarePolicy: undefined })
    ).toThrow(/firmware policy/);
    expect(() =>
      localConfigs({
        ...inputs,
        brandRuntime: { ...inputs.brandRuntime, brand_id: "foreign" },
      })
    ).toThrow(/brand runtime/);
    expect(Object.keys(configs).sort()).toEqual([
      "api-ai",
      "api-core",
      "auth",
      "edge",
      "jobs",
      "provider",
      "rate-limit",
      "realtime",
      "screen-frame-writer",
    ]);
    const names = new Set(Object.values(configs).map((config) => config.name));
    const databases = new Map();
    const producers = new Set();
    const consumers = [];
    for (const [role, config] of Object.entries(configs)) {
      expect(config.account_id).toBeUndefined();
      expect(config.routes).toBeUndefined();
      expect(config.ai).toBeUndefined();
      expect(config.vars?.ORIGIN_BACKEND_URL).toBeUndefined();
      for (const service of config.services ?? []) {
        expect(names.has(service.service)).toBe(true);
        expect(service.remote).toBeUndefined();
      }
      for (const db of config.d1_databases ?? []) {
        const prior = databases.get(db.binding);
        if (prior) expect(db.database_id).toBe(prior);
        databases.set(db.binding, db.database_id);
        expect(db.database_name.startsWith(inputs.namespace)).toBe(true);
      }
      for (const binding of config.r2_buckets ?? [])
        expect(binding.bucket_name.startsWith(inputs.namespace)).toBe(true);
      for (const producer of config.queues?.producers ?? [])
        producers.add(producer.queue);
      for (const consumer of config.queues?.consumers ?? []) {
        consumers.push(consumer.queue);
        if (consumer.dead_letter_queue)
          producers.add(consumer.dead_letter_queue);
      }
      if (role !== "provider") {
        expect(config.vars.AUTH_JWT_ISSUER).toBe(origin);
        expect(config.vars.AUTH_JWT_AUDIENCE).toBe(origin);
      }
    }
    expect(databases.size).toBe(2);
    expect(new Set(databases.values()).size).toBe(2);
    expect(consumers.every((queue) => producers.has(queue))).toBe(true);
    expect(
      configs.realtime.services.some((binding) => binding.binding === "AI")
    ).toBe(false);
    expect(configs.realtime.vars.ASR_WS_URL).toBe("http://127.0.0.1:34001");
  });
  it("rejects unowned production binding drift and newly introduced runtime primitives", () => {
    expect(() => localConfigs({ ...inputs, brandId: null })).toThrow(
      "invalid local brand"
    );
    expect(() => localConfigs({ ...inputs, namespace: true })).toThrow(
      "invalid local brand"
    );
    const templates = readWorkerTemplates(root);
    templates.edge.config.services.push({
      binding: "NEW_SERVICE",
      service: "not-in-this-stack",
    });
    expect(() => localConfigs({ ...inputs, templates })).toThrow(
      "unowned Worker"
    );
    const changed = readWorkerTemplates(root);
    changed.edge.config.kv_namespaces = [{ binding: "REMOTE", id: "unowned" }];
    expect(() => localConfigs({ ...inputs, templates: changed })).toThrow(
      "local adapter required"
    );
  });
  it("never adopts an existing output owner or a broken link", async () => {
    const directory = mkdtempSync(resolve(tmpdir(), "cf-owner-"));
    directories.push(directory);
    writeFileSync(resolve(directory, "retained"), "user-owned");
    await expect(startLocalTarget({ output: directory })).rejects.toThrow(
      "fresh output owner"
    );
    symlinkSync(resolve(directory, "missing"), resolve(directory, "broken"));
    await expect(
      startLocalTarget({ output: resolve(directory, "broken") })
    ).rejects.toThrow("fresh output owner");
    expect(readFileSync(resolve(directory, "retained"), "utf8")).toBe(
      "user-owned"
    );
  });
  it("cancels its actual process tree and closes admission to later commands", async () => {
    const owner = new LocalProcesses();
    const server = createServer();
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const ready = new Promise((resolve) =>
      server.once("connection", (socket) =>
        socket.once("data", (data) => {
          socket.destroy();
          resolve(Number(data));
        })
      )
    );
    const code = `const {spawn}=require('node:child_process'); const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'inherit'}); const socket=require('node:net').connect(${
      server.address().port
    },'127.0.0.1',()=>socket.end(String(child.pid))); setInterval(()=>{},1000);`;
    const result = owner.run(process.execPath, ["-e", code], {
      stdio: "ignore",
    });
    try {
      const descendant = await ready;
      await owner.close();
      expect((await result).signal).toBe("SIGKILL");
      let status = "";
      try {
        status = execFileSync("ps", ["-p", String(descendant), "-o", "stat="], {
          encoding: "utf8",
        }).trim();
      } catch {}
      expect(!status || status.startsWith("Z")).toBe(true);
      expect(() =>
        owner.start(process.execPath, ["-e", "process.exit(0)"])
      ).toThrow("cancelled");
    } finally {
      await owner.close();
      await new Promise((resolve) => server.close(resolve));
    }
  });
  it("bounds a stalled actual tool command and reports failure rather than readiness", async () => {
    const owner = new LocalProcesses();
    try {
      const result = await owner.run(
        process.execPath,
        ["-e", "setInterval(()=>{},1000)"],
        { timeout: 100, stdio: "ignore" }
      );
      expect(result).toMatchObject({
        status: null,
        signal: "SIGKILL",
        timedOut: true,
      });
      const missing = await owner.run("/no/such/cf-tool", [], {
        stdio: "ignore",
      });
      expect(missing.status).toBeNull();
      expect(missing.error.code).toBe("ENOENT");
    } finally {
      await owner.close();
    }
  });
  it("reaps a completed tool's inherited-pipe descendants while its group leader is still owned", async () => {
    const owner = new LocalProcesses();
    const { child, completion } = owner.start(process.execPath, [
      "-e",
      `const child=require('node:child_process').spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'inherit'}); console.log(child.pid); process.exit(7);`,
    ]);
    let output = "";
    child.stdout.on("data", (data) => (output += data));
    try {
      expect(await completion).toMatchObject({
        status: 7,
        signal: null,
        timedOut: false,
      });
      const descendant = Number(output.trim());
      expect(descendant).toBeGreaterThan(1);
      let status = "";
      try {
        status = execFileSync("ps", ["-p", String(descendant), "-o", "stat="], {
          encoding: "utf8",
        }).trim();
      } catch {}
      expect(!status || status.startsWith("Z")).toBe(true);
    } finally {
      await owner.close();
    }
  });
  it("preserves a tool's extra control descriptor and keeps supervisor IPC separate", async () => {
    const owner = new LocalProcesses();
    const { child, completion } = owner.start(
      process.execPath,
      ["-e", "require('node:fs').writeSync(3,'actual control channel')"],
      { stdio: ["ignore", "ignore", "ignore", "pipe"] }
    );
    let control = "";
    child.stdio[3].on("data", (data) => (control += data));
    try {
      expect((await completion).status).toBe(0);
      expect(control).toBe("actual control channel");
    } finally {
      await owner.close();
    }
  });
  it("reports a denied cleanup without losing the tool failure or throwing from its event callback", async () => {
    let denied = true;
    const owner = new LocalProcesses({
      signal: (pid, signal) => {
        if (denied) {
          denied = false;
          throw Object.assign(new Error("fixture signal denied"), {
            code: "EPERM",
          });
        }
        return process.kill(pid, signal);
      },
    });
    try {
      const result = await owner.run(
        process.execPath,
        ["-e", "process.exit(9)"],
        { stdio: "ignore" }
      );
      expect(result).toMatchObject({
        status: null,
        error: { code: "EPERM" },
        toolResult: { status: 9 },
        timedOut: false,
      });
      expect(() => owner.start(process.execPath, [])).toThrow("cancelled");
    } finally {
      await owner.close();
    }
  });
});
