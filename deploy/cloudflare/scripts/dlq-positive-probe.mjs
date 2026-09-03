import { createHmac, randomBytes } from "node:crypto";

const DEFAULT_EDGE_URL =
  "https://omi-cf-edge-staging.summersmile1984.workers.dev";
const REQUEST_TIMEOUT_MS = 15_000;
const MESSAGE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);

function envValue(env, name) {
  const value = env[name];
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Resolve an Edge URL for the staging-only positive probe.
 *
 * The host guard is intentional: this command must not become an easy way to
 * send an operator replay request to a production hostname. Local hosts are
 * accepted for a hermetic proxy/fixture harness; every remote host must both
 * be a workers.dev origin and carry an explicit `staging` label.
 */
export function resolveStagingEdgeUrl(raw = process.env.CLOUDFLARE_EDGE_URL) {
  const value = (raw || DEFAULT_EDGE_URL).trim();
  if (!value) throw new Error("CLOUDFLARE_EDGE_URL must not be empty");
  const url = new URL(value);
  const hostname = url.hostname.toLowerCase();
  const local = LOCAL_HOSTS.has(hostname);
  if (url.protocol !== (local ? "http:" : "https:")) {
    throw new Error(
      local
        ? "local DLQ probe endpoints must use http"
        : "remote DLQ probe endpoints must use https",
    );
  }
  if (
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    (url.pathname !== "/" && url.pathname !== "")
  ) {
    throw new Error("CLOUDFLARE_EDGE_URL must be an origin without a path");
  }
  if (
    !local &&
    (!hostname.endsWith(".workers.dev") || !hostname.includes("staging"))
  ) {
    throw new Error(
      "DLQ positive probe is staging-only: remote URL must be a staging workers.dev origin",
    );
  }
  return url.origin;
}

function validateMessageId(messageId) {
  if (typeof messageId !== "string" || !MESSAGE_ID.test(messageId)) {
    throw new Error(
      "DLQ positive probe requires one valid captured message id",
    );
  }
  return messageId;
}

function validateIdempotencyKey(idempotencyKey) {
  if (
    typeof idempotencyKey !== "string" ||
    idempotencyKey.length > 128 ||
    !IDEMPOTENCY_KEY.test(idempotencyKey)
  ) {
    throw new Error("DLQ positive probe idempotency key is invalid");
  }
  return idempotencyKey;
}

function validateSecret(secret, label) {
  if (typeof secret !== "string" || !secret.trim()) {
    throw new Error(`${label} is required`);
  }
  return secret.trim();
}

function createSignature(timestamp, idempotencyKey, body, signingSecret) {
  return createHmac("sha256", signingSecret)
    .update(`${timestamp}\n${idempotencyKey}\n${body}`)
    .digest("base64url");
}

function parseJsonResponse(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("DLQ replay returned a non-object JSON response");
  }
  return body;
}

function positiveReplayResult(response, body) {
  const requestedCount = Number(body.requestedCount);
  const queuedCount = Number(body.queuedCount);
  const skippedCount = Number(body.skippedCount);
  const failedCount = Number(body.failedCount);
  if (
    (response.status !== 202 && response.status !== 200) ||
    body.status !== "completed" ||
    requestedCount !== 1 ||
    queuedCount !== 1 ||
    skippedCount !== 0 ||
    failedCount !== 0
  ) {
    throw new Error(
      `DLQ positive probe did not queue one message (HTTP ${response.status}; status=${String(body.status)}, requested=${String(body.requestedCount)}, queued=${String(body.queuedCount)}, skipped=${String(body.skippedCount)}, failed=${String(body.failedCount)})`,
    );
  }
  return {
    status: body.status,
    requestedCount,
    queuedCount,
    skippedCount,
    failedCount,
  };
}

/**
 * Replay one already-captured staging message and require a positive queue
 * admission response. This does not wait for the asynchronous job to finish.
 */
