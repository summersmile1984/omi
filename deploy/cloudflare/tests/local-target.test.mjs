import { execFileSync } from "node:child_process";
import {
  mkdtempSync,
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
import { LocalProcesses } from "../contracts/local-process.mjs";
import {
  prepareLocalCache,
  startLocalTarget,
} from "../contracts/local-target.mjs";
import { readWorkerTemplates } from "../scripts/resource-configs.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const directories = [];
afterEach(() =>
  directories
    .splice(0)
    .forEach((path) => rmSync(path, { recursive: true, force: true })),
);
const inputs = {
  root,
  brandId: "fixture",
  brandRuntime: {
    brand_id: "fixture",
    display_name: "Atlas",
    ai_persona_name: "Mira",
  },
  supportEmail: "support@atlas.example.invalid",
  shareOrigin: "http://127.0.0.1:34002",
  webOrigin: "http://127.0.0.1:34002",
  namespace: "local-fixture-123",
  port: 34000,
  asrPort: 34001,
};

describe("disposable actual Cloudflare target", () => {
  it("creates a private default cache and only reuses an explicitly existing ordinary cache", () => {
    const directory = mkdtempSync(resolve(tmpdir(), "cf-cache-"));
    directories.push(directory);
    for (const path of ["", " \t", null, true])
      expect(() =>
        prepareLocalCache(directory, { CLOUDFLARE_PYODIDE_CACHE_DIR: path }),
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
        prepareLocalCache(directory, { CLOUDFLARE_PYODIDE_CACHE_DIR: path }),
      ).toThrow("ordinary directory");
  });
  it("projects all seven production owners and isolates every storage and queue binding", () => {
    const { configs, origin } = localConfigs(inputs);
    expect(configs["api-core"].vars.BRAND_SUPPORT_EMAIL).toBe(
      inputs.supportEmail,
    );
    expect(configs["api-core"].vars.PUBLIC_SHARE_BASE_URL).toBe(
      inputs.shareOrigin,
    );
    expect(configs.auth.vars.ALLOWED_ORIGINS).toBe(inputs.webOrigin);
    expect(() => localConfigs({ ...inputs, supportEmail: undefined })).toThrow(
      /support contact/,
    );
    expect(() =>
      localConfigs({
        ...inputs,
        shareOrigin: "https://remote.example.invalid",
      }),
    ).toThrow(/share origin/);
    for (const role of ["api-core", "api-ai"])
      expect(JSON.parse(configs[role].vars.BRAND_RUNTIME_JSON)).toEqual(
        inputs.brandRuntime,
      );
    expect(() => localConfigs({ ...inputs, brandRuntime: undefined })).toThrow(
      /brand runtime/,
    );
    expect(() =>
      localConfigs({
        ...inputs,
        brandRuntime: { ...inputs.brandRuntime, brand_id: "foreign" },
      }),
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
      configs.realtime.services.some((binding) => binding.binding === "AI"),
    ).toBe(false);
    expect(configs.realtime.vars.ASR_WS_URL).toBe("http://127.0.0.1:34001");
  });
  it("rejects unowned production binding drift and newly introduced runtime primitives", () => {
    expect(() => localConfigs({ ...inputs, brandId: null })).toThrow(
      "invalid local brand",
    );
    expect(() => localConfigs({ ...inputs, namespace: true })).toThrow(
      "invalid local brand",
    );
    const templates = readWorkerTemplates(root);
    templates.edge.config.services.push({
      binding: "NEW_SERVICE",
      service: "not-in-this-stack",
    });
    expect(() => localConfigs({ ...inputs, templates })).toThrow(
      "unowned Worker",
    );
    const changed = readWorkerTemplates(root);
    changed.edge.config.kv_namespaces = [{ binding: "REMOTE", id: "unowned" }];
    expect(() => localConfigs({ ...inputs, templates: changed })).toThrow(
      "local adapter required",
    );
  });
  it("never adopts an existing output owner or a broken link", async () => {
    const directory = mkdtempSync(resolve(tmpdir(), "cf-owner-"));
    directories.push(directory);
    writeFileSync(resolve(directory, "retained"), "user-owned");
    await expect(startLocalTarget({ output: directory })).rejects.toThrow(
      "fresh output owner",
    );
    symlinkSync(resolve(directory, "missing"), resolve(directory, "broken"));
    await expect(
      startLocalTarget({ output: resolve(directory, "broken") }),
    ).rejects.toThrow("fresh output owner");
    expect(readFileSync(resolve(directory, "retained"), "utf8")).toBe(
      "user-owned",
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
        }),
      ),
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
        owner.start(process.execPath, ["-e", "process.exit(0)"]),
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
        { timeout: 100, stdio: "ignore" },
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
      { stdio: ["ignore", "ignore", "ignore", "pipe"] },
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
        { stdio: "ignore" },
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
