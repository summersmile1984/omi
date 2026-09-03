#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash, createHmac } from "node:crypto";
import { readFile, stat, writeFile } from "node:fs/promises";
import { planChatHistoryReconciliation } from "./chat-history-reconcile.mjs";

const MAX_EXPORT_BYTES = 8 * 1024 * 1024;
const MAX_APPLY_ENTRIES = 20;
const SHA256 = /^[0-9a-f]{64}$/;
const CHAT_COLLECTIONS = [
  "users/{uid}/chat_sessions",
  "users/{uid}/messages",
];

function fail(message) {
  throw new Error(`chat history export verification: ${message}`);
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
      .join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) fail("plan contains an unsupported value");
  return encoded;
}

function requiredSha(value, field) {
  if (typeof value !== "string" || !SHA256.test(value))
    fail(`${field} must be lowercase SHA-256`);
  return value;
}

function parseExportBytes(bytes) {
  if (!Buffer.isBuffer(bytes) && !(bytes instanceof Uint8Array))
    fail("export bytes must be a Buffer or Uint8Array");
  if (bytes.byteLength < 1 || bytes.byteLength > MAX_EXPORT_BYTES)
    fail(`export must be between 1 and ${MAX_EXPORT_BYTES} bytes`);
  let parsed;
  try {
    parsed = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(bytes),
    );
  } catch {
    fail("export is not valid UTF-8 JSON");
  }
  const object = objectValue(parsed);
  const source = objectValue(object?.source);
  if (!object || !source) fail("export must contain a source object");
  if (source.kind !== "firestore")
    fail("export source.kind must be firestore");
  if (
    !Array.isArray(source.collections) ||
    source.collections.length !== CHAT_COLLECTIONS.length ||
    new Set(source.collections).size !== CHAT_COLLECTIONS.length ||
    CHAT_COLLECTIONS.some((collection) => !source.collections.includes(collection))
  ) {
    fail("export source.collections must identify both chat collections");
  }
  return { object, source };
}

/**
 * Verify a bounded Firestore chat-history export and feed the parsed export
 * into the canonical offline planner. The digest is computed over the exact
 * bytes before UTF-8 decoding, JSON parsing, or key reordering. Exports may
 * omit source.export_sha256 because embedding a digest in the bytes would be
 * circular; when present, it must match the original bytes.
 */
export function verifyChatHistoryExport(
  bytes,
  { expectedSha256 = null, maxEntities = 5_000, fencedUids = [] } = {},
) {
  const { object, source } = parseExportBytes(bytes);
  const computedSha256 = sha256(bytes);
  if (
    expectedSha256 !== null &&
    (typeof expectedSha256 !== "string" ||
      !SHA256.test(expectedSha256) ||
      expectedSha256 !== computedSha256)
  ) {
    fail("export checksum does not match --expected-sha256");
  }
  if (
    source.export_sha256 !== undefined &&
    (typeof source.export_sha256 !== "string" ||
      source.export_sha256 !== computedSha256)
  ) {
    fail("source.export_sha256 does not match the original export bytes");
  }
  const manifest = {
    ...object,
    source: {
      ...source,
      export_sha256: computedSha256,
    },
  };
  const plan = planChatHistoryReconciliation(manifest, {
    maxEntities,
    fencedUids,
  });
  return {
    verified: expectedSha256 !== null,
    export_sha256: computedSha256,
    export_bytes: bytes.byteLength,
    plan,
  };
}

function orderedEntries(plan) {
  return [...plan.entries].sort((left, right) =>
    `${left.uid}\0${left.entityKind}\0${left.entityId}`.localeCompare(
      `${right.uid}\0${right.entityKind}\0${right.entityId}`,
    ),
  );
}

function applyablePlan(plan) {
  if (!plan || typeof plan !== "object" || plan.mode !== "reviewed-plan")
    fail("plan is invalid");
  if (!Array.isArray(plan.entries) || plan.total !== plan.entries.length)
    fail("plan entries do not match total");
  if (plan.entries.length < 1 || plan.entries.length > MAX_APPLY_ENTRIES)
    fail(`apply supports 1-${MAX_APPLY_ENTRIES} entries per request`);
  if (plan.stage !== plan.entries.length || plan.blocked !== 0)
    fail("only an all-stage, unblocked planner result may be applied");
  requiredSha(plan.manifestHash, "plan.manifestHash");
  const source = objectValue(plan.source);
  requiredSha(source?.export_sha256, "plan.source.export_sha256");
  const entries = orderedEntries(plan);
  for (const [index, entry] of entries.entries()) {
    if (!entry || entry.action !== "stage" || entry.status !== "planned")
      fail(`entry ${index + 1} is not staged and planned`);
    if (entry.lastError !== null)
      fail(`entry ${index + 1} has an error`);
    requiredSha(entry.sourceFingerprint, `entry ${index + 1}.sourceFingerprint`);
    requiredSha(entry.sourceExportSha256, `entry ${index + 1}.sourceExportSha256`);
    requiredSha(entry.sourceRowSha256, `entry ${index + 1}.sourceRowSha256`);
    requiredSha(entry.importId, `entry ${index + 1}.importId`);
    requiredSha(entry.planHash, `entry ${index + 1}.planHash`);
    if (entry.sourceExportSha256 !== source.export_sha256)
      fail(`entry ${index + 1} source export does not match the plan`);
    if (!Array.isArray(entry.fileIds) || entry.fileIds.length !== 0)
      fail(`entry ${index + 1} contains unverified file references`);
  }
  return entries;
}

