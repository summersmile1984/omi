#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { planChatHistoryReconciliation } from "./chat-history-reconcile.mjs";
import { planPersonaAppHistory } from "./persona-app-history-reconcile.mjs";

// This is an offline operator artifact generator.  It deliberately has no
// Firestore, D1, R2, provider, or fetch dependency.  The JSON is reviewed and
// submitted by a separate operator step; this script never calls the admin
// attestation endpoint and never performs memory re-encryption.
const SCHEMA_VERSION = 1;
const MAX_APP_PROJECTION_ROWS = 500;
const MAX_MEMORY_PROJECTION_ROWS = 1_000_000;
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;
const SOURCE_UID = /^fb-anon-([0-9a-f]{64})$/;
const UID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;

function fail(message) {
  throw new Error(`app owner attestation: ${message}`);
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`)
      .join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) fail("value is not JSON serializable");
  return encoded;
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function requiredHash(value, field) {
  if (typeof value !== "string" || !SHA256.test(value))
    fail(`${field} must be lowercase SHA-256`);
  return value;
}

function requiredUid(value, field) {
  if (typeof value !== "string" || !UID.test(value))
    fail(`${field} is invalid`);
  return value;
}

function requiredInteger(value, field, maximum = Number.MAX_SAFE_INTEGER) {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > maximum)
    fail(`${field} is invalid`);
  return parsed;
}

function sourceExportHash(source, field) {
  const value = objectValue(source);
  if (!value) fail(`${field}.source is missing`);
  return requiredHash(value.export_sha256, `${field}.source.export_sha256`);
}

function personaManifestHash(plan) {
  return sha256(
    stableJson({
      schema_version: SCHEMA_VERSION,
      source: plan.source,
      rows: plan.entries.map((entry) => entry.sourceRowSha256),
    }),
  );
}

function chatManifestHash(plan) {
  return sha256(
    stableJson({
      schema_version: plan.schemaVersion,
      source: plan.source,
      entries: plan.entries.map((entry) => entry.sourceRowSha256),
    }),
  );
}

function validatePersonaPlan(plan) {
  if (!objectValue(plan) || plan.mode !== "dry-run")
    fail("persona input is not a planner output");
  if (plan.schema_version !== SCHEMA_VERSION || !Array.isArray(plan.entries))
    fail("persona plan schema is invalid");
  const exportSha256 = sourceExportHash(plan.source, "persona");
  if (
    plan.source.kind !== "firestore" ||
    plan.source.collection !== "plugins_data"
  )
    fail("persona source must be the plugins_data Firestore collection");
  if (plan.total !== plan.entries.length)
    fail("persona plan total does not match entries");
  const stage = plan.entries.filter((entry) => entry.action === "stage").length;
  const blocked = plan.entries.filter(
    (entry) => entry.action === "blocked",
  ).length;
  if (
    stage !== plan.stage ||
    blocked !== plan.blocked ||
    stage + blocked !== plan.total
  )
    fail("persona plan counts do not match entries");
  if (
    typeof plan.manifest_sha256 !== "string" ||
    !SHA256.test(plan.manifest_sha256)
  )
    fail("persona plan manifest checksum is invalid");
  if (personaManifestHash(plan) !== plan.manifest_sha256)
    fail("persona plan manifest checksum does not match entries");
  for (const [index, entry] of plan.entries.entries()) {
    if (!objectValue(entry)) fail(`persona entry ${index + 1} is invalid`);
    if (entry.action !== "stage" && entry.action !== "blocked")
      fail(`persona entry ${index + 1} action is invalid`);
    requiredHash(
      entry.sourceRowSha256,
      `persona entry ${index + 1}.sourceRowSha256`,
    );
    if (entry.action === "stage") {
      const sourceMatch =
        typeof entry.sourceRef === "string"
          ? SOURCE_UID.exec(entry.sourceRef)
          : null;
      if (!sourceMatch || entry.sourceUidHash !== sourceMatch[1])
        fail(`persona entry ${index + 1} source identity is invalid`);
      if (entry.sourceExportSha256 !== exportSha256)
        fail(`persona entry ${index + 1} source export does not match plan`);
      requiredHash(
        entry.sourceFingerprint,
        `persona entry ${index + 1}.sourceFingerprint`,
      );
      requiredUid(entry.uid, `persona entry ${index + 1}.uid`);
      requiredUid(entry.appId, `persona entry ${index + 1}.appId`);
      requiredHash(
        entry.sourceProjectionRevision,
        `persona entry ${index + 1}.sourceProjectionRevision`,
      );
      requiredInteger(
        entry.targetAccountGeneration,
        `persona entry ${index + 1}.targetAccountGeneration`,
      );
    }
  }
  if (plan.blocked !== 0)
    fail(`persona plan is incomplete: ${plan.blocked} blocked entries`);
  if (plan.stage > MAX_APP_PROJECTION_ROWS)
    fail(`persona plan exceeds ${MAX_APP_PROJECTION_ROWS} app rows`);
  return plan;
}

function validateChatPlan(plan) {
  if (!objectValue(plan) || plan.mode !== "reviewed-plan")
    fail("chat input is not a planner output");
  if (plan.schemaVersion !== SCHEMA_VERSION || !Array.isArray(plan.entries))
    fail("chat plan schema is invalid");
  const exportSha256 = sourceExportHash(plan.source, "chat");
  if (
    plan.source.kind !== "firestore" ||
    !Array.isArray(plan.source.collections) ||
    plan.source.collections.length !== 2
  )
    fail("chat source must contain both Firestore chat collections");
  if (plan.total !== plan.entries.length)
    fail("chat plan total does not match entries");
  const stage = plan.entries.filter((entry) => entry.action === "stage").length;
  const blocked = plan.entries.filter(
    (entry) => entry.action === "blocked",
  ).length;
  if (
    stage !== plan.stage ||
    blocked !== plan.blocked ||
    stage + blocked !== plan.total
  )
    fail("chat plan counts do not match entries");
  if (typeof plan.manifestHash !== "string" || !SHA256.test(plan.manifestHash))
    fail("chat plan manifest checksum is invalid");
  if (chatManifestHash(plan) !== plan.manifestHash)
    fail("chat plan manifest checksum does not match entries");
  for (const [index, entry] of plan.entries.entries()) {
    if (!objectValue(entry)) fail(`chat entry ${index + 1} is invalid`);
    if (entry.action !== "stage" && entry.action !== "blocked")
      fail(`chat entry ${index + 1} action is invalid`);
    requiredHash(
      entry.sourceRowSha256,
      `chat entry ${index + 1}.sourceRowSha256`,
    );
    if (entry.action === "stage") {
      if (entry.sourceExportSha256 !== exportSha256)
        fail(`chat entry ${index + 1} source export does not match plan`);
      requiredUid(entry.uid, `chat entry ${index + 1}.uid`);
      if (entry.entityKind !== "session" && entry.entityKind !== "message")
        fail(`chat entry ${index + 1}.entityKind is invalid`);
      requiredUid(entry.entityId, `chat entry ${index + 1}.entityId`);
      requiredInteger(
        entry.accountGeneration,
        `chat entry ${index + 1}.accountGeneration`,
      );
      requiredHash(
        entry.sourceFingerprint,
        `chat entry ${index + 1}.sourceFingerprint`,
      );
      requiredHash(entry.planHash, `chat entry ${index + 1}.planHash`);
      if (!Array.isArray(entry.fileIds) || entry.fileIds.length !== 0)
        fail(`chat entry ${index + 1} contains unverified file references`);
    }
  }
  if (plan.blocked !== 0)
    fail(`chat plan is incomplete: ${plan.blocked} blocked entries`);
  return plan;
}

function normalizePersonaInput(input) {
  return objectValue(input)?.mode === "dry-run"
    ? validatePersonaPlan(input)
    : validatePersonaPlan(planPersonaAppHistory(input));
}

function normalizeChatInput(input) {
  return objectValue(input)?.mode === "reviewed-plan"
    ? validateChatPlan(input)
    : validateChatPlan(planChatHistoryReconciliation(input));
}

function uniqueValues(entries, field) {
  return [...new Set(entries.map((entry) => entry[field]))];
}

function oneOrNull(values, field) {
  if (values.length > 1) fail(`planner outputs disagree on ${field}`);
  return values[0] ?? null;
}

function resolveValue(explicit, inferred, field) {
  if (explicit !== undefined && explicit !== null) {
    if (inferred !== null && explicit !== inferred)
      fail(`${field} does not match planner outputs`);
    return explicit;
  }
  if (inferred === null)
    fail(`${field} is required when planners have no staged rows`);
  return inferred;
}

function memoryValue(options) {
  const count = requiredInteger(
    options.memoryProjectionCount,
    "memoryProjectionCount",
    MAX_MEMORY_PROJECTION_ROWS,
  );
  const status = options.memoryReencryptionStatus;
  if (status !== "not_required" && status !== "completed")
    fail("memoryReencryptionStatus must be not_required or completed");
  const revision =
    options.memoryReencryptionRevision === undefined ||
    options.memoryReencryptionRevision === null
      ? null
      : options.memoryReencryptionRevision;
  if (count === 0 && status !== "not_required")
    fail("zero memory rows require memoryReencryptionStatus=not_required");
  if (count > 0 && status !== "completed")
    fail("non-zero memory rows require memoryReencryptionStatus=completed");
  if (status === "completed")
    requiredHash(revision, "memoryReencryptionRevision");
  if (status === "not_required" && revision !== null)
    fail(
      "not_required memory attestation cannot carry a re-encryption revision",
    );
  return {
    memoryProjectionCount: count,
    memoryReencryptionStatus: status,
    memoryReencryptionRevision: revision,
  };
}

function reviewSummary(plan, manifestHash, sourceExportSha256, kind) {
  return {
    kind,
    manifestHash,
    sourceExportSha256,
    total: plan.total,
    staged: plan.stage,
    blocked: plan.blocked,
    entryHashes: plan.entries.map((entry) => entry.sourceRowSha256).sort(),
  };
}

export function buildAppOwnerDataAttestation({
  persona,
  chat,
  sourceUid,
  sourceProofHash,
  sourceProjectionRevision,
  targetUid,
  targetAccountGeneration,
  memoryProjectionCount,
  memoryReencryptionStatus,
  memoryReencryptionRevision,
}) {
  const personaPlan = normalizePersonaInput(persona);
  const chatPlan = normalizeChatInput(chat);
  const personaEntries = personaPlan.entries.filter(
    (entry) => entry.action === "stage",
  );
  const chatEntries = chatPlan.entries.filter(
    (entry) => entry.action === "stage",
  );
  const sourceRefs = uniqueValues(personaEntries, "sourceRef");
  const sourceRevisions = uniqueValues(
    personaEntries,
    "sourceProjectionRevision",
  );
  const targetUids = [
    ...uniqueValues(personaEntries, "uid"),
    ...uniqueValues(chatEntries, "uid"),
  ];
  const generations = [
    ...uniqueValues(personaEntries, "targetAccountGeneration"),
    ...uniqueValues(chatEntries, "accountGeneration"),
  ];
  const resolvedSourceUid = requiredUid(sourceUid, "sourceUid");
  const sourceMatch = SOURCE_UID.exec(resolvedSourceUid);
  if (!sourceMatch) fail("sourceUid must be hash-only fb-anon-<sha256>");
  if (sourceRefs.some((value) => value !== resolvedSourceUid))
    fail("sourceUid does not match persona planner outputs");
  const resolvedProofHash = requiredHash(sourceProofHash, "sourceProofHash");
  const resolvedSourceRevision = requiredHash(
    sourceProjectionRevision,
    "sourceProjectionRevision",
  );
  if (sourceRevisions.some((value) => value !== resolvedSourceRevision))
    fail("sourceProjectionRevision does not match persona planner outputs");
  const inferredTargetUid = oneOrNull([...new Set(targetUids)], "target uid");
  const resolvedTargetUid = requiredUid(
    resolveValue(targetUid, inferredTargetUid, "targetUid"),
    "targetUid",
  );
  const inferredGeneration = oneOrNull(
    [...new Set(generations)],
    "account generation",
  );
  const resolvedGeneration = requiredInteger(
    resolveValue(
      targetAccountGeneration,
      inferredGeneration,
      "targetAccountGeneration",
    ),
    "targetAccountGeneration",
  );
  const memory = memoryValue({
    memoryProjectionCount,
    memoryReencryptionStatus,
    memoryReencryptionRevision,
  });
  const appProjectionCount = personaEntries.length;
  const revisionInput = {
    schema_version: SCHEMA_VERSION,
    source_uid: resolvedSourceUid,
    source_uid_hash: sourceMatch[1],
    source_proof_hash: resolvedProofHash,
    source_projection_revision: resolvedSourceRevision,
    target_uid: resolvedTargetUid,
    target_account_generation: resolvedGeneration,
    app_projection_count: appProjectionCount,
    memory_projection_count: memory.memoryProjectionCount,
    memory_reencryption_status: memory.memoryReencryptionStatus,
    memory_reencryption_revision: memory.memoryReencryptionRevision,
    persona_manifest_sha256: personaPlan.manifest_sha256,
    chat_manifest_sha256: chatPlan.manifestHash,
    persona_entry_sha256: personaPlan.entries
      .map((entry) => entry.sourceRowSha256)
      .sort(),
    chat_entry_sha256: chatPlan.entries
      .map((entry) => entry.sourceRowSha256)
      .sort(),
  };
  const attestation = {
    source_uid: resolvedSourceUid,
    source_uid_hash: sourceMatch[1],
    source_proof_hash: resolvedProofHash,
    source_projection_revision: resolvedSourceRevision,
    target_uid: resolvedTargetUid,
    target_account_generation: resolvedGeneration,
    data_projection_revision: sha256(stableJson(revisionInput)),
    app_projection_count: appProjectionCount,
    memory_projection_count: memory.memoryProjectionCount,
    memory_reencryption_status: memory.memoryReencryptionStatus,
    memory_reencryption_revision: memory.memoryReencryptionRevision,
  };
  const review = {
    schema_version: SCHEMA_VERSION,
    kind: "app_owner_data_attestation_review",
    status: "ready_for_review",
    attestation,
    evidence: {
      persona: reviewSummary(
        personaPlan,
        personaPlan.manifest_sha256,
        sourceExportHash(personaPlan.source, "persona"),
        "persona_app_history",
      ),
      chat: reviewSummary(
        chatPlan,
        chatPlan.manifestHash,
        sourceExportHash(chatPlan.source, "chat"),
        "chat_history",
      ),
      contentBoundRevisionInput: revisionInput,
    },
    safety: {
      firestore_connected: false,
      d1_connected: false,
      admin_endpoint_called: false,
      memory_reencryption_performed: false,
    },
  };
  return review;
}

function assertReview(review) {
  if (
    !objectValue(review) ||
    review.kind !== "app_owner_data_attestation_review" ||
    review.status !== "ready_for_review" ||
    !objectValue(review.attestation)
  ) {
    fail("review is invalid");
  }
  const attestation = review.attestation;
  const sourceMatch = SOURCE_UID.exec(attestation.source_uid || "");
  if (!sourceMatch || attestation.source_uid_hash !== sourceMatch[1])
    fail("review source identity is invalid");
  for (const field of [
    "source_proof_hash",
    "source_projection_revision",
    "data_projection_revision",
  ]) {
    requiredHash(attestation[field], `attestation.${field}`);
  }
  requiredUid(attestation.target_uid, "attestation.target_uid");
  requiredInteger(
    attestation.target_account_generation,
    "attestation.target_account_generation",
  );
  requiredInteger(
    attestation.app_projection_count,
    "attestation.app_projection_count",
    MAX_APP_PROJECTION_ROWS,
  );
  const memory = memoryValue({
    memoryProjectionCount: attestation.memory_projection_count,
    memoryReencryptionStatus: attestation.memory_reencryption_status,
    memoryReencryptionRevision: attestation.memory_reencryption_revision,
  });
  if (
    memory.memoryReencryptionRevision !==
      attestation.memory_reencryption_revision ||
    memory.memoryProjectionCount !== attestation.memory_projection_count
  ) {
    fail("review memory evidence is invalid");
  }
  return review;
}

function sqlLiteral(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") return String(value);
  return `'${String(value).replaceAll("'", "''")}'`;
}