export async function runDlqPositiveProbe({
  edgeUrl = resolveStagingEdgeUrl(),
  messageId,
  adminKey,
  signingSecret,
  idempotencyKey,
  nowSeconds = Math.floor(Date.now() / 1_000),
  fetchImpl = fetch,
} = {}) {
  const base = resolveStagingEdgeUrl(edgeUrl);
  const validMessageId = validateMessageId(messageId);
  const validAdminKey = validateSecret(adminKey, "DLQ probe admin key");
  const validSigningSecret = validateSecret(
    signingSecret,
    "DLQ probe signing secret",
  );
  if (validSigningSecret.length < 32) {
    throw new Error("DLQ probe signing secret must be at least 32 bytes");
  }
  const validIdempotencyKey = validateIdempotencyKey(
    idempotencyKey ||
      `cf-dlq-probe-${nowSeconds}-${randomBytes(6).toString("hex")}`,
  );
  if (!Number.isSafeInteger(nowSeconds) || nowSeconds < 0) {
    throw new Error("DLQ probe timestamp must be a non-negative integer");
  }
  const body = JSON.stringify({ message_ids: [validMessageId] });
  const response = await fetchImpl(`${base}/internal/cf/jobs/dlq/replay`, {
    method: "POST",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    headers: {
      "content-type": "application/json",
      "secret-key": validAdminKey,
      "idempotency-key": validIdempotencyKey,
      "x-dlq-replay-timestamp": String(nowSeconds),
      "x-dlq-replay-signature": createSignature(
        nowSeconds,
        validIdempotencyKey,
        body,
        validSigningSecret,
      ),
    },
    body,
  });
  let rawBody = "";
  try {
    rawBody = await response.text();
  } catch {
    throw new Error(
      `DLQ replay response could not be read (HTTP ${response.status})`,
    );
  }
  let result;
  try {
    result = parseJsonResponse(JSON.parse(rawBody));
  } catch (error) {
    if (
      error instanceof Error &&
      error.message.startsWith("DLQ replay response")
    ) {
      throw error;
    }
    throw new Error(
      `DLQ replay returned invalid JSON (HTTP ${response.status})`,
    );
  }
  const positive = positiveReplayResult(response, result);
  return {
    endpoint: `${base}/internal/cf/jobs/dlq/replay`,
    messageId: validMessageId,
    idempotencyKey: validIdempotencyKey,
    ...positive,
  };
}

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--"))
      throw new Error(`unknown argument: ${argument}`);
    const key = argument.slice(2);
    if (key !== "message-id" && key !== "idempotency-key") {
      throw new Error(`unknown argument: --${key}`);
    }
    if (!key || index + 1 >= argv.length || argv[index + 1].startsWith("--")) {
      throw new Error(`argument requires a value: --${key}`);
    }
    values[key] = argv[index + 1];
    index += 1;
  }
  return values;
}

function usage() {
  return [
    "Usage: node scripts/dlq-positive-probe.mjs --message-id <captured-id>",
    "Environment: CLOUDFLARE_EDGE_URL (staging workers.dev only),",
    "  CLOUDFLARE_DLQ_ADMIN_KEY, CLOUDFLARE_DLQ_REPLAY_SIGNING_SECRET",
    "Optional: --idempotency-key <key> (re-use only to inspect one receipt)",
  ].join("\n");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const messageId =
    args["message-id"] || envValue(process.env, "CLOUDFLARE_DLQ_MESSAGE_ID");
  if (!messageId) throw new Error(usage());
  const result = await runDlqPositiveProbe({
    edgeUrl: resolveStagingEdgeUrl(),
    messageId,
    idempotencyKey: args["idempotency-key"],
    adminKey: envValue(process.env, "CLOUDFLARE_DLQ_ADMIN_KEY"),
    signingSecret: envValue(
      process.env,
      "CLOUDFLARE_DLQ_REPLAY_SIGNING_SECRET",
    ),
  });
  console.log(
    `Cloudflare staging DLQ positive probe passed: ${JSON.stringify(result)}`,
  );
}

if (process.argv[1]?.endsWith("dlq-positive-probe.mjs")) {
  main().catch((error) => {
    console.error(
      `Cloudflare staging DLQ positive probe failed: ${error instanceof Error ? error.message : "unknown error"}`,
    );
    process.exitCode = 1;
  });
}
