import { randomBytes } from "node:crypto";

export const PRODUCTION_CONFIRMATION =
  "deploy-independent-cloudflare-production";

export const PRODUCTION_DEPLOYMENTS = Object.freeze([
  Object.freeze({
    workerName: "omi-cf-auth-production",
    runtime: "typescript",
    target: "workers/auth/wrangler.jsonc",
  }),
  Object.freeze({
    workerName: "omi-cf-rate-limit-production",
    runtime: "typescript",
    target: "workers/rate-limit/wrangler.jsonc",
  }),
  Object.freeze({
    workerName: "omi-cf-api-core-production",
    runtime: "python",
    target: "python/api-core/wrangler.jsonc",
  }),
  Object.freeze({
    workerName: "omi-cf-api-ai-production",
    runtime: "python",
    target: "python/api-ai/wrangler.jsonc",
  }),
  Object.freeze({
    workerName: "omi-cf-realtime-production",
    runtime: "typescript",
    target: "workers/realtime/wrangler.jsonc",
  }),
  Object.freeze({
    workerName: "omi-cf-jobs-production",
    runtime: "typescript",
    target: "workers/jobs/wrangler.jsonc",
  }),
  Object.freeze({
    workerName: "omi-cf-edge-production",
    runtime: "typescript",
    target: "workers/edge/wrangler.jsonc",
  }),
]);

export const PRODUCTION_WORKERS = Object.freeze([
  ...PRODUCTION_DEPLOYMENTS.map(({ workerName }) => workerName),
  "omi-web-app-production",
]);

export const PRODUCTION_RESOURCES = Object.freeze({
  d1: Object.freeze(["omi-cf-auth-production", "omi-cf-app-production"]),
  r2: Object.freeze([
    "omi-cf-production",
    "omi-cf-chat-files-production",
    "omi-cf-conversation-recordings-production",
    "omi-cf-speech-profiles-production",
    "omi-cf-desktop-updates-production",
  ]),
  queues: Object.freeze([
    "omi-cf-jobs-production",
    "omi-cf-jobs-dlq-production",
    "omi-cf-sync-fresh-production",
    "omi-cf-sync-backfill-production",
  ]),
  vectorize: Object.freeze([
    "omi-cf-conversations-production-v1",
    "omi-cf-memories-production-v1",
    "omi-cf-action-items-production-v1",
    "omi-cf-transcript-chunks-production-v1",
    "omi-cf-x-posts-production-v1",
    "omi-cf-workstreams-production-v1",
    "omi-cf-screen-activity-production-v1",
  ]),
});

const PRODUCTION_DEPENDENCIES = Object.freeze({
  "omi-cf-api-ai-production": ["omi-cf-rate-limit-production"],
  "omi-cf-jobs-production": ["omi-cf-auth-production"],
  "omi-cf-edge-production": [
    "omi-cf-auth-production",
    "omi-cf-rate-limit-production",
    "omi-cf-api-core-production",
    "omi-cf-api-ai-production",
    "omi-cf-realtime-production",
    "omi-cf-jobs-production",
  ],
});

const PRODUCTION_ROLLBACK_ORDER = Object.freeze([
  "omi-web-app-production",
  "omi-cf-edge-production",
  "omi-cf-jobs-production",
  "omi-cf-realtime-production",
  "omi-cf-api-ai-production",
  "omi-cf-api-core-production",
  "omi-cf-rate-limit-production",
  "omi-cf-auth-production",
]);

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SUBDOMAIN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

export function assertProductionConfirmation(env = process.env) {
  if (env.CLOUDFLARE_PRODUCTION_CONFIRM !== PRODUCTION_CONFIRMATION) {
    throw new Error(
      `production release requires CLOUDFLARE_PRODUCTION_CONFIRM=${PRODUCTION_CONFIRMATION}`,
    );
  }
}

export function resolveWorkersSubdomain(
  raw = process.env.CLOUDFLARE_WORKERS_SUBDOMAIN,
) {
  // No default: a silent fallback here would bake one specific Cloudflare
  // account's workers.dev subdomain into every brand's production config
  // that forgets to set this. Every deploy caller must name its own account.
  if (!raw || !String(raw).trim()) {
    throw new Error(
      "CLOUDFLARE_WORKERS_SUBDOMAIN is required (no default subdomain)",
    );
  }
  const value = String(raw).trim().toLowerCase();
  if (!SUBDOMAIN.test(value)) {
    throw new Error("invalid Cloudflare workers.dev subdomain");
  }
  return value;
}

export function productionUrls(subdomain = resolveWorkersSubdomain()) {
  return Object.freeze({
    auth: `https://omi-cf-auth-production.${subdomain}.workers.dev`,
    edge: `https://omi-cf-edge-production.${subdomain}.workers.dev`,
    web: `https://omi-web-app-production.${subdomain}.workers.dev`,
  });
}