export function renderAppOwnerDataAttestationSql(review) {
  assertReview(review);
  const a = review.attestation;
  const persona = review.evidence.persona;
  const chat = review.evidence.chat;
  return [
    "-- REVIEW ONLY: generated app-owner data attestation evidence.",
    "-- This SQL performs no DML write and must not be used to bypass the admin writer.",
    "-- The operator must review the JSON and submit the attestation through the separately gated workflow.",
    "WITH expected AS (",
    "  SELECT",
    `    ${sqlLiteral(a.source_uid)} AS source_uid,`,
    `    ${sqlLiteral(a.source_uid_hash)} AS source_uid_hash,`,
    `    ${sqlLiteral(a.source_proof_hash)} AS source_proof_hash,`,
    `    ${sqlLiteral(a.source_projection_revision)} AS source_projection_revision,`,
    `    ${sqlLiteral(a.target_uid)} AS target_uid,`,
    `    ${a.target_account_generation} AS target_account_generation,`,
    `    ${sqlLiteral(a.data_projection_revision)} AS data_projection_revision,`,
    `    ${a.app_projection_count} AS app_projection_count,`,
    `    ${a.memory_projection_count} AS memory_projection_count,`,
    `    ${sqlLiteral(a.memory_reencryption_status)} AS memory_reencryption_status,`,
    `    ${sqlLiteral(a.memory_reencryption_revision)} AS memory_reencryption_revision,`,
    `    ${sqlLiteral(persona.manifestHash)} AS persona_manifest_sha256,`,
    `    ${sqlLiteral(chat.manifestHash)} AS chat_manifest_sha256,`,
    `    ${sqlLiteral(review.status)} AS review_status`,
    ")",
    "SELECT expected.*,",
    "  CASE WHEN (memory_projection_count = 0 AND memory_reencryption_status = 'not_required')",
    "         OR (memory_projection_count > 0 AND memory_reencryption_status = 'completed' AND memory_reencryption_revision IS NOT NULL)",
    "       THEN 'memory_constraint_ok' ELSE 'memory_constraint_invalid' END AS memory_check,",
    "  'offline_only_no_write' AS execution_mode",
    "FROM expected;",
    "",
  ].join("\n");
}

