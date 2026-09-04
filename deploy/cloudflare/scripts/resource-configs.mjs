import { readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import YAML from "yaml";
import {
  canonical,
  digest,
  STORAGE_BINDINGS,
  validateResourceInput,
  WORKERS,
} from "./resource-input.mjs";

// Templates are JSONC; strings containing URLs or ",}" must stay byte-exact.
export function parseJsonc(text) {
  let clean = "",
    quoted = false,
    escaped = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      clean += char;
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
    } else if (char === '"') {
      quoted = true;
      clean += char;
    } else if (char === "/" && text[i + 1] === "/") {
      while (i < text.length && text[i] !== "\n") i++;
      clean += "\n";
    } else if (char === "/" && text[i + 1] === "*") {
      const end = text.indexOf("*/", i + 2);
      if (end < 0) throw new Error("unterminated JSONC comment");
      clean += " ";
      i = end + 1;
    } else if (char === "," && /^[\s]*[}\]]/.test(text.slice(i + 1))) continue;
    else clean += char;
  }
  // A comment between a final comma and closing brace is handled after comments
  // are removed, by a second string-aware pass rather than a string regex.
  let result = "";
  quoted = false;
  escaped = false;
  for (let i = 0; i < clean.length; i++) {
    const char = clean[i];
    if (!quoted && char === "," && /^\s*[}\]]/.test(clean.slice(i + 1)))
      continue;
    result += char;
    if (quoted && escaped) escaped = false;
    else if (quoted && char === "\\") escaped = true;
    else if (char === '"') quoted = !quoted;
  }
  return JSON.parse(result);
}
export function readWorkerTemplates(root) {
  return Object.fromEntries(
    WORKERS.filter((role) => role !== "web").map((role) => {
      const path = `${
        role.startsWith("api-") ? "python" : "workers"
      }/${role}/wrangler.jsonc`;
      const source = readFileSync(resolve(root, path), "utf8");
      return [
        role,
        { path, source_hash: digest(source), config: parseJsonc(source) },
      ];
    }),
  );
}
function dependencyOrder(dependencies) {
  const order = [],
    active = new Set(),
    done = new Set();
  function visit(role) {
    if (active.has(role)) throw new Error(`Worker binding cycle at ${role}`);
    if (done.has(role)) return;
    if (!WORKERS.includes(role))
      throw new Error(`unknown Worker dependency: ${role}`);
    active.add(role);
    for (const other of dependencies[role]) visit(other);
    active.delete(role);
    done.add(role);
    order.push(role);
  }
  for (const role of WORKERS) visit(role);
  return order;
}
function publicConfig(role, config, input, projected, names, origins) {
  config.name = names[`worker:${role}`];
  config.account_id = input.account_id;
  config.preview_urls = false;
  config.workers_dev =
    ["auth", "edge", "web"].includes(role) &&
    input.routing.mode === "workers_dev";
  delete config.routes;
  if (input.routing.mode === "custom_domains") {
    const hosts = [
      ...new Set(
        Object.entries({
          auth: "auth",
          api: "edge",
          web: "web",
          mcp: "edge",
          share: "edge",
          objects: "edge",
        })
          .filter(([, owner]) => role === owner)
          .map(([key]) => new URL(origins[key]).hostname),
      ),
    ];
    if (hosts.length)
      config.routes = hosts.map((pattern) => ({
        pattern,
        custom_domain: true,
      }));
  }
  config.vars ??= {};
  if (["api-core", "api-ai"].includes(role))
    config.vars.BRAND_RUNTIME_JSON = JSON.stringify(projected.brand_runtime);
  if (["edge", "auth"].includes(role)) {
    config.vars.ALLOWED_ORIGINS = origins.web;
    config.vars.MCP_RESOURCE_URL = `${origins.mcp}/v1/mcp/sse`;
  }
  if (role === "auth") {
    config.vars.BETTER_AUTH_URL = origins.auth;
    config.vars.AUTH_JWT_ISSUER = origins.auth;
    config.vars.AUTH_JWT_AUDIENCE = origins.auth;
    config.vars.NATIVE_AUTH_PUBLIC_BASE_URL = origins.api;
    if (input.allocation === "new") {
      config.vars.MCP_ALLOW_UNAUTHENTICATED_DCR = "false";
      config.vars.LEGACY_AUTH_EXACT_STAGING_ENABLED = "false";
    }
  }
  if (role === "edge")
    config.vars.MCP_AUTHORIZATION_SERVER_URL = `${origins.auth}/api/auth`;
  if (["api-core", "jobs"].includes(role)) {
    config.vars.PUBLIC_API_BASE_URL = origins.api;
    config.vars.ACCOUNT_CUTOVER_MANIFEST_ID = input.migration_lineage;
  }
  if (input.allocation === "new") {
    if (["edge", "realtime"].includes(role))
      config.vars.ACCOUNT_ACTIVATION_FENCE_ENABLED = String(
        projected.profile.capabilities.account_activation_fence,
      );
    if (role === "api-core")
      config.vars.ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED = "false";
    const freshOff = {
      jobs: [
        "MCP_APP_LEGACY_EXACT_STAGING_ENABLED",
        "LEGACY_EXTERNAL_APP_OAUTH_STAGING_ENABLED",
      ],
      edge: [
        "AUTH_EXACT_NATIVE_STAGING_ENABLED",
        "AUTH_EXACT_OAUTH_STAGING_ENABLED",
        "MCP_APP_EXACT_LEGACY_STAGING_ENABLED",
        "LEGACY_CHAT_FILES_STAGING_ENABLED",
      ],
    };
    for (const key of freshOff[role] ?? []) config.vars[key] = "false";
  }
}
export function renderResourcePlan({
  root,
  input,
  projected,
  templates = readWorkerTemplates(root),
  web,
  sourceCommit,
}) {
  const { names, origins } = validateResourceInput(input, projected);
  if (!/^[0-9a-f]{40}$/.test(sourceCommit))
    throw new Error("an exact source commit is required");
  if (
    !web?.manifest ||
    web.manifest.brand !== input.brand ||
    web.manifest.target !== input.target ||
    web.manifest.stage !== input.stage ||
    web.manifest.source_commit !== sourceCommit ||
    web.manifest.entry !== "wrangler.json" ||
    !web.manifest.routes?.length
  )
    throw new Error("current matching Moonshine Web artifact is required");
  const publicKeys = [
    "name",
    "target",
    "stage",
    "identity_provider",
    "api_base_url",
    "auth_base_url",
    "web_base_url",
    "mcp_base_url",
    "share_base_url",
    "objects_base_url",
    "auth_callback_scheme",
    "capabilities",
  ];
  if (
    publicKeys.some(
      (key) =>
        JSON.stringify(canonical(projected.profile[key])) !==
        JSON.stringify(canonical(web.manifest.profile?.[key])),
    )
  )
    throw new Error("Web artifact and backend resource profile differ");
  if (!web.config?.main || !web.config?.assets?.directory)
    throw new Error("Web artifact has no Worker/assets closure");
  const sourceOwners = new Map(
    Object.entries(templates).map(([role, template]) => [
      template.config.name,
      role,
    ]),
  );
  if (sourceOwners.size !== 7)
    throw new Error("Worker templates contain duplicate names");
  const sourceResources = new Map(),
    resources = new Map(),
    dependencies = Object.fromEntries(WORKERS.map((role) => [role, []]));
  const register = (kind, role, oldName, owner) => {
    const key = `${kind}:${role}`,
      name = names[key];
    if (!name || !oldName) throw new Error(`unowned resource binding: ${key}`);
    const sourceKey = `${kind}:${oldName}`;
    if (
      sourceResources.has(sourceKey) &&
      sourceResources.get(sourceKey) !== key
    )
      throw new Error("one template resource has conflicting logical owners");
    sourceResources.set(sourceKey, key);
    if (resources.has(key) && resources.get(key).source_name !== oldName)
      throw new Error(`binding ${key} drifts between Workers`);
    const previous = resources.get(key);
    resources.set(key, {
      key,
      kind,
      name,
      source_name: oldName,
      owners: [...new Set([...(previous?.owners ?? []), owner])].sort(),
    });
    return name;
  };
  for (const role of WORKERS)
    register(
      "worker",
      role,
      role === "web" ? web.config.name : templates[role].config.name,
      role,
    );
  // Discover queue producer and DLQ identities before rewriting any consumer.
  for (const [role, template] of Object.entries(templates)) {
    for (const producer of template.config.queues?.producers ?? [])
      register(
        "queue",
        STORAGE_BINDINGS.queue[producer.binding],
        producer.queue,
        role,
      );
    for (const consumer of template.config.queues?.consumers ?? [])
      if (consumer.dead_letter_queue)
        register("queue", "jobs-dlq", consumer.dead_letter_queue, role);
  }
  const configs = {};
  const data = (kind, binding, oldName, owner) =>
    register(kind, STORAGE_BINDINGS[kind][binding], oldName, owner);
  const worker = (sourceName, owner) => {
    const role = sourceOwners.get(sourceName);
    if (!role) throw new Error(`unknown service/DO binding in ${owner}`);
    dependencies[owner].push(role);
    return names[`worker:${role}`];
  };
  for (const role of WORKERS) {
    const config = structuredClone(
      role === "web" ? web.config : templates[role].config,
    );
    const classifiedFields = new Set([
      "$schema",
      "name",
      "main",
      "compatibility_date",
      "compatibility_flags",
      "workers_dev",
      "preview_urls",
      "limits",
      "alias",
      "vars",
      "services",
      "ai",
      "images",
      "vectorize",
      "queues",
      "d1_databases",
      "r2_buckets",
      "durable_objects",
      "migrations",
      "assets",
      "triggers",
    ]);
    for (const key of Object.keys(config))
      if (!classifiedFields.has(key))
        throw new Error(`unclassified ${key} binding/configuration in ${role}`);
    for (const item of config.services ?? [])
      item.service = worker(item.service, role);
    for (const item of config.durable_objects?.bindings ?? []) {
      if (item.script_name) item.script_name = worker(item.script_name, role);
      else {
        const key = `durable-object:${role}/${item.class_name}`;
        resources.set(key, {
          key,
          kind: "durable-object",
          name: `${names[`worker:${role}`]}::${item.class_name}`,
          owners: [role],
          worker: names[`worker:${role}`],
          class_name: item.class_name,
        });
      }
    }
    for (const item of config.d1_databases ?? []) {
      const db = STORAGE_BINDINGS.d1[item.binding];
      item.database_name = data("d1", item.binding, item.database_name, role);
      item.database_id = input.d1_ids[db];
      item.migrations_dir = `migrations/${db}`;
    }
    for (const item of config.r2_buckets ?? [])
      item.bucket_name = data("r2", item.binding, item.bucket_name, role);
    for (const item of config.vectorize ?? [])
      item.index_name = data("vectorize", item.binding, item.index_name, role);
    for (const item of config.queues?.producers ?? [])
      item.queue = data("queue", item.binding, item.queue, role);
    for (const item of config.queues?.consumers ?? []) {
      for (const key of ["queue", "dead_letter_queue"])
        if (item[key]) {
          const owned = sourceResources.get(`queue:${item[key]}`);
          if (!owned) throw new Error(`unowned queue consumer in ${role}`);
          item[key] = names[owned];
        }
    }
    publicConfig(role, config, input, projected, names, origins);
    configs[role] = {
      source_config:
        role === "web" ? "web-artifact/wrangler.json" : templates[role].path,
      config,
    };
  }
  dependencies.web.push("auth", "edge");
  for (const role of WORKERS)
    dependencies[role] = [...new Set(dependencies[role])].sort();
  const allocated = [...resources.values()];
  for (const key of Object.keys(names))
    if (!resources.has(key))
      throw new Error(`resource catalog entry has no consumer: ${key}`);
  const vectorNamespaces = YAML.parse(
    readFileSync(resolve(root, "manifests/vector-namespaces.yaml"), "utf8"),
  ).namespaces;
  for (const resource of allocated.filter(
    (item) => item.kind === "vectorize",
  )) {
    const specs = vectorNamespaces.filter(
      (entry) => entry.target_index === resource.source_name,
    );
    if (
      specs.length !== 1 ||
      specs[0].target_dimensions > projected.profile.capabilities.embedding_dims
    )
      throw new Error(
        "Vectorize resource/model dimensions have no unique owner",
      );
    Object.assign(resource, {
      dimensions: specs[0].target_dimensions,
      metric: "cosine",
      model: specs[0].target_model,
    });
  }
  // Source names describe migration inputs, never an implicit allocation target.
  for (const resource of allocated) delete resource.source_name;
  const migrations = ["auth", "app"].map((db) => ({
    authority: db,
    worker: names[`worker:${db === "auth" ? "auth" : "api-core"}`],
    binding: db === "auth" ? "AUTH_DB" : "APP_DB",
    database_name: names[`d1:${db}`],
    database_id: input.d1_ids[db],
    directory: `migrations/${db}`,
    files: readdirSync(resolve(root, `migrations/${db}`))
      .filter((file) => file.endsWith(".sql"))
      .sort()
      .map((name) => ({
        name,
        sha256: digest(readFileSync(resolve(root, `migrations/${db}`, name))),
      })),
  }));
  const order = dependencyOrder(dependencies);
  const plan = {
    schema_version: 1,
    brand: input.brand,
    target: input.target,
    stage: input.stage,
    account_id: input.account_id,
    allocation: input.allocation,
    source_commit: sourceCommit,
    migration_lineage: input.migration_lineage,
    profile: projected.profile,
    origins,
    resources: allocated,
    dependencies,
    deploy_order: order.map((role) => names[`worker:${role}`]),
    rollback_order: [...order].reverse().map((role) => names[`worker:${role}`]),
    migrations,
    platform_bindings: Object.entries(configs).flatMap(([role, { config }]) =>
      ["ai", "images"]
        .filter((kind) => config[kind])
        .map((kind) => ({
          worker: config.name,
          kind,
          binding: config[kind].binding,
          account_id: input.account_id,
        })),
    ),
    secrets: input.secret_refs,
    configs,
    source_hashes: Object.fromEntries(
      Object.entries(templates).map(([role, template]) => [
        role,
        template.source_hash,
      ]),
    ),
    dependency_locks: Object.fromEntries(
      [
        "package-lock.json",
        "python/api-core/pylock.toml",
        "python/api-ai/pylock.toml",
        "../../web/app/bun.lock",
      ].map((path) => [path, digest(readFileSync(resolve(root, path)))]),
    ),
    web: {
      routes: web.manifest.routes,
      source_dirty: web.manifest.source_dirty,
      pending_qualification: web.manifest.pending_qualification ?? [],
    },
    release_ready: false,
    remote_state_verified: false,
    pending_qualification: [
      "CF-5 remote resource identity and release transaction",
      "CI-1 full dual-target product contracts",
      "CF-4 API/MCP/share/object mount paths",
      "Optional provider credentials and provider flows",
    ],
  };
  return { ...plan, plan_digest: digest(plan) };
}