export function assertValidProductionDeploymentOrder(
  deployments = PRODUCTION_DEPLOYMENTS,
) {
  const seen = new Set();
  for (const { workerName } of deployments) {
    for (const dependency of PRODUCTION_DEPENDENCIES[workerName] || []) {
      if (!seen.has(dependency)) {
        throw new Error(
          `${workerName} must deploy after dependency ${dependency}`,
        );
      }
    }
    seen.add(workerName);
  }
}

function replaceExact(source, from, to) {
  if (!source.includes(from)) {
    throw new Error(`production config source is missing ${from}`);
  }
  return source.replaceAll(from, to);
}

function bindDatabaseId(source, databaseName, databaseId) {
  if (!UUID.test(databaseId)) {
    throw new Error(`invalid production D1 UUID for ${databaseName}`);
  }
  const escaped = databaseName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(
    `^(\\s*)"database_name": "${escaped}",(?:\\n\\1"database_id": "[^"]+",)?`,
    "gm",
  );
  let count = 0;
  const rendered = source.replace(pattern, (_, indentation) => {
    count += 1;
    return `${indentation}"database_name": "${databaseName}",\n${indentation}"database_id": "${databaseId}",`;
  });
  if (count === 0) {
    throw new Error(`production config does not bind ${databaseName}`);
  }
  return rendered;
}

export function renderProductionConfig(
  source,
  { appDatabaseId, authDatabaseId, subdomain = resolveWorkersSubdomain() },
) {
  if (typeof source !== "string" || !source.trim()) {
    throw new Error("production config source is empty");
  }
  const urls = productionUrls(subdomain);
  const replacements = [
    [
      "omi-cf-conversation-recordings-staging",
      "omi-cf-conversation-recordings-production",
    ],
    ["omi-cf-transcript-chunks-v2", "omi-cf-transcript-chunks-production-v1"],
    ["omi-cf-desktop-updates-staging", "omi-cf-desktop-updates-production"],
    ["omi-cf-sync-backfill-staging", "omi-cf-sync-backfill-production"],
    ["omi-cf-action-items-v2", "omi-cf-action-items-production-v1"],
    ["omi-cf-speech-profiles-staging", "omi-cf-speech-profiles-production"],
    ["omi-cf-conversations-v2", "omi-cf-conversations-production-v1"],
    ["omi-cf-chat-files-staging", "omi-cf-chat-files-production"],
    ["omi-cf-sync-fresh-staging", "omi-cf-sync-fresh-production"],
    ["omi-cf-rate-limit-staging", "omi-cf-rate-limit-production"],
    ["omi-cf-api-core-staging", "omi-cf-api-core-production"],
    ["omi-cf-api-ai-staging", "omi-cf-api-ai-production"],
    ["omi-cf-realtime-staging", "omi-cf-realtime-production"],
    ["omi-cf-jobs-dlq-staging", "omi-cf-jobs-dlq-production"],
    ["omi-cf-jobs-staging", "omi-cf-jobs-production"],
    ["omi-cf-auth-staging", "omi-cf-auth-production"],
    ["omi-cf-edge-staging", "omi-cf-edge-production"],
    ["omi-cf-memories-v2", "omi-cf-memories-production-v1"],
    ["omi-cf-x-posts-v2", "omi-cf-x-posts-production-v1"],
    ["omi-cf-workstreams-v1", "omi-cf-workstreams-production-v1"],
    ["omi-cf-screen-activity-v1", "omi-cf-screen-activity-production-v1"],
    ["omi-cf-app-staging", "omi-cf-app-production"],
    ["omi-cf-staging", "omi-cf-production"],
    ["omi-web-app-staging", "omi-web-app-production"],
    ["https://omi-cf-edge-staging.summersmile1984.workers.dev", urls.edge],
    ["https://omi-cf-auth-staging.summersmile1984.workers.dev", urls.auth],
    ["https://omi-web-app-staging.summersmile1984.workers.dev", urls.web],
    ["isolated-staging-v1", "isolated-production-v1"],
  ].sort(([left], [right]) => right.length - left.length);
  let rendered = source;
  for (const [from, to] of replacements) {
    if (rendered.includes(from)) rendered = replaceExact(rendered, from, to);
  }
  rendered = rendered
    .replace(
      '"MCP_ALLOW_UNAUTHENTICATED_DCR": "true"',
      '"MCP_ALLOW_UNAUTHENTICATED_DCR": "false"',
    )
    .replace(
      '"LEGACY_EXTERNAL_APP_OAUTH_STAGING_ENABLED": "true"',
      '"LEGACY_EXTERNAL_APP_OAUTH_STAGING_ENABLED": "false"',
    )
    .replace(
      '"MCP_APP_LEGACY_EXACT_STAGING_ENABLED": "true"',
      '"MCP_APP_LEGACY_EXACT_STAGING_ENABLED": "false"',
    )
    .replace(
      '"MCP_APP_EXACT_LEGACY_STAGING_ENABLED": "true"',
      '"MCP_APP_EXACT_LEGACY_STAGING_ENABLED": "false"',
    )
    .replace(
      '"LEGACY_CHAT_FILES_STAGING_ENABLED": "true"',
      '"LEGACY_CHAT_FILES_STAGING_ENABLED": "false"',
    );
  if (rendered.includes('"database_name": "omi-cf-app-production"')) {
    rendered = bindDatabaseId(rendered, "omi-cf-app-production", appDatabaseId);
  }
  if (rendered.includes('"database_name": "omi-cf-auth-production"')) {
    rendered = bindDatabaseId(
      rendered,
      "omi-cf-auth-production",
      authDatabaseId,
    );
  }
  return rendered;
}

