import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { readJson, verifyCandidate, writeJson } from "./release-files.mjs";
import { digest, WORKERS } from "./resource-input.mjs";
import { WranglerReleaseAdapter } from "./release-wrangler.mjs";
import { observeReleaseCandidate } from "./release-transaction.mjs";
import { continuationContext, deliveryContinuation } from "./release-continuation.mjs";
import { qualifyFirstRelease, executeFrozenSchema, querySchema, comparableSchemaCatalog, SCHEMA_QUERY } from "../contracts/qualify-prior-schema.mjs";
import { qualificationContext } from "../contracts/qualification-context.mjs";

const HEALTH = { auth: "/ready", "api-core": "/health", "api-ai": "/health", edge: "/ready", web: "/login" };
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const PROBE_SECRET = "CF_RELEASE_PROBE_TOKEN";
const gatewaySource = `export default {
  async fetch(request, env) {
    const paths = ${JSON.stringify(HEALTH)};
    const role = new URL(request.url).pathname.slice(1);
    if (request.method !== "GET" || request.headers.get("x-release-probe") !== env.PROBE_TOKEN || !Object.hasOwn(paths, role))
      return new Response(null, {status: 404});
    return env[role.replaceAll("-", "_")].fetch("https://release-ci.internal" + paths[role]);
  }
};\n`;

// This is a rehearsal of the existing frozen publisher, not a second builder.
// Cloudflare beta run 34373519437 uploaded three Workers before Core's actual
// cloud initialization failed. Local no-bundle dry-runs did not catch that.
export function probeConfiguration(config, names, directory) {
  const result = structuredClone(config);
  if (!names[result.name]) throw new Error("probe Worker is outside the candidate");
  for (const migration of result.migrations ?? [])
    if (Object.keys(migration).some((key) => ![
      "tag", "new_classes", "new_sqlite_classes", "deleted_classes", "renamed_classes",
    ].includes(key))) throw new Error("probe cannot transfer external Durable Object ownership");
  result.name = names[result.name];
  result.workers_dev = false;
  result.preview_urls = false;
  result.routes = [];
  result.triggers = { crons: [] };
  if (result.queues) result.queues.consumers = [];
  if (result.tail_consumers?.length || result.workflows?.length)
    throw new Error("probe cannot install live event consumers");
  for (const service of result.services ?? []) {
    if (!names[service.service]) throw new Error("probe service is outside the candidate");
    service.service = names[service.service];
  }
  for (const binding of result.durable_objects?.bindings ?? []) {
    if (!binding.script_name) continue;
    if (!names[binding.script_name]) throw new Error("probe Durable Object is outside the candidate");
    binding.script_name = names[binding.script_name];
  }
  for (const key of ["main", "base_dir"])
    if (result[key]) result[key] = resolve(directory, result[key]);
  if (result.assets) result.assets.directory = resolve(directory, result.assets.directory);
  // Preserve variables/secrets, compatibility flags and uploaded module bytes.
  // The caller subsequently replaces D1 IDs with its confirmed trial databases.
  return result;
}