function signaturePayload(plan, batchId, entries) {
  return stableJson({
    batch_id: batchId,
    manifest_sha256: plan.manifestHash,
    entries: entries.map((entry) => ({
      uid: entry.uid,
      entity_kind: entry.entityKind,
      entity_id: entry.entityId,
      account_generation: entry.accountGeneration,
      source_fingerprint: entry.sourceFingerprint,
      source_export_sha256: entry.sourceExportSha256,
      source_row_sha256: entry.sourceRowSha256,
      import_id: entry.importId,
      plan_hash: entry.planHash,
      action: "stage",
      status: "planned",
      last_error: null,
    })),
  });
}

/**
 * Build the exact content-bound HMAC expected by the Jobs apply endpoint.
 * Secrets are accepted only as arguments, used in memory, and never returned.
 */
export function signChatHistoryPlan(plan, signingSecret) {
  const entries = applyablePlan(plan);
  if (typeof signingSecret !== "string" || signingSecret.length < 32)
    fail("signing secret must contain at least 32 characters");
  const batchId = sha256(
    `${plan.manifestHash}\0${entries.map((entry) => entry.importId).join("\0")}`,
  );
  const signature = createHmac("sha256", signingSecret)
    .update(signaturePayload(plan, batchId, entries))
    .digest("base64url");
  return { batchId, signature };
}

async function responseJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/**
 * Apply a verified, reviewed plan directly to the existing Jobs endpoint.
 * The caller must establish independent export verification before calling
 * this function; the CLI enforces that by requiring --expected-sha256 when
 * --apply is used.
 */
export async function applyChatHistoryPlan(
  plan,
  { endpoint, adminKey, signingSecret, fetchImpl = globalThis.fetch },
) {
  if (!endpoint || typeof endpoint !== "string")
    fail("apply endpoint is required");
  if (!adminKey || typeof adminKey !== "string") fail("admin key is required");
  if (typeof fetchImpl !== "function")
    fail("fetch implementation is unavailable");
  const { batchId, signature } = signChatHistoryPlan(plan, signingSecret);
  const response = await fetchImpl(endpoint.replace(/\/$/, ""), {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "secret-key": adminKey,
      "x-chat-history-plan-signature": signature,
    },
    body: JSON.stringify({ ...plan, batch_id: batchId }),
  });
  const body = await responseJson(response);
  if (response.status !== 200 || body?.status !== "applied")
    fail(`apply endpoint returned HTTP ${response.status}`);
  return {
    batch_id: batchId,
    status: "applied",
    manifest_sha256: body.manifest_sha256,
    entry_count: body.entry_count,
    applied_count: body.applied_count,
    already_applied_count: body.already_applied_count,
  };
}

function argument(args, name) {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : null;
}

async function readExportFile(filename) {
  if (!filename || filename.startsWith("--")) fail("--export is required");
  const metadata = await stat(filename);
  if (metadata.size > MAX_EXPORT_BYTES)
    fail(`export exceeds ${MAX_EXPORT_BYTES} bytes`);
  const bytes = await readFile(filename);
  if (bytes.byteLength > MAX_EXPORT_BYTES)
    fail(`export exceeds ${MAX_EXPORT_BYTES} bytes`);
  return bytes;
}

async function main() {
  const args = process.argv.slice(2);
  const filename = argument(args, "--export");
  const bytes = await readExportFile(filename);
  const expectedSha256 = argument(args, "--expected-sha256");
  const result = verifyChatHistoryExport(bytes, { expectedSha256 });
  if (args.includes("--apply")) {
    if (!result.verified) fail("--apply requires --expected-sha256");
    const endpoint = argument(args, "--apply");
    const adminKeyEnv = argument(args, "--admin-key-env") || "ADMIN_KEY";
    const signingSecretEnv =
      argument(args, "--signing-secret-env") ||
      "CHAT_HISTORY_IMPORT_SIGNING_SECRET";
    if (!endpoint || endpoint.startsWith("--"))
      fail("--apply requires the Jobs apply endpoint URL");
    result.apply = await applyChatHistoryPlan(result.plan, {
      endpoint,
      adminKey: process.env[adminKeyEnv],
      signingSecret: process.env[signingSecretEnv],
    });
  }
  const output = `${JSON.stringify(result, null, 2)}\n`;
  const outputFilename = argument(args, "--output");
  if (outputFilename)
    await writeFile(outputFilename, output, { encoding: "utf8", mode: 0o600 });
  else process.stdout.write(output);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(
      `${error instanceof Error ? error.message : String(error)}\n`,
    );
    process.exitCode = 2;
  });
}
