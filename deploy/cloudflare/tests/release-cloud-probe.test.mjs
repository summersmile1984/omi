import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { WORKERS } from "../scripts/resource-input.mjs";
import { probeConfiguration, qualifyCloudRuntime } from "../scripts/release-cloud-probe.mjs";

const directories = [];
afterEach(() => directories.splice(0).forEach((directory) => rmSync(directory, { recursive: true, force: true })));
function fixture({ failedRole, foreignVersion, failedSchema, failedHealth, occupiedName, failedMigration, catalogDrift } = {}) {
  const directory = mkdtempSync(resolve(tmpdir(), "release-cloud-probe-"));
  directories.push(directory);
  const states = new Map(), databases = new Map(), removedDatabases = [], removed = [], uploaded = [], configs = [], requests = [];
  const catalog = [{ type: "table", name: "fixture", tbl_name: "fixture", sql: "CREATE TABLE fixture(id TEXT PRIMARY KEY)" }];
  const candidate = {
    account_id: "a".repeat(32), candidate_digest: "b".repeat(64), source: { commit: "c".repeat(40) },
    stage: "beta", workers: {}, inventory: { d1_ids: { app: "serving-app", auth: "serving-auth" } },
    resource_plan: { resources: [], secrets: {}, deploy_order: [], migrations: [] },
  };
  for (const role of WORKERS) {
    const name = `eddy-${role}-beta`;
    const config = `${role}.json`;
    candidate.workers[role] = { name, config, sha256: "d".repeat(64) };
    candidate.resource_plan.secrets[role] = { INTERNAL: "ORIGINAL_SECRET" };
    candidate.resource_plan.deploy_order.push(name);
    writeFileSync(resolve(directory, `${role}.js`), `export default {fetch: () => new Response("${role}")};\n`);
    writeFileSync(resolve(directory, config), JSON.stringify({
      name, main: `${role}.js`, compatibility_date: "2026-09-01",
      vars: { ORIGINAL: "runtime-variable" },
      routes: [{ pattern: "api.example.test", custom_domain: true }],
      triggers: { crons: ["* * * * *"] },
      queues: { consumers: [{ queue: "live" }], producers: [{ binding: "QUEUE", queue: "live" }] },
      d1_databases: [{ binding: "DB", database_id: "serving-app" }],
      services: [{ binding: "AUTH", service: "eddy-auth-beta" }],
    }));
  }
  mkdirSync(resolve(directory, "migrations"));
  for (const authority of ["app", "auth"]) {
    candidate.resource_plan.migrations.push({ authority, database_id: `serving-${authority}`, binding: "DB", files: [{ name: "001_fixture.sql", sha256: "e".repeat(64) }] });
    writeFileSync(resolve(directory, `migrations/${authority}.json`), JSON.stringify({
      name: "eddy-auth-beta", d1_databases: [{ database_id: `serving-${authority}`, migrations_dir: `../sql/${authority}` }],
    }));
  }
  const verify = vi.fn();
  const adapterFactory = ({ candidate: selected, env }) => ({
    async preconditions() {},
    async observeWorker(name) {
      if (occupiedName && name.startsWith("eddy-ci-") && !uploaded.length)
        return { status: "present", version: "preexisting", tag: "external" };
      const value = states.get(name) ?? { status: "absent" };
      if (foreignVersion && name.endsWith(`-${failedRole}`) && uploaded.length)
        return { ...value, status: "present", tag: "external", version: "external" };
      return value;
    },
    deploy(role, tag, message) {
      const worker = selected.workers[role];
      const config = JSON.parse(readFileSync(worker.config, "utf8"));
      expect(config.name).toBe(worker.name);
      if (role !== "probe") {
        expect(config.workers_dev).toBe(false);
        expect(config.preview_urls).toBe(false);
        expect(config.routes).toEqual([]);
        expect(config.triggers.crons).toEqual([]);
        expect(config.queues.consumers).toEqual([]);
        expect(config.queues.producers[0].queue).toBe("live");
        expect(config.vars).toEqual({ ORIGINAL: "runtime-variable" });
        expect(config.d1_databases[0].database_id).toBe(databases.values().next().value.id);
        expect(config.services[0].service).toMatch(/^eddy-ci-.*-auth$/);
        expect(readFileSync(config.main, "utf8")).toContain(`new Response("${role}")`);
        expect(env.ORIGINAL_SECRET).toBe("synthetic-existing-secret");
      }
      configs.push(config);
      uploaded.push(worker.name);
      states.set(worker.name, { status: "present", version: `version-${role}`, tag, message });
      // Reproduce a remote mutation succeeding before the launcher reports a
      // failure, so cleanup has to observe ownership rather than trust exit=0.
      return { exit: role === failedRole ? 1 : 0 };
    },
    async observeResource(resource) {
      return databases.get(resource.name) ?? { status: "absent" };
    },
    async create(resource) {
      const id = `10000000-0000-4000-8000-00000000000${databases.size + 1}`;
      databases.set(resource.name, { status: "present", name: resource.name, id });
      return { created_id: id };
    },
    migrate(authority) {
      expect([...databases.values()].map((item) => item.id)).toContain(authority.database_id);
      return { exit: failedMigration ? 1 : 0 };
    },
    async migrationLedger(authority) { return authority.files.map((file) => file.name); },
    async api(path, options) {
      if (path === "workers/subdomain") return { result: { subdomain: "fixture" } };
      if (path.startsWith("d1/database/")) {
        const id = path.split("/")[2];
        const owned = [...databases.values()].find((item) => item.id === id);
        expect(owned).toBeDefined();
        if (options.method === "DELETE") {
          removedDatabases.push(id);
          databases.delete(owned.name);
          return { success: true };
        }
        expect(options.method).toBe("POST");
        const rows = options.body.sql.startsWith("PRAGMA") ? [] : catalogDrift ? [] : catalog;
        return { result: [{ success: true, results: rows }] };
      }
      expect(options.method).toBe("DELETE");
      const name = path.split("/").at(-1).split("?")[0];
      expect(name).toMatch(/^eddy-ci-/);
      expect(candidate.resource_plan.deploy_order).not.toContain(name);
      removed.push(name);
      states.delete(name);
      return { success: true };
    },
  });
  const journalDirectory = resolve(directory, "journal");
  const options = {
    journalDirectory, adapterFactory,
    env: { ORIGINAL_SECRET: "synthetic-existing-secret" },
    qualifySchema: async () => {
      if (failedSchema) throw new Error("prior schema did not qualify");
      return { cases: [{ id: "schema.executed", result: "pass" }] };
    },
    frozenSchema: () => ["app", "auth"].map((authority) => ({ authority, schema_catalog: catalog })),
    fetchProbe: async (url, options) => {
      requests.push(url);
      expect(options.headers["x-release-probe"]).toMatch(/^[0-9a-f]{64}$/);
      expect(options.redirect).toBe("manual");
      return new Response(null, { status: failedHealth ? 503 : 200 });
    },
  };
  return { directory, context: { directory, root: directory, candidate, verify }, options, uploaded, removed, removedDatabases, configs, requests,
    journal: () => JSON.parse(readFileSync(resolve(journalDirectory, "journal.json"), "utf8")) };
}