export async function qualifyCloudRuntime(context, {
  journalDirectory,
  adapterFactory = (options) => new WranglerReleaseAdapter(options),
  fetchProbe = fetch,
  qualifySchema = qualifyFirstRelease,
  frozenSchema = executeFrozenSchema,
  continuation,
  env = process.env,
} = {}) {
  const { root, directory, candidate, verify } = context;
  verify();
  if (Object.keys(candidate.workers).sort().join() !== [...WORKERS].sort().join())
    throw new Error("cloud qualification requires all nine frozen Workers");
  mkdirSync(journalDirectory, { mode: 0o700 });
  const transaction = randomUUID();
  const prefix = `eddy-ci-${candidate.stage === "beta" ? "b" : "p"}-${transaction.slice(0, 8)}`;
  const names = Object.fromEntries(Object.entries(candidate.workers).map(([role, worker]) => [worker.name, `${prefix}-${role}`]));
  const probe = structuredClone(candidate);
  for (const [role, worker] of Object.entries(candidate.workers)) {
    const config = resolve(directory, worker.config);
    const path = resolve(journalDirectory, `${role}.json`);
    writeFileSync(path, JSON.stringify(probeConfiguration(readJson(config), names, dirname(config))), { mode: 0o600 });
    probe.workers[role] = { ...worker, name: names[worker.name], config: path };
  }
  const token = randomBytes(32).toString("hex");
  const gateway = `${prefix}-probe`;
  const gatewayFile = resolve(journalDirectory, "probe.js");
  writeFileSync(gatewayFile, gatewaySource, { mode: 0o600 });
  const gatewayConfig = resolve(journalDirectory, "probe.json");
  writeFileSync(gatewayConfig, JSON.stringify({
    name: gateway, account_id: candidate.account_id,
    main: gatewayFile, compatibility_date: "2026-09-01",
    workers_dev: true, preview_urls: false, routes: [],
    services: Object.keys(HEALTH).map((role) => ({ binding: role.replaceAll("-", "_"), service: probe.workers[role].name })),
  }), { mode: 0o600 });
  probe.workers.probe = { name: gateway, config: gatewayConfig };
  probe.resource_plan.secrets.probe = { PROBE_TOKEN: PROBE_SECRET };
  const adapter = adapterFactory({ root, directory: journalDirectory, candidate: probe, env: { ...env, [PROBE_SECRET]: token } });
  const original = adapterFactory({ root, directory, candidate, env });
  const journal = {
    schema_version: 1, transaction, candidate_digest: candidate.candidate_digest,
    commit: candidate.source.commit, state: "observing", artifact_qualified: false,
    release_ready: false, workers: [], databases: [], cases: [], cleanup: [],
  };
  const persist = () => writeJson(journalDirectory, "journal.json", journal);
  persist();
  const before = {};
  let failure;
  try {
    await original.preconditions();
    const observations = await observeReleaseCandidate(candidate, original, continuation);
    const schema = await qualifySchema({ ...context, observations }, original);
    journal.cases.push(...schema.cases);
    for (const worker of Object.values(candidate.workers)) before[worker.name] = observations[worker.name];
    for (const worker of Object.values(probe.workers))
      if (digest(await adapter.observeWorker(worker.name)) !== digest({ status: "absent" }))
        throw new Error("probe name already exists; no existing Worker may be adopted");
    // Execute the exact SQL through remote D1 before CI can approve a delivery.
    // Trial Auth readiness may initialize JWKS, so every trial Worker is bound
    // to these disposable databases rather than the serving data authorities.
    const fixtures = frozenSchema(context), databaseIds = new Map();
    mkdirSync(resolve(journalDirectory, "migrations"));
    for (const authority of candidate.resource_plan.migrations) {
      verify();
      const resource = { kind: "d1", key: `d1:${authority.authority}`, name: `${prefix}-${authority.authority}-db` };
      if (digest(await adapter.observeResource(resource)) !== digest({ status: "absent" }))
        throw new Error("probe database name already exists");
      const event = { resource, state: "in_flight", sql_sha256: digest(authority.files) };
      journal.databases.push(event);
      persist();
      const created = await adapter.create(resource);
      if (!UUID.test(created.created_id ?? "") || Object.values(candidate.inventory.d1_ids).includes(created.created_id))
        throw new Error("probe database creation did not return an independent identity");
      event.created_id = created.created_id;
      persist();
      const observed = await adapter.observeResource(resource);
      if (observed.status !== "present" || observed.id !== event.created_id)
        throw new Error("probe database creation was not observed");
      const path = resolve(directory, `migrations/${authority.authority}.json`);
      const config = readJson(path);
      config.name = names[config.name];
      if (!config.name || config.d1_databases?.length !== 1 || config.d1_databases[0].database_id !== authority.database_id)
        throw new Error("probe migration differs from the frozen authority");
      const binding = config.d1_databases[0];
      binding.database_id = event.created_id;
      binding.database_name = resource.name;
      binding.migrations_dir = resolve(dirname(path), binding.migrations_dir);
      writeFileSync(resolve(journalDirectory, `migrations/${authority.authority}.json`), JSON.stringify(config), { mode: 0o600 });
      const privateAuthority = { ...authority, database_id: event.created_id };
      if (adapter.migrate(privateAuthority).exit !== 0)
        throw new Error(`cloud SQL migration did not qualify: ${authority.authority}`);
      const expected = fixtures.find((fixture) => fixture.authority === authority.authority);
      if (!expected || digest(await adapter.migrationLedger(privateAuthority)) !== digest(authority.files.map((file) => file.name)) ||
          digest(comparableSchemaCatalog(await querySchema(adapter, privateAuthority, SCHEMA_QUERY))) !== digest(comparableSchemaCatalog(expected.schema_catalog)) ||
          (await querySchema(adapter, privateAuthority, "PRAGMA foreign_key_check")).length)
        throw new Error(`cloud SQL catalog did not qualify: ${authority.authority}`);
      databaseIds.set(authority.database_id, { id: event.created_id, name: resource.name });
      event.state = "migrated";
      journal.cases.push({ id: `cloud.sql.${authority.authority}`, result: "pass" });
      persist();
    }
    if (databaseIds.size !== 2) throw new Error("cloud qualification requires both SQL authorities");
    for (const worker of Object.values(probe.workers)) {
      const config = readJson(worker.config);
      for (const binding of config.d1_databases ?? []) {
        const database = databaseIds.get(binding.database_id);
        if (!database) throw new Error("probe cannot bind to a serving D1 database");
        binding.database_id = database.id;
        if (binding.database_name) binding.database_name = database.name;
      }
      writeFileSync(worker.config, JSON.stringify(config), { mode: 0o600 });
    }
    journal.state = "uploading";
    persist();
    const roles = candidate.resource_plan.deploy_order.map((name) =>
      Object.keys(candidate.workers).find((role) => candidate.workers[role].name === name));
    if (roles.length !== WORKERS.length || new Set(roles).size !== WORKERS.length || roles.includes(undefined))
      throw new Error("probe requires the candidate's complete deployment order");
    for (const role of [...roles, "probe"]) {
      verify();
      const worker = probe.workers[role];
      const event = { role, name: worker.name, tag: `${transaction}:${role}`, message: `release-ci=${candidate.candidate_digest}`, state: "in_flight" };
      journal.workers.push(event);
      persist();
      const process = adapter.deploy(role, event.tag, event.message);
      const observed = await adapter.observeWorker(worker.name);
      if (observed.status === "present" && observed.tag === event.tag && observed.message === event.message) {
        event.version = observed.version;
        event.state = "uploaded";
        persist();
      }
      if (process.exit !== 0 || event.state !== "uploaded")
        throw new Error(`cloud runtime upload did not qualify: ${role}`);
      if (role !== "probe") journal.cases.push({ id: `cloud.upload.${role}`, result: "pass" });
    }
    const subdomain = (await adapter.api("workers/subdomain")).result?.subdomain;
    if (typeof subdomain !== "string" || !/^[a-z0-9-]+$/.test(subdomain))
      throw new Error("cloud probe account subdomain is unavailable");
    for (const role of Object.keys(HEALTH)) {
      // Two actual requests exercise the Python cold application import and its
      // subsequent request, through a separate token-guarded service gateway.
      for (let attempt = 0; attempt < 2; attempt++) {
        const response = await fetchProbe(`https://${gateway}.${subdomain}.workers.dev/${role}`, {
          headers: { "x-release-probe": token }, redirect: "manual", signal: AbortSignal.timeout(60_000),
        });
        await response.body?.cancel();
        if (response.status !== 200) throw new Error(`cloud runtime health did not qualify: ${role} (${response.status})`);
      }
      journal.cases.push({ id: `cloud.cold-and-warm.${role}`, result: "pass" });
      persist();
    }
    verify();
    for (const worker of Object.values(candidate.workers))
      if (digest(await original.observeWorker(worker.name)) !== digest(before[worker.name]))
        throw new Error("serving Worker changed during cloud qualification");
  } catch (error) {
    failure = error;
  } finally {
    // A failed upload has an unknown outcome. Reobserve the exact generated
    // name and transaction annotation before deleting any exclusively owned probe.
    for (const event of [...journal.workers].reverse()) {
      try {
        const observed = await adapter.observeWorker(event.name);
        if (observed.status === "present") {
          if (observed.tag !== event.tag || observed.message !== event.message ||
              (event.version && observed.version !== event.version))
            throw new Error("probe cleanup found an external version");
          await adapter.api(`workers/scripts/${encodeURIComponent(event.name)}?force=true`, { method: "DELETE" });
          if (digest(await adapter.observeWorker(event.name)) !== digest({ status: "absent" }))
            throw new Error("probe deletion is not confirmed");
        } else if (digest(observed) !== digest({ status: "absent" })) {
          throw new Error("probe cleanup cannot establish absence");
        }
        journal.cleanup.push({ name: event.name, result: "pass" });
      } catch {
        journal.cleanup.push({ name: event.name, result: "reconciliation-required" });
        failure ??= new Error("cloud probe cleanup requires reconciliation");
      }
      persist();
    }
    for (const event of [...journal.databases].reverse()) {
      try {
        const observed = await adapter.observeResource(event.resource);
        if (event.created_id) {
          if (observed.status !== "absent" && (observed.status !== "present" || observed.id !== event.created_id))
            throw new Error("probe database has a different owner");
          await adapter.api(`d1/database/${event.created_id}`, { method: "DELETE" });
          if (digest(await adapter.observeResource(event.resource)) !== digest({ status: "absent" }))
            throw new Error("probe database deletion is not confirmed");
        } else if (digest(observed) !== digest({ status: "absent" })) {
          throw new Error("database creation has no confirmed ownership");
        }
        journal.cleanup.push({ name: event.resource.name, result: "pass" });
      } catch {
        journal.cleanup.push({ name: event.resource.name, result: "reconciliation-required" });
        failure ??= new Error("cloud database cleanup requires reconciliation");
      }
      persist();
    }
  }
  journal.state = failure ? "failed" : "runtime-qualified";
  journal.artifact_qualified = !failure;
  persist();
  if (failure) throw failure;
  return journal;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  let journalDirectory;
  try {
    const { values } = parseArgs({ options: { delivery: { type: "string" }, "journal-root": { type: "string" } } });
    const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
    const journalRoot = values["journal-root"];
    if (!isAbsolute(journalRoot ?? "") || journalRoot.includes("/_work/"))
      throw new Error("cloud qualification requires a persistent journal root");
    const delivery = resolve(values.delivery);
    const directory = resolve(delivery, "unpacked/cloudflare");
    const candidate = verifyCandidate(directory, root);
    const receipt = readJson(resolve(delivery, "delivery.json"));
    if (receipt.commit !== candidate.source.commit || receipt.candidate_digest !== candidate.candidate_digest || receipt.stage !== candidate.stage)
      throw new Error("cloud qualification differs from the verified delivery");
    const secrets = JSON.parse(process.env.RELEASE_SECRETS_JSON || "{}");
    const env = { ...process.env };
    delete env.RELEASE_SECRETS_JSON;
    for (const reference of new Set(Object.values(candidate.inventory.secret_refs).flatMap(Object.values))) {
      if (typeof secrets[reference] !== "string" || !secrets[reference]) throw new Error("cloud qualification secret is missing");
      env[reference] = secrets[reference];
    }
    mkdirSync(journalRoot, { recursive: true, mode: 0o700 });
    journalDirectory = resolve(journalRoot, `ci-${candidate.source.commit}-${randomUUID()}`);
    const previous = deliveryContinuation(journalRoot, receipt);
    const continuation = previous ? continuationContext(root, candidate, previous) : undefined;
    const context = qualificationContext(root, { candidate_directory: directory, candidate, observations: { release_phase: "candidate" } });
    const result = await qualifyCloudRuntime(context, { journalDirectory, env, continuation });
    console.log(JSON.stringify({ state: result.state, artifact_qualified: result.artifact_qualified, cases: result.cases.length, journal: journalDirectory }));
  } catch (error) {
    console.error(`Cloud release CI failed: ${error.message}`);
    if (journalDirectory) console.error(`Qualification evidence: ${journalDirectory}`);
    process.exitCode = 1;
  }
}