export function createInitialProductionSecrets(
  subdomain = resolveWorkersSubdomain(),
  random = () => randomBytes(48).toString("base64url"),
) {
  const urls = productionUrls(subdomain);
  const internal = random();
  return {
    version: 1,
    environment: "production",
    workers: {
      "omi-cf-auth-production": {
        BETTER_AUTH_SECRET: random(),
        BETTER_AUTH_URL: urls.web,
        INTERNAL_ASSERTION_SECRET: internal,
      },
      "omi-cf-api-core-production": {
        ANNOUNCEMENTS_ADMIN_KEY: random(),
        FAIR_USE_ADMIN_KEY: random(),
        INTERNAL_ASSERTION_SECRET: internal,
      },
      "omi-cf-api-ai-production": {
        INTERNAL_ASSERTION_SECRET: internal,
      },
      "omi-cf-realtime-production": {
        INTERNAL_ASSERTION_SECRET: internal,
      },
      "omi-cf-jobs-production": {
        ADMIN_KEY: random(),
        APPS_ADMIN_KEY: random(),
        GOOGLE_CALENDAR_TOKEN_ENCRYPTION_SECRET: random(),
        INTERNAL_ASSERTION_SECRET: internal,
        MCP_APP_TOKEN_ENCRYPTION_SECRET: random(),
        STRIPE_CONNECT_REFRESH_SECRET: random(),
        TASK_INTEGRATION_TOKEN_ENCRYPTION_SECRET: random(),
      },
      "omi-cf-edge-production": {
        BYOK_FINGERPRINT_PEPPER: random(),
        INTERNAL_ASSERTION_SECRET: internal,
      },
    },
  };
}

function activeVersion(raw, workerName) {
  if (raw === null) return null;
  const status = typeof raw === "string" ? JSON.parse(raw) : raw;
  const versions = Array.isArray(status?.versions) ? status.versions : [];
  const active = versions.filter(
    (version) =>
      version &&
      typeof version.version_id === "string" &&
      version.version_id.length > 0 &&
      version.percentage === 100,
  );
  if (active.length !== 1 || versions.length !== 1) {
    throw new Error(
      `${workerName} must have exactly one 100% active version before a production release`,
    );
  }
  return active[0].version_id;
}

export function createProductionDeploymentSnapshot(
  statuses,
  createdAt = new Date(),
) {
  const workers = {};
  for (const workerName of PRODUCTION_WORKERS) {
    if (!(workerName in statuses)) {
      throw new Error(`missing deployment status for ${workerName}`);
    }
    workers[workerName] = activeVersion(statuses[workerName], workerName);
  }
  return {
    version: 1,
    environment: "production",
    createdAt: createdAt.toISOString(),
    workers,
  };
}

export function productionRollbackPlan(snapshot) {
  if (
    snapshot?.version !== 1 ||
    snapshot?.environment !== "production" ||
    !snapshot.workers ||
    typeof snapshot.workers !== "object"
  ) {
    throw new Error("invalid Cloudflare production deployment snapshot");
  }
  return PRODUCTION_ROLLBACK_ORDER.map((workerName) => {
    const versionId = snapshot.workers[workerName];
    if (versionId === null) {
      return {
        action: "delete",
        workerName,
        versionId: null,
        queueConsumers:
          workerName === "omi-cf-jobs-production"
            ? [...PRODUCTION_RESOURCES.queues]
            : [],
      };
    }
    if (typeof versionId !== "string" || !/^[A-Za-z0-9-]+$/.test(versionId)) {
      throw new Error(`invalid rollback version for ${workerName}`);
    }
    return { action: "rollback", workerName, versionId, queueConsumers: [] };
  });
}