export function parseAppOwnerAttestationJson(raw, label = "input") {
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
  } catch {
    fail(`${label} is not valid UTF-8 JSON`);
  }
  try {
    return JSON.parse(text);
  } catch {
    fail(`${label} is not valid UTF-8 JSON`);
  }
}

async function readJson(filename, label) {
  if (!filename || filename.startsWith("--")) fail(`${label} is required`);
  const raw = await readFile(filename);
  if (raw.byteLength > MAX_INPUT_BYTES)
    fail(`${label} exceeds ${MAX_INPUT_BYTES} bytes`);
  return parseAppOwnerAttestationJson(raw, label);
}

function flag(args, name) {
  const index = args.indexOf(name);
  return index < 0 ? undefined : args[index + 1];
}

async function main() {
  const args = process.argv.slice(2);
  const personaFile =
    flag(args, "--persona-plan") || flag(args, "--persona-input");
  const chatFile = flag(args, "--chat-plan") || flag(args, "--chat-input");
  const format = flag(args, "--format") || "bundle";
  if (!["bundle", "json", "sql"].includes(format))
    fail("--format must be bundle, json, or sql");
  const review = buildAppOwnerDataAttestation({
    persona: await readJson(personaFile, "--persona-plan"),
    chat: await readJson(chatFile, "--chat-plan"),
    sourceUid: flag(args, "--source-uid"),
    sourceProofHash: flag(args, "--source-proof-hash"),
    sourceProjectionRevision: flag(args, "--source-projection-revision"),
    targetUid: flag(args, "--target-uid"),
    targetAccountGeneration: flag(args, "--target-account-generation"),
    memoryProjectionCount: flag(args, "--memory-projection-count"),
    memoryReencryptionStatus: flag(args, "--memory-reencryption-status"),
    memoryReencryptionRevision: flag(args, "--memory-reencryption-revision"),
  });
  if (format === "sql") {
    process.stdout.write(renderAppOwnerDataAttestationSql(review));
    return;
  }
  const output =
    format === "json"
      ? review
      : { ...review, review_sql: renderAppOwnerDataAttestationSql(review) };
  process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(
      `${error instanceof Error ? error.message : String(error)}\n`,
    );
    process.exitCode = 2;
  });
}