describe("frozen Cloudflare release CI rehearsal", () => {
  it("uploads every unchanged module with its runtime bindings, probes cold/warm requests and removes only owned Workers", async () => {
    const f = fixture();
    const result = await qualifyCloudRuntime(f.context, f.options);
    expect(result.artifact_qualified).toBe(true);
    expect(result.release_ready).toBe(false);
    expect(result.cases).toHaveLength(17);
    expect(f.uploaded).toHaveLength(10);
    expect(f.removed).toEqual([...f.uploaded].reverse());
    expect(f.requests).toHaveLength(10);
    expect(result.cleanup.every((entry) => entry.result === "pass")).toBe(true);
    expect(JSON.stringify(result)).not.toContain("synthetic-existing-secret");
    expect(f.removedDatabases).toHaveLength(2);
    expect(f.context.verify).toHaveBeenCalledTimes(14);
    const code = readFileSync(f.configs.at(-1).main);
    const { default: gateway } = await import(`data:text/javascript;base64,${code.toString("base64")}`);
    const forwarded = [];
    const env = { PROBE_TOKEN: "synthetic-token", api_core: {
      fetch: async (url) => { forwarded.push(url); return new Response("healthy"); },
    } };
    for (const [path, method, token, expected] of [
      ["api-core", "GET", "", 404], ["api-core", "POST", "synthetic-token", 404],
      ["unknown", "GET", "synthetic-token", 404], ["api-core", "GET", "synthetic-token", 200],
    ]) {
      const response = await gateway.fetch(new Request(`https://fixture/${path}`, { method, headers: { "x-release-probe": token } }), env);
      expect(response.status).toBe(expected);
    }
    expect(forwarded).toEqual(["https://release-ci.internal/health"]);
  });
  it("rejects the Core upload failure and cleans up a remotely created version even when its process failed", async () => {
    const f = fixture({ failedRole: "api-core" });
    await expect(qualifyCloudRuntime(f.context, f.options)).rejects.toThrow("cloud runtime upload did not qualify: api-core");
    expect(f.journal().artifact_qualified).toBe(false);
    expect(f.removed).toEqual([...f.uploaded].reverse());
    expect(f.requests).toEqual([]);
  });
  it("refuses existing names, incompatible schema and unhealthy runtime responses", async () => {
    for (const setting of [{ occupiedName: true }, { failedSchema: true }, { failedHealth: true }, { failedMigration: true }, { catalogDrift: true }]) {
      const f = fixture(setting);
      await expect(qualifyCloudRuntime(f.context, f.options)).rejects.toThrow();
      expect(f.journal().artifact_qualified).toBe(false);
      if (!setting.failedHealth) expect(f.uploaded).toEqual([]);
      expect(f.removed).toEqual([...f.uploaded].reverse());
    }
  });
  it("retains ambiguous external ownership for reconciliation instead of deleting it", async () => {
    const f = fixture({ failedRole: "api-core", foreignVersion: true });
    await expect(qualifyCloudRuntime(f.context, f.options)).rejects.toThrow();
    expect(f.removed.some((name) => name.endsWith("-api-core"))).toBe(false);
    expect(f.journal().cleanup.some((entry) => entry.result === "reconciliation-required")).toBe(true);
  });
  it("does not install external event/DO ownership in a private rehearsal", () => {
    for (const extra of [
      { migrations: [{ tag: "v2", transferred_classes: [{ from: "External", to: "Private", from_script: "live-worker" }] }] },
      { tail_consumers: [{ service: "live-worker" }] },
      { workflows: [{ name: "live-workflow" }] },
    ]) expect(() => probeConfiguration({ name: "live", ...extra }, { live: "private" }, "/tmp")).toThrow();
    const config = probeConfiguration({
      name: "live", main: "module.js", assets: { directory: "assets" },
      durable_objects: { bindings: [{ name: "SELF", class_name: "Owned" }, { name: "SHARED", script_name: "other" }] },
    }, { live: "private", other: "other-private" }, "/tmp/frozen");
    expect(config.durable_objects.bindings).toEqual([{ name: "SELF", class_name: "Owned" }, { name: "SHARED", script_name: "other-private" }]);
    expect(config.assets.directory).toBe("/tmp/frozen/assets");
  });
});
