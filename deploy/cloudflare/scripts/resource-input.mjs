import { createHash } from "node:crypto";

export const WORKERS = Object.freeze([
  "auth",
  "rate-limit",
  "api-core",
  "api-ai",
  "realtime",
  "jobs",
  "edge",
  "web",
]);
export const STORAGE_BINDINGS = Object.freeze({
  d1: { AUTH_DB: "auth", APP_DB: "app" },
  r2: {
    ASSETS: "assets",
    CHAT_FILES: "chat-files",
    CONVERSATION_RECORDINGS: "conversation-recordings",
    SPEECH_PROFILES: "speech-profiles",
    DESKTOP_UPDATES: "desktop-updates",
  },
  vectorize: {
    CONVERSATION_VECTORS: "conversations",
    MEMORY_VECTORS: "memories",
    ACTION_ITEM_VECTORS: "action-items",
    TRANSCRIPT_CHUNK_VECTORS: "transcript-chunks",
    X_POST_VECTORS: "x-posts",
    WORKSTREAM_VECTORS: "workstreams",
    SCREEN_ACTIVITY_VECTORS: "screen-activity",
  },
  queue: {
    JOBS: "jobs",
    SYNC_FRESH: "sync-fresh",
    SYNC_BACKFILL: "sync-backfill",
  },
});
export const REQUIRED_SECRETS = Object.freeze({
  auth: ["BETTER_AUTH_SECRET", "INTERNAL_ASSERTION_SECRET"],
  "api-core": [
    "ANNOUNCEMENTS_ADMIN_KEY",
    "FAIR_USE_ADMIN_KEY",
    "LIFECYCLE_EMAIL_SIGNING_SECRET",
    "INTERNAL_ASSERTION_SECRET",
  ],
  "api-ai": ["INTERNAL_ASSERTION_SECRET"],
  realtime: ["INTERNAL_ASSERTION_SECRET"],
  jobs: [
    "ADMIN_KEY",
    "APPS_ADMIN_KEY",
    "GOOGLE_CALENDAR_TOKEN_ENCRYPTION_SECRET",
    "MCP_APP_TOKEN_ENCRYPTION_SECRET",
    "STRIPE_CONNECT_REFRESH_SECRET",
    "TASK_INTEGRATION_TOKEN_ENCRYPTION_SECRET",
    "INTERNAL_ASSERTION_SECRET",
  ],
  edge: ["BYOK_FINGERPRINT_PEPPER", "INTERNAL_ASSERTION_SECRET"],
  "rate-limit": [],
  web: [],
});
const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const NAME = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const REFERENCE = /^[A-Z][A-Z0-9_]{2,127}$/;
export function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonical(value[key])]),
    );
  return value;
}
export function digest(value) {
  return createHash("sha256")
    .update(
      typeof value === "string" || Buffer.isBuffer(value)
        ? value
        : JSON.stringify(canonical(value)),
    )
    .digest("hex");
}
export function exactKeys(value, keys, label) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).sort().join("|") !== [...keys].sort().join("|")
  ) {
    throw new Error(`${label} requires exactly: ${keys.join(", ")}`);
  }
}
export function resourceKeys() {
  return [
    ...WORKERS.map((role) => `worker:${role}`),
    ...Object.entries(STORAGE_BINDINGS).flatMap(([kind, bindings]) =>
      Object.values(bindings).map((role) => `${kind}:${role}`),
    ),
    "queue:jobs-dlq",
  ];
}
export function validateBrandRuntime(value, brandId) {
  exactKeys(
    value,
    ["brand_id", "display_name", "ai_persona_name"],
    "brand runtime",
  );
  if (
    value.brand_id !== brandId ||
    !Object.values(value).every(
      (item) =>
        typeof item === "string" &&
        item.trim() &&
        !/[\u0000-\u001f\u007f]/u.test(item),
    ) ||
    !/^[a-z0-9-]+$/.test(value.brand_id)
  )
    throw new Error("brand runtime must match the rendered brand identity");
  return value;
}
export function validateSupportEmail(value) {
  if (
    typeof value !== "string" ||
    !/^[^\s@<>(),:;[\]\\"]+@[^\s@<>(),:;[\]\\"]+$/u.test(value) ||
    /[\u0000-\u001f\u007f]/u.test(value)
  )
    throw new Error(
      "brand support contact must be an explicit plain email address",
    );
  return value;
}
export function validateFirmwarePolicy(value, brand) {
  exactKeys(
    value,
    [
      "schema_version",
      "brand_id",
      "device_model",
      "device_model_aliases",
      "release_tag_prefix",
      "release_asset_prefix",
      "github_releases_url",
    ],
    "firmware policy",
  );
  if (
    value.schema_version !== 1 ||
    value.brand_id !== brand ||
    typeof value.device_model !== "string" ||
    !value.device_model.trim() ||
    !Array.isArray(value.device_model_aliases) ||
    !value.device_model_aliases.length ||
    value.device_model_aliases.length > 8 ||
    !value.device_model_aliases.every(
      (item) => typeof item === "string" && item.trim(),
    ) ||
    new Set(value.device_model_aliases).size !== value.device_model_aliases.length ||
    value.device_model_aliases.includes(value.device_model) ||
    typeof value.release_tag_prefix !== "string" ||
    !/^[A-Za-z0-9_]+_v$/.test(value.release_tag_prefix) ||
    value.release_asset_prefix !== value.release_tag_prefix.slice(0, -1) + "OTA_v" ||
    typeof value.github_releases_url !== "string"
  )
    throw new Error("firmware policy does not match the rendered brand identity");
  let url;
  try {
    url = new URL(value.github_releases_url);
  } catch {
    throw new Error("firmware release source must be an explicit HTTPS URL");
  }
  if (
    url.protocol !== "https:" ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error("firmware release source must be an explicit HTTPS URL");
  return value;
}
export function validateResourceInput(input, projected) {
  exactKeys(
    input,
    [
      "schema_version",
      "brand",
      "target",
      "stage",
      "account_id",
      "routing",
      "allocation",
      "d1_ids",
      "secret_refs",
      "existing_names",
      "migration_lineage",
    ],
    "resource inventory",
  );
  const { brand_id: brand, profile } = projected;
  validateBrandRuntime(projected.brand_runtime, brand);
  validateSupportEmail(projected.support_email);
  validateFirmwarePolicy(projected.firmware_policy, brand);
  if (projected.product_name !== projected.brand_runtime.display_name)
    throw new Error("brand runtime and Web product identity differ");
  if (
    input.schema_version !== 1 ||
    input.brand !== brand ||
    input.target !== "cloudflare" ||
    input.stage !== profile.stage ||
    profile.target !== "cloudflare" ||
    profile.identity_provider !== "better_auth"
  ) {
    throw new Error(
      "resource inventory and rendered brand/target/stage identity differ",
    );
  }
  if (
    !NAME.test(brand) ||
    !["local", "beta", "production"].includes(input.stage) ||
    !/^[0-9a-f]{32}$/i.test(input.account_id)
  )
    throw new Error("invalid brand, stage or Cloudflare account id");
  exactKeys(input.d1_ids, ["auth", "app"], "D1 IDs");
  if (
    !Object.values(input.d1_ids).every(
      (id) => typeof id === "string" && UUID.test(id),
    ) ||
    input.d1_ids.auth.toLowerCase() === input.d1_ids.app.toLowerCase()
  )
    throw new Error("distinct explicit Auth/App D1 UUIDs are required");
  if (!["new", "existing"].includes(input.allocation))
    throw new Error("allocation must be new or existing");
  exactKeys(
    input.existing_names,
    input.allocation === "existing" ? resourceKeys() : [],
    "existing resource names",
  );
  if (
    typeof input.migration_lineage !== "string" ||
    !NAME.test(input.migration_lineage)
  )
    throw new Error("explicit migration lineage is required");
  exactKeys(input.secret_refs, WORKERS, "secret mapping owners");
  const internal = new Set();
  const privateRefs = new Set();
  for (const role of WORKERS) {
    exactKeys(
      input.secret_refs[role],
      REQUIRED_SECRETS[role],
      `${role} secret name mapping`,
    );
    for (const [binding, reference] of Object.entries(
      input.secret_refs[role],
    )) {
      if (typeof reference !== "string" || !REFERENCE.test(reference))
        throw new Error(
          "secret mappings contain environment variable names, never secret values",
        );
      if (binding === "INTERNAL_ASSERTION_SECRET") internal.add(reference);
      else {
        if (privateRefs.has(reference))
          throw new Error(
            "separate credential boundaries require distinct secret references",
          );
        privateRefs.add(reference);
      }
    }
  }
  if (internal.size !== 1 || privateRefs.has([...internal][0]))
    throw new Error(
      "all internal assertions must use one shared reference isolated from other credentials",
    );
  exactKeys(input.routing, ["mode", "workers_subdomain"], "routing");
  if (
    !["local", "custom_domains", "workers_dev"].includes(input.routing.mode) ||
    (input.stage === "local") !== (input.routing.mode === "local")
  )
    throw new Error("routing mode must match the deployment stage");
  if (
    input.routing.mode === "workers_dev"
      ? typeof input.routing.workers_subdomain !== "string" ||
        !NAME.test(input.routing.workers_subdomain)
      : input.routing.workers_subdomain !== null
  )
    throw new Error(
      "workers.dev routing requires an explicit subdomain; other modes use null",
    );
  const names = {};
  for (const key of resourceKeys()) {
    const [kind, role] = key.split(":");
    names[key] =
      input.allocation === "existing"
        ? input.existing_names[key]
        : kind === "worker" && role === "web"
        ? `${brand}-web-${input.stage}`
        : `${brand}-cf-${role}-${input.stage}${
            kind === "vectorize" ? "-v1" : ""
          }`;
    if (typeof names[key] !== "string" || !NAME.test(names[key]))
      throw new Error(`invalid or overlong resource name: ${key}`);
  }
  const physical = resourceKeys().map(
    (key) => `${key.split(":")[0]}:${names[key]}`,
  );
  if (new Set(physical).size !== physical.length)
    throw new Error("resource names collide within a Cloudflare namespace");
  const origins = {};
  const ownership = new Map();
  for (const [key, role] of Object.entries({
    api: "edge",
    auth: "auth",
    web: "web",
    mcp: "edge",
    share: "web",
    objects: "edge",
  })) {
    const url = new URL(profile[`${key}_base_url`]);
    if (
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      url.pathname !== "/"
    )
      throw new Error(
        `CF-4: ${key}_base_url mount paths are not yet qualified; an origin is required`,
      );
    if (input.stage !== "local" && (url.protocol !== "https:" || url.port))
      throw new Error(
        "Cloudflare remote origins require HTTPS without a custom port",
      );
    if (
      input.stage === "local" &&
      (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
        !["http:", "https:"].includes(url.protocol))
    )
      throw new Error("local resource fixtures require loopback origins");
    if (ownership.has(url.origin) && ownership.get(url.origin) !== role)
      throw new Error(
        "one public origin cannot route to different Worker owners",
      );
    ownership.set(url.origin, role);
    if (
      input.routing.mode === "workers_dev" &&
      url.origin !==
        `https://${names[`worker:${role}`]}.${
          input.routing.workers_subdomain
        }.workers.dev`
    )
      throw new Error(
        `profile ${key} origin differs from its workers.dev allocation`,
      );
    origins[key] = url.origin;
  }
  return { names, origins };
}
export function assertDisjointPlans(plans) {
  const owned = new Map();
  for (const plan of plans) {
    const identity = `${plan.brand}/${plan.target}/${plan.stage}`;
    const keys = [
      ...plan.resources.map(
        (entry) => `${plan.account_id}/${entry.kind}/${entry.name}`,
      ),
      ...plan.migrations.map(
        (entry) =>
          `${plan.account_id}/d1-id/${entry.database_id.toLowerCase()}`,
      ),
      ...Object.values(plan.origins).map((origin) => `public-origin/${origin}`),
      ...Object.values(plan.secrets).flatMap((mapping) =>
        Object.values(mapping).map((ref) => `secret-ref/${ref}`),
      ),
    ];
    for (const key of new Set(keys)) {
      if (owned.has(key))
        throw new Error(
          `resource ownership collision: ${identity} and ${owned.get(key)}`,
        );
      owned.set(key, identity);
    }
  }
}
