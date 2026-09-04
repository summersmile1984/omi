import { randomUUID } from "node:crypto";
import { dirname, resolve } from "node:path";
import { readWorkerTemplates } from "../scripts/resource-configs.mjs";
import {
  STORAGE_BINDINGS,
  validateBrandRuntime,
  validateSupportEmail,
} from "../scripts/resource-input.mjs";

// This projection is exclusively a disposable loopback test target. It consumes
// production Worker/binding owners, never a resource inventory or deployment
// approval, and cannot select external accounts, origins, secrets or resources.
export function localConfigs({
  root,
  brandId,
  brandRuntime,
  supportEmail,
  namespace,
  port,
  asrPort,
  templates = readWorkerTemplates(root),
}) {
  if (
    typeof brandId !== "string" ||
    typeof namespace !== "string" ||
    !/^[a-z][a-z0-9-]{0,23}$/.test(brandId) ||
    !/^[a-z0-9-]{1,50}$/.test(namespace)
  )
    throw new Error("invalid local brand or namespace");
  validateBrandRuntime(brandRuntime, brandId);
  validateSupportEmail(supportEmail);
  for (const value of [port, asrPort])
    if (!Number.isInteger(value) || value < 1024 || value > 65535)
      throw new Error("invalid loopback port");
  const origin = `http://127.0.0.1:${port}`;
  const workerNames = Object.fromEntries(
    Object.entries(templates).map(([role, entry]) => [
      entry.config.name,
      `${namespace}-${role}`,
    ]),
  );
  const workerName = (name) => {
    if (!workerNames[name]) throw new Error("unowned Worker binding");
    return workerNames[name];
  };
  const ownerName = (kind, binding) => {
    const owner = STORAGE_BINDINGS[kind][binding];
    if (!owner) throw new Error("unowned local storage binding");
    return `${namespace}-${owner}`;
  };
  const queueNames = new Map();
  for (const { config } of Object.values(templates)) {
    for (const queue of config.queues?.producers ?? [])
      queueNames.set(queue.queue, ownerName("queue", queue.binding));
    for (const queue of config.queues?.consumers ?? [])
      if (queue.dead_letter_queue)
        queueNames.set(queue.dead_letter_queue, `${namespace}-jobs-dlq`);
  }
  const queueName = (name) => {
    if (!queueNames.has(name)) throw new Error("unowned local queue consumer");
    return queueNames.get(name);
  };
  const databaseIds = { app: randomUUID(), auth: randomUUID() };
  const configs = {};
  for (const [role, template] of Object.entries(templates)) {
    const config = structuredClone(template.config),
      sourceDirectory = resolve(root, dirname(template.path));
    for (const unsupported of [
      "kv_namespaces",
      "hyperdrive",
      "containers",
      "send_email",
      "browser",
      "unsafe",
    ])
      if (config[unsupported])
        throw new Error(`local adapter required for ${unsupported}`);
    for (const key of [
      "$schema",
      "account_id",
      "routes",
      "ai",
      "images",
      "vectorize",
      "triggers",
      "limits",
    ])
      delete config[key];
    config.name = workerName(config.name);
    config.main = resolve(sourceDirectory, config.main);
    config.workers_dev = false;
    config.preview_urls = false;
    for (const [key, value] of Object.entries(config.alias ?? {}))
      config.alias[key] = resolve(sourceDirectory, value);
    config.vars = {
      ...config.vars,
      PUBLIC_API_BASE_URL: origin,
      BETTER_AUTH_URL: origin,
      AUTH_JWT_ISSUER: origin,
      AUTH_JWT_AUDIENCE: origin,
      ALLOWED_ORIGINS: origin,
      MCP_RESOURCE_URL: `${origin}/v1/mcp/sse`,
      MCP_AUTHORIZATION_SERVER_URL: `${origin}/api/auth`,
      NATIVE_AUTH_PUBLIC_BASE_URL: origin,
      ACCOUNT_ACTIVATION_FENCE_ENABLED: "false",
      ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED: "false",
      MCP_ALLOW_UNAUTHENTICATED_DCR: "false",
    };
    if (["api-core", "api-ai"].includes(role))
      config.vars.BRAND_RUNTIME_JSON = JSON.stringify(brandRuntime);
    if (role === "api-core") config.vars.BRAND_SUPPORT_EMAIL = supportEmail;
    delete config.vars.ORIGIN_BACKEND_URL;
    for (const key of Object.keys(config.vars))
      if (key.endsWith("_STAGING_ENABLED")) config.vars[key] = "false";
    for (const db of config.d1_databases ?? []) {
      const owner = STORAGE_BINDINGS.d1[db.binding];
      db.database_name = ownerName("d1", db.binding);
      db.database_id = databaseIds[owner];
      db.migrations_dir = resolve(root, "migrations", owner);
      delete db.remote;
    }
    for (const bucket of config.r2_buckets ?? []) {
      bucket.bucket_name = ownerName("r2", bucket.binding);
      delete bucket.remote;
    }
    for (const service of config.services ?? []) {
      service.service = workerName(service.service);
      delete service.remote;
    }
    if (["api-core", "api-ai", "jobs"].includes(role)) {
      config.services ??= [];
      config.services.push({
        binding: "AI",
        service: `${namespace}-provider`,
        entrypoint: "Provider",
      });
    }
    for (const object of config.durable_objects?.bindings ?? [])
      if (object.script_name)
        object.script_name = workerName(object.script_name);
    for (const queue of config.queues?.producers ?? [])
      queue.queue = queueName(queue.queue);
    for (const queue of config.queues?.consumers ?? []) {
      queue.queue = queueName(queue.queue);
      if (queue.dead_letter_queue)
        queue.dead_letter_queue = queueName(queue.dead_letter_queue);
      queue.max_batch_timeout = 1;
    }
    if (role === "realtime")
      config.vars.ASR_WS_URL = `http://127.0.0.1:${asrPort}`;
    configs[role] = config;
  }
  configs.provider = {
    name: `${namespace}-provider`,
    main: resolve(root, "contracts/provider-fixture.ts"),
    compatibility_date: "2026-08-27",
  };
  return { origin, configs, databaseIds };
}
