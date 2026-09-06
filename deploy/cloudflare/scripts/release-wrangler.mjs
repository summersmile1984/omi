import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { assertInstalledRuntime } from "./python-worker.mjs";

export const WRANGLER_PROCESS_TIMEOUT_MS = 15 * 60 * 1000;

// POSIX process groups include Wrangler's Node launcher child and any runner
// descendants. A timeout must end their ownership, not just the wrapper PID.
export function runReleaseProcess(
  command,
  args,
  options,
  { spawn = spawnSync } = {}
) {
  if (process.platform === "win32")
    throw new Error("release execution requires POSIX process-group ownership");
  const result = spawn(command, args, {
    ...options,
    detached: true,
    killSignal: "SIGKILL",
  });
  if (result.pid) {
    try {
      process.kill(-result.pid, "SIGKILL");
    } catch (error) {
      if (error.code !== "ESRCH")
        return { ...result, status: null, signal: null, error };
    }
  }
  return result;
}

const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
export function activeVersion(status) {
  if (
    !status ||
    status.versions?.length !== 1 ||
    status.versions[0].percentage !== 100 ||
    !UUID.test(status.versions[0].version_id)
  )
    throw new Error("exactly one observed 100% active UUID is required");
  return status.versions[0].version_id;
}

// This is the only release process/API owner. Subprocess and HTTP are injectable
// for fault tests; the CLI always constructs this adapter with locked tools.
export class WranglerReleaseAdapter {
  constructor({
    root,
    directory,
    candidate,
    spawn = spawnSync,
    fetchImpl = fetch,
    env = process.env,
  }) {
    this.root = root;
    this.directory = directory;
    this.candidate = candidate;
    this.spawn = spawn;
    this.fetch = fetchImpl;
    this.env = env;
    assertInstalledRuntime(resolve(root, "deploy/cloudflare"));
    this.bin = resolve(
      root,
      "deploy/cloudflare/node_modules/wrangler/bin/wrangler.js"
    );
    this.account = candidate.account_id;
    if (!/^[0-9a-f]{32}$/i.test(this.account))
      throw new Error("invalid release account");
  }
  command(args, input) {
    const result = runReleaseProcess(
      process.execPath,
      [this.bin, ...args],
      {
        timeout: WRANGLER_PROCESS_TIMEOUT_MS,
        cwd: this.directory,
        encoding: "utf8",
        input,
        maxBuffer: 32 * 1024 * 1024,
        env: {
          ...this.env,
          CLOUDFLARE_ACCOUNT_ID: this.account,
          WRANGLER_SEND_METRICS: "false",
          CI: "true",
          NPM_CONFIG_OFFLINE: "true",
        },
        stdio: [input === undefined ? "ignore" : "pipe", "pipe", "pipe"],
      },
      { spawn: this.spawn }
    );
    // CLI output can contain vars, provider errors or credentials. It never
    // enters the release journal. A failed process has an unknown remote result.
    return { exit: result.status ?? null, signal: result.signal ?? null };
  }
  async api(path, { method = "GET", body, absentWorker = false } = {}) {
    if (!this.env.CLOUDFLARE_API_TOKEN)
      throw new Error("Cloudflare API credential is unavailable");
    let response, json;
    try {
      response = await this.fetch(
        `https://api.cloudflare.com/client/v4${
          path.startsWith("/") ? path : `/accounts/${this.account}/${path}`
        }`,
        {
          method,
          redirect: "error",
          signal: AbortSignal.timeout(30_000),
          headers: {
            Authorization: `Bearer ${this.env.CLOUDFLARE_API_TOKEN}`,
            "Content-Type": "application/json",
          },
          ...(body ? { body: JSON.stringify(body) } : {}),
        }
      );
      json = await response.json();
    } catch {
      throw new Error("Cloudflare observation outcome is unknown");
    }
    if (
      absentWorker &&
      response.status === 404 &&
      json.errors?.some((error) => error.code === 10007)
    )
      return null;
    if (!response.ok || json.success !== true)
      throw new Error(
        `Cloudflare observation failed (HTTP ${response.status})`
      );
    return json;
  }
  async list(kind) {
    const paths = {
      d1: "d1/database",
      r2: "r2/buckets",
      queue: "queues",
      vectorize: "vectorize/v2/indexes",
    };
    if (!paths[kind]) throw new Error("unknown resource list owner");
    let page = 1,
      cursor,
      all = [];
    for (;;) {
      const query = cursor
        ? `cursor=${encodeURIComponent(cursor)}`
        : `page=${page}&per_page=1000`;
      const response = await this.api(`${paths[kind]}?${query}`);
      const rows = Array.isArray(response.result)
        ? response.result
        : response.result?.buckets ??
          response.result?.queues ??
          response.result?.indexes;
      if (!Array.isArray(rows))
        throw new Error("resource inventory response is incomplete");
      all.push(...rows);
      const next = response.result_info?.cursor ?? response.result?.cursor;
      if (next) {
        if (next === cursor) throw new Error("resource pagination stalled");
        cursor = next;
      } else if (response.result_info?.total_pages > page) page++;
      else {
        if (rows.length >= 1000 && !response.result_info?.total_pages)
          throw new Error("resource inventory pagination is ambiguous");
        return all;
      }
      if (page > 1000 || all.length > 100_000)
        throw new Error("resource inventory exceeds bounded observation");
    }
  }
  async observeResource(resource) {
    const matches = (await this.list(resource.kind)).filter(
      (entry) => (entry.name ?? entry.queue_name) === resource.name
    );
    if (matches.length > 1)
      throw new Error("resource name has multiple remote owners");
    if (!matches.length) return { status: "absent" };
    const row = matches[0];
    if (resource.kind === "d1" && !UUID.test(row.uuid))
      throw new Error("D1 identity is invalid");
    if (
      resource.kind === "vectorize" &&
      (row.config?.dimensions !== resource.dimensions ||
        row.config?.metric !== resource.metric)
    )
      throw new Error(
        "Vectorize dimensions or metric differ from the resource plan"
      );
    return {
      status: "present",
      name: resource.name,
      id: row.uuid ?? row.queue_id ?? row.name,
    };
  }
  async create(resource) {
    // A successful create response proves ownership; list-before/list-after
    // alone could accidentally adopt a concurrently created resource.
    const paths = {
      d1: "d1/database",
      r2: "r2/buckets",
      queue: "queues",
      vectorize: "vectorize/v2/indexes",
    };
    if (!paths[resource.kind])
      throw new Error("only explicit storage resources can be provisioned");
    const body =
      resource.kind === "queue"
        ? { queue_name: resource.name }
        : resource.kind === "vectorize"
        ? {
            name: resource.name,
            config: {
              dimensions: resource.dimensions,
              metric: resource.metric,
            },
          }
        : { name: resource.name };
    const result = (
      await this.api(paths[resource.kind], { method: "POST", body })
    ).result;
    const id = result?.uuid ?? result?.queue_id ?? result?.name;
    if (!id) throw new Error("resource create did not return an identity");
    return { created_id: id };
  }
  policies() {
    const resources = this.candidate.resource_plan.resources;
    return [
      ...["conversations", "transcript-chunks", "screen-activity"].map(
        (role) => ({
          kind: "vectorize",
          name: resources.find((entry) => entry.key === `vectorize:${role}`)
            .name,
          id: "created_at",
          type: "number",
        })
      ),
      ...[
        ["expire-staged-transcriptions", "cf-transcriptions/"],
        ["expire-staged-sync", "cf-sync/"],
      ].map(([id, prefix]) => ({
        kind: "r2",
        name: resources.find((entry) => entry.key === "r2:assets").name,
        id,
        prefix,
        seconds: 86400,
      })),
      {
        kind: "r2",
        name: resources.find(
          (entry) => entry.key === "r2:frame-requests-temporary"
        ).name,
        id: "expire-temporary-frames",
        prefix: "",
        seconds: 604800,
        exclusive: true,
      },
      {
        kind: "r2",
        name: resources.find((entry) => entry.key === "r2:frame-requests").name,
        id: "retain-conversation-frames",
        retain: true,
      },
    ];
  }
  async observePolicy(policy) {
    if (policy.kind === "vectorize") {
      const result = (
        await this.api(
          `vectorize/v2/indexes/${policy.name}/metadata_index/list`
        )
      ).result;
      const rows = result?.metadataIndexes;
      if (!Array.isArray(rows))
        throw new Error("Vectorize metadata observation is incomplete");
      const entry = rows.find((row) => row.propertyName === policy.id);
      // The API documents lowercase values but production also returns the
      // title-case enum (observed during Eddy provisioning on 2026-09-05).
      const observedType = new Map([
        ["number", "number"],
        ["Number", "number"],
        ["string", "string"],
        ["String", "string"],
        ["boolean", "boolean"],
        ["Boolean", "boolean"],
      ]).get(entry?.indexType);
      if (entry && (!observedType || observedType !== policy.type))
        throw new Error("Vectorize metadata owner differs");
      return entry ? { status: "present" } : { status: "absent" };
    }
    const rules = (await this.api(`r2/buckets/${policy.name}/lifecycle`)).result
      ?.rules;
    if (!Array.isArray(rules))
      throw new Error("R2 lifecycle observation is incomplete");
    if (policy.retain) {
      if (
        rules.some(
          (rule) => rule.enabled === true && rule.deleteObjectsTransition
        )
      )
        throw new Error("conversation frame bucket must not expire objects");
      return { status: "present" };
    }
    if (
      policy.exclusive &&
      rules.some(
        (rule) =>
          rule.enabled === true &&
          rule.deleteObjectsTransition &&
          rule.id !== policy.id
      )
    )
      throw new Error("temporary frame bucket has an unowned expiration rule");
    const row = rules.find((rule) => rule.id === policy.id);
    if (
      row &&
      (row.enabled !== true ||
        row.conditions?.prefix !== policy.prefix ||
        row.deleteObjectsTransition?.condition?.type !== "Age" ||
        row.deleteObjectsTransition.condition.maxAge !== policy.seconds)
    )
      throw new Error("R2 lifecycle owner differs");
    return row ? { status: "present" } : { status: "absent" };
  }
  addPolicy(policy) {
    if (
      policy.kind === "r2" &&
      (policy.retain ||
        !Number.isSafeInteger(policy.seconds / 86400) ||
        policy.seconds <= 0)
    )
      throw new Error(
        "R2 expiration requires an explicit positive whole-day duration"
      );
    return this.command(
      policy.kind === "vectorize"
        ? [
            "vectorize",
            "create-metadata-index",
            policy.name,
            "--propertyName",
            policy.id,
            "--type",
            policy.type,
          ]
        : [
            "r2",
            "bucket",
            "lifecycle",
            "add",
            policy.name,
            policy.id,
            policy.prefix,
            "--expire-days",
            String(policy.seconds / 86400),
          ]
    );
  }
  async waitForPolicy(
    policy,
    {
      attempts = 10,
      retryDelayMs = 2000,
      sleep = (ms) => new Promise((done) => setTimeout(done, ms)),
    } = {}
  ) {
    if (
      !Number.isInteger(attempts) ||
      attempts < 1 ||
      attempts > 30 ||
      !Number.isInteger(retryDelayMs) ||
      retryDelayMs < 0 ||
      retryDelayMs > 5000
    )
      throw new Error("invalid policy observation bounds");
    // Creation is asynchronous. Only an authoritative absence is polled;
    // transport/permission/type failures remain unknown and stop immediately.
    for (let attempt = 0; attempt < attempts; attempt++) {
      const observed = await this.observePolicy(policy);
      if (observed.status === "present") return observed;
      if (attempt + 1 < attempts) await sleep(retryDelayMs);
    }
    throw new Error(
      "created policy did not become observable within the deadline"
    );
  }
  async preconditions() {
    for (const refs of Object.values(this.candidate.resource_plan.secrets))
      for (const reference of Object.values(refs))
        if (
          typeof this.env[reference] !== "string" ||
          this.env[reference].length < 32
        )
          throw new Error(
            `required secret reference is unavailable: ${reference}`
          );
    for (const policy of this.policies())
      if ((await this.observePolicy(policy)).status !== "present")
        throw new Error(
          "resource policy must be provisioned and observed before publishing"
        );
    const input = this.candidate.inventory;
    if (input.routing.mode === "workers_dev") {
      const observed = (await this.api("workers/subdomain")).result?.subdomain;
      if (observed !== input.routing.workers_subdomain)
        throw new Error("workers.dev account subdomain differs from profile");
    } else {
      const response = await this.api("workers/domains");
      if (!Array.isArray(response.result))
        throw new Error("custom-domain ownership observation is incomplete");
      const zones = [];
      for (let page = 1; ; page++) {
        const observed = await this.api(
          `/zones?account.id=${this.account}&status=active&per_page=50&page=${page}`
        );
        if (
          !Array.isArray(observed.result) ||
          !Number.isInteger(observed.result_info?.total_pages)
        )
          throw new Error("zone ownership observation is incomplete");
        zones.push(...observed.result);
        if (page >= observed.result_info.total_pages) break;
        if (page >= 1000)
          throw new Error("zone ownership exceeds bounded observation");
      }
      for (const { config } of Object.values(
        this.candidate.resource_plan.configs
      ))
        for (const route of config.routes ?? []) {
          const matches = response.result.filter(
            (row) => row.hostname === route.pattern
          );
          if (
            matches.length > 1 ||
            matches.some((row) => row.service !== config.name)
          )
            throw new Error(
              "custom domain belongs to a different Worker owner"
            );
          if (
            !zones.some(
              (zone) =>
                zone.account?.id === this.account &&
                (route.pattern === zone.name ||
                  route.pattern.endsWith(`.${zone.name}`))
            )
          )
            throw new Error(
              "custom domain has no observed active zone in the release account"
            );
        }
    }
    return { observed: true };
  }
  async observeWorker(name) {
    const response = await this.api(
      `workers/scripts/${encodeURIComponent(name)}/deployments`,
      { absentWorker: true }
    );
    if (response === null) return { status: "absent" };
    const version = activeVersion(response.result?.deployments?.[0]);
    const detail = await this.api(
      `workers/scripts/${encodeURIComponent(name)}/versions/${version}`
    );
    return {
      status: "present",
      version,
      tag: detail.result?.annotations?.["workers/tag"] ?? null,
      message: detail.result?.annotations?.["workers/message"] ?? null,
    };
  }
  async migrationLedger(authority) {
    const query = async (sql) => {
      const result = (
        await this.api(`d1/database/${authority.database_id}/query`, {
          method: "POST",
          body: { sql },
        })
      ).result;
      if (
        !Array.isArray(result) ||
        result.length !== 1 ||
        result[0].success !== true ||
        !Array.isArray(result[0].results)
      )
        throw new Error("D1 ledger query is incomplete");
      return result[0].results;
    };
    const tables = await query(
      "SELECT name FROM sqlite_master WHERE type='table' AND name='d1_migrations'"
    );
    return tables.length
      ? (await query("SELECT name FROM d1_migrations ORDER BY id")).map(
          (row) => row.name
        )
      : [];
  }
  migrate(authority) {
    return this.command([
      "d1",
      "migrations",
      "apply",
      authority.binding,
      "--remote",
      "--config",
      resolve(this.directory, `migrations/${authority.authority}.json`),
    ]);
  }
  deploy(role, tag, message) {
    const worker = this.candidate.workers[role],
      refs = this.candidate.resource_plan.secrets[role],
      values = {};
    for (const [binding, reference] of Object.entries(refs)) {
      if (
        typeof this.env[reference] !== "string" ||
        this.env[reference].length < 32
      )
        throw new Error(
          `required secret reference is unavailable: ${reference}`
        );
      values[binding] = this.env[reference];
    }
    const temporary = mkdtempSync(resolve(tmpdir(), "cf-release-secrets-"));
    try {
      const path = resolve(temporary, "secrets.json");
      writeFileSync(path, JSON.stringify(values), { mode: 0o600 });
      return this.command([
        "deploy",
        "--config",
        resolve(this.directory, worker.config),
        "--no-bundle",
        "--strict",
        "--tag",
        tag,
        "--message",
        message,
        ...(Object.keys(values).length ? ["--secrets-file", path] : []),
      ]);
    } finally {
      rmSync(temporary, { recursive: true, force: true });
    }
  }
  rollback(name, version, transaction) {
    if (!UUID.test(version)) throw new Error("invalid retained Worker version");
    return this.command([
      "rollback",
      version,
      "--name",
      name,
      "--message",
      `release recovery ${transaction}`.slice(0, 120),
      "--yes",
    ]);
  }
  async readiness({
    attempts = 6,
    retryDelayMs = 2000,
    sleep = (ms) => new Promise((done) => setTimeout(done, ms)),
  } = {}) {
    const origins = this.candidate.resource_plan.origins;
    if (!Number.isInteger(attempts) || attempts < 1)
      throw new Error("readiness attempts must be positive");
    for (const url of [
      `${origins.api}/ready`,
      `${origins.web}/api/worker-ready`,
    ]) {
      for (let attempt = 1; attempt <= attempts; attempt++) {
        let response, body;
        try {
          response = await this.fetch(url, {
            signal: AbortSignal.timeout(15_000),
            redirect: "error",
          });
          body = await response.json();
        } catch {
          response = undefined;
        }
        if (response?.status === 200 && body?.status === "ready") break;
        if (attempt === attempts)
          throw new Error("release readiness did not report ready JSON");
        await sleep(retryDelayMs);
      }
    }
    return { edge: "ready", web: "ready" };
  }
}
