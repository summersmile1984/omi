#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

// This module is intentionally a file-only planner.  It does not import a
// Firestore/GCS client, call fetch, or execute D1/R2 writes.  A future apply
// step must consume a reviewed plan and perform its own generation/fence
// checks immediately before committing.
const MANIFEST_SCHEMA_VERSION = 1;
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_ROWS = 5_000;
const MAX_PUBLIC_METADATA_BYTES = 500 * 1024;
const MAX_ENVELOPE_BYTES = 400 * 1024;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;
const OPAQUE_SOURCE_REF = /^fb-anon-([0-9a-f]{64})$/;
const UID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const APP_ID = /^[^/\\\u0000-\u001f\u007f]{1,256}$/;
const REVISION = /^[^\u0000-\u001f\u007f]{1,256}$/;
const BASE64URL = /^[A-Za-z0-9_-]+$/;
const IMAGE_MIME = /^image\/[a-z0-9][a-z0-9!#$&^_.+-]*$/;
const ALLOWED_SOURCE_COLLECTIONS = new Set(["plugins_data"]);

// These keys must never be copied into the public D1 catalog projection.
// `image_object` is deliberately not listed: it is a bounded source-object
// descriptor and produces a reviewable R2 copy plan, not catalog JSON.
const PRIVATE_KEYS = new Set([
  "access_token",
  "api_key",
  "authorization",
  "avatar",
  "bearer",
  "client_secret",
  "credential",
  "credentials",
  "custom_token",
  "email",
  "firebase_id_token",
  "id_token",
  "image",
  "image_url",
  "logo",
  "logo_url",
  "mcp_oauth_tokens",
  "memory_prompt",
  "openai_api_key",
  "password",
  "persona_prompt",
  "photo_url",
  "private_key",
  "refresh_token",
  "secret",
  "secret_key",
  "token",
  "twitter",
]);

const PUBLIC_METADATA_PRIVATE_KEYS = new Set([
  ...PRIVATE_KEYS,
  "uid",
  "owner_uid",
]);

function fail(message) {
  throw new Error(`persona/app history reconciliation: ${message}`);
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
}

function bytes(value) {
  return Buffer.byteLength(value, "utf8");
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function stableJson(value) {
  if (value === null || typeof value !== "object") {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) fail("manifest contains an unsupported value");
    return encoded;
  }
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`)
    .join(",")}}`;
}

function sensitiveField(value, path = "", keys = PRIVATE_KEYS) {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const found = sensitiveField(value[index], `${path}[${index}]`, keys);
      if (found) return found;
    }
    return null;
  }
  for (const [key, nested] of Object.entries(value)) {
    const fieldPath = path ? `${path}.${key}` : key;
    if (keys.has(key.toLowerCase())) return fieldPath;
    const found = sensitiveField(nested, fieldPath, keys);
    if (found) return found;
  }
  return null;
}

function requiredText(value, field, pattern, maxBytes = 256) {
  if (typeof value !== "string" || !value || !pattern.test(value))
    fail(`${field} is invalid`);
  if (bytes(value) > maxBytes) fail(`${field} is too large`);
  return value;
}

function optionalText(value, field, pattern, maxBytes = 256) {
  if (value === undefined || value === null || value === "") return null;
  return requiredText(value, field, pattern, maxBytes);
}

function integerValue(value, field, { minimum = 0, maximum = Number.MAX_SAFE_INTEGER } = {}) {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum)
    fail(`${field} is invalid`);
  return parsed;
}

function epochValue(value, field) {
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0)
    return value;
  if (typeof value === "string" && /^\d+$/.test(value.trim())) {
    const parsed = Number(value.trim());
    if (Number.isSafeInteger(parsed) && parsed >= 0) return parsed;
  }
  if (value !== undefined && value !== null) {
    const parsed = Date.parse(String(value));
    if (!Number.isNaN(parsed)) return Math.floor(parsed / 1_000);
  }
  return null;
}

function sourceValue(value) {
  const source = objectValue(value);
  if (!source) fail("source must be an object");
  if (source.kind !== "firestore") fail("source.kind must be firestore");
  if (!ALLOWED_SOURCE_COLLECTIONS.has(source.collection)) {
    fail("source.collection must be plugins_data");
  }
  if (typeof source.export_sha256 !== "string" || !SHA256.test(source.export_sha256)) {
    fail("source.export_sha256 must be lowercase SHA-256");
  }
  const exportedAt = optionalText(source.exported_at, "source.exported_at", REVISION);
  return {
    kind: "firestore",
    collection: source.collection,
    export_sha256: source.export_sha256,
    ...(exportedAt === null ? {} : { exported_at: exportedAt }),
  };
}

function validatePublicMetadata(value, appId) {
  const metadata = objectValue(value);
  if (!metadata) return { error: "public_metadata_missing" };
  let encoded;
  try {
    encoded = stableJson(metadata);
  } catch {
    return { error: "public_metadata_not_serializable" };
  }
  if (bytes(encoded) > MAX_PUBLIC_METADATA_BYTES)
    return { error: "public_metadata_too_large" };
  const sensitive = sensitiveField(metadata, "", PUBLIC_METADATA_PRIVATE_KEYS);
  if (sensitive) return { error: `plaintext_private_field:${sensitive}` };
  if (metadata.id !== undefined && metadata.id !== appId)
    return { error: "public_metadata_id_mismatch" };
  if (metadata.capabilities !== undefined) {
    if (
      !Array.isArray(metadata.capabilities) ||
      metadata.capabilities.length > 64 ||
      metadata.capabilities.some(
        (entry) => typeof entry !== "string" || !entry || bytes(entry) > 128,
      )
    ) {
      return { error: "public_metadata_capabilities_invalid" };
    }
  }
  if (metadata.private !== undefined && typeof metadata.private !== "boolean")
    return { error: "public_metadata_private_flag_invalid" };
  return { encoded };
}

function decodeBase64Url(value, field, { minimum = 1, maximum = MAX_ENVELOPE_BYTES } = {}) {
  if (typeof value !== "string" || !BASE64URL.test(value))
    throw new Error(`${field} is invalid`);
  if (bytes(value) > maximum) throw new Error(`${field} is too large`);
  const decoded = Buffer.from(value, "base64url");
  if (decoded.length < minimum) throw new Error(`${field} is too short`);
  if (decoded.toString("base64url") !== value)
    throw new Error(`${field} is not canonical base64url`);
  return decoded;
}

function privateEnvelopeValue(value) {
  if (value === undefined || value === null) return null;
  const rawEncoded = typeof value === "string" ? value : stableJson(value);
  if (bytes(rawEncoded) > MAX_ENVELOPE_BYTES) return { error: "private_envelope_too_large" };
  const nestedSensitive = sensitiveField(value, "", PUBLIC_METADATA_PRIVATE_KEYS);
  if (nestedSensitive) return { error: `plaintext_private_field:private_envelope.${nestedSensitive}` };

  if (typeof value === "string") {
    const parts = value.split(".");
    if (
      parts.length !== 3 ||
      parts[0] !== "v1" ||
      !BASE64URL.test(parts[1]) ||
      !BASE64URL.test(parts[2])
    ) {
      return { error: "private_envelope_format_invalid" };
    }
    try {
      decodeBase64Url(parts[1], "private_envelope.iv", { minimum: 12, maximum: 128 });
      decodeBase64Url(parts[2], "private_envelope.ciphertext", { minimum: 16 });
    } catch {
      return { error: "private_envelope_ciphertext_invalid" };
    }
    return {
      format: "v1.compact-aes-gcm",
      keyVersion: null,
      sha256: sha256(rawEncoded),
    };
  }

  const envelope = objectValue(value);
  if (!envelope) return { error: "private_envelope_format_invalid" };
  const allowed = new Set([
    "version",
    "algorithm",
    "iv",
    "ciphertext",
    "key_version",
    "aad_sha256",
    "plaintext_sha256",
  ]);
  if (Object.keys(envelope).some((key) => !allowed.has(key)))
    return { error: "private_envelope_metadata_invalid" };
  if (envelope.version !== 1 || envelope.algorithm !== "A256GCM")
    return { error: "private_envelope_metadata_invalid" };
  const keyVersion = Number(envelope.key_version);
  if (!Number.isSafeInteger(keyVersion) || keyVersion < 1 || keyVersion > 32)
    return { error: "private_envelope_key_version_invalid" };
  try {
    decodeBase64Url(envelope.iv, "private_envelope.iv", { minimum: 12, maximum: 128 });
    decodeBase64Url(envelope.ciphertext, "private_envelope.ciphertext", { minimum: 16 });
  } catch {
    return { error: "private_envelope_ciphertext_invalid" };
  }
  for (const field of ["aad_sha256", "plaintext_sha256"]) {
    if (envelope[field] !== undefined && !SHA256.test(envelope[field]))
      return { error: `private_envelope_${field}_invalid` };
  }
  return {
    format: "v1.aes-gcm",
    keyVersion,
    sha256: sha256(rawEncoded),
  };
}

function sourceUriValue(value) {
  if (typeof value !== "string" || bytes(value) > 1_024) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (
    parsed.protocol !== "gs:" ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  )
    return null;
  const segments = parsed.pathname.split("/").filter(Boolean);
  if (
    !segments.length ||
    segments.some(
      (segment) => {
        let decoded;
        try {
          decoded = decodeURIComponent(segment);
        } catch {
          return true;
        }
        return (
          segment === "." ||
          segment === ".." ||
          decoded === "." ||
          decoded === ".." ||
          /[\u0000-\u001f\u007f]/.test(segment) ||
          /[\u0000-\u001f\u007f]/.test(decoded)
        );
      },
    )
  )
    return null;
  return value;
}

function imageObjectValue(value, targetUid, appId) {
  if (value === undefined || value === null) return { descriptor: null };
  const image = objectValue(value);
  if (!image) return { error: "image_object_invalid" };
  const sourceObjectUri = sourceUriValue(image.source_object_uri ?? image.gcs_uri ?? image.gcs_path);
  if (!sourceObjectUri) return { error: "image_object_source_uri_invalid" };
  if (typeof image.source_generation !== "string" || !REVISION.test(image.source_generation))
    return { error: "image_object_source_generation_invalid" };
  if (typeof image.checksum_sha256 !== "string" || !SHA256.test(image.checksum_sha256))
    return { error: "image_object_checksum_invalid" };
  const size = Number(image.size);
  if (!Number.isSafeInteger(size) || size < 1 || size > MAX_IMAGE_BYTES)
    return { error: "image_object_size_invalid" };
  if (typeof image.content_type !== "string" || !IMAGE_MIME.test(image.content_type.toLowerCase()))
    return { error: "image_object_content_type_invalid" };
  const version = image.version === undefined ? image.checksum_sha256.slice(0, 32) : image.version;
  if (typeof version !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(version))
    return { error: "image_object_version_invalid" };
  return {
    descriptor: {
      sourceObjectUri,
      sourceGeneration: image.source_generation,
      checksumSha256: image.checksum_sha256,
      size,
      contentType: image.content_type.toLowerCase(),
      destinationKey: `cf-app-logos/${targetUid}/${appId}/${version}`,
    },
  };
}

function cutoverValue(row) {
  const target = objectValue(row.target) || {};
  const cutover = objectValue(row.target_cutover ?? target.cutover);
  if (!cutover) return { error: "target_cutover_missing" };
  if (cutover.state !== "new") return { error: "target_cutover_not_new" };
  if (cutover.checkpoint_phase !== "completed") return { error: "target_cutover_incomplete" };
  if (!(cutover.destination_backend_bound === true || cutover.destination_backend_bound === 1))
    return { error: "target_backend_not_bound" };
  if (typeof cutover.deletion_fenced !== "boolean")
    return { error: "deletion_fence_status_missing" };
  if (cutover.deletion_fenced) return { error: "account_deletion_fence" };
  const accountGeneration = row.target_account_generation ?? row.account_generation ?? target.account_generation;
  try {
    return { accountGeneration: integerValue(accountGeneration, "target_account_generation") };
  } catch {
    return { error: "target_account_generation_invalid" };
  }
}

function planCandidate(row, source, index, fencedUids) {
  if (!row || typeof row !== "object" || Array.isArray(row)) fail(`row ${index + 1} must be an object`);
  const errors = [];
  const sensitive = sensitiveField(row);
  if (sensitive) errors.push(`plaintext_private_field:${sensitive}`);

  const sourceUid = row.source_uid ?? row.source_ref;
  const sourceMatch = typeof sourceUid === "string" ? OPAQUE_SOURCE_REF.exec(sourceUid) : null;
  if (!sourceMatch) errors.push("source_uid_not_opaque");
  // Never echo a raw Firebase uid into a plan. The identity projection
  // ledger's opaque `fb-anon-<hash>` reference is the only source identity a
  // later apply step may receive.
  const sourceRef = sourceMatch ? sourceUid : "";
  const sourceUidHash = sourceMatch ? sourceMatch[1] : null;
  const targetUid = row.uid ?? row.target_uid ?? objectValue(row.target)?.uid;
  if (typeof targetUid !== "string" || !UID.test(targetUid)) errors.push("uid_invalid");
  if (sourceRef && targetUid === sourceRef) errors.push("source_target_identity_same");
  const appId = row.app_id ?? row.id;
  if (typeof appId !== "string" || !APP_ID.test(appId)) errors.push("app_id_invalid");

  const sourceFingerprint = typeof row.source_fingerprint === "string" ? row.source_fingerprint : "";
  if (!SHA256.test(sourceFingerprint)) errors.push("source_fingerprint_missing_or_invalid");
  let sourceProjectionRevision = null;
  try {
    sourceProjectionRevision = optionalText(row.source_projection_revision, "source_projection_revision", REVISION);
  } catch {
    errors.push("source_projection_revision_invalid");
  }
  if (!sourceProjectionRevision) errors.push("source_projection_revision_missing");
  const createdAt = epochValue(row.created_at, "created_at");
  const updatedAt = epochValue(row.updated_at ?? row.created_at, "updated_at");
  if (createdAt === null) errors.push("created_at_invalid");
  if (updatedAt === null) errors.push("updated_at_invalid");

  const cutover = cutoverValue(row);
  if (cutover.error) errors.push(cutover.error);
  if (targetUid && fencedUids.has(targetUid)) errors.push("account_deletion_fence");
  const publicValue = row.public_metadata ?? row.public ?? row.app;
  const publicMetadata = validatePublicMetadata(publicValue, appId);
  if (publicMetadata.error) errors.push(publicMetadata.error);
  const envelope = privateEnvelopeValue(row.private_envelope ?? row.private_metadata_envelope);
  if (envelope.error) errors.push(envelope.error);
  const image = imageObjectValue(row.image_object ?? row.logo_object, targetUid || "invalid-uid", appId || "invalid-app");
  if (image.error) errors.push(image.error);

  const base = {
    sourceRef,
    sourceUidHash,
    uid: typeof targetUid === "string" && UID.test(targetUid) ? targetUid : "",
    appId: typeof appId === "string" && APP_ID.test(appId) ? appId : "",
    sourceProjectionRevision,
    targetAccountGeneration: cutover.accountGeneration ?? null,
    sourceFingerprint,
    sourceExportSha256: source.export_sha256,
    publicMetadataJson: publicMetadata.encoded ?? null,
    privateEnvelope: envelope.error || !envelope ? null : envelope,
    imageObject: image.descriptor ?? null,
    createdAt,
    updatedAt,
  };
  const sourceRowSha256 = sha256(stableJson(base));
  const requestFingerprint = sha256(`persona-app-history\0${sourceRef}\0${base.uid}\0${base.appId}\0${sourceRowSha256}`);
  const idempotencyKey = `persona-app-history-${requestFingerprint.slice(0, 40)}`;
  return {
    ...base,
    requestFingerprint,
    idempotencyKey,
    sourceRowSha256,
    action: errors.length ? "blocked" : "stage",
    status: errors.length ? "blocked" : "planned",
    lastError: errors.length ? [...new Set(errors)].join(",") : null,
  };
}

function manifestValue(input) {
  const manifest = objectValue(input);
  if (!manifest || !Array.isArray(manifest.rows)) fail("input must be a manifest object with rows");
  if (manifest.schema_version !== MANIFEST_SCHEMA_VERSION)
    fail(`schema_version must be ${MANIFEST_SCHEMA_VERSION}`);
  const source = sourceValue(manifest.source);
  return { schema_version: MANIFEST_SCHEMA_VERSION, source, rows: manifest.rows };
}

function blockConflict(entry, reason) {
  const reasons = entry.lastError ? entry.lastError.split(",") : [];
  if (!reasons.includes(reason)) reasons.push(reason);
  entry.action = "blocked";
  entry.status = "blocked";
  entry.lastError = reasons.join(",");
}

function sameEntry(left, right) {
  return left.sourceRowSha256 === right.sourceRowSha256;
}

function planManifestHash(plan) {
  return sha256(
    stableJson({
      schema_version: MANIFEST_SCHEMA_VERSION,
      source: plan.source,
      rows: plan.entries.map((entry) => entry.sourceRowSha256),
    }),
  );
}

export function planPersonaAppHistory(input, { maxRows = MAX_ROWS, fencedUids = [] } = {}) {
  const manifest = manifestValue(input);
  if (!Number.isSafeInteger(maxRows) || maxRows < 1 || maxRows > MAX_ROWS)
    fail(`maximum rows must be between 1 and ${MAX_ROWS}`);
  if (manifest.rows.length > maxRows || manifest.rows.length > MAX_ROWS)
    fail(`maximum ${Math.min(maxRows, MAX_ROWS)} rows per run`);
  const fenced = new Set(fencedUids);
  const byTargetApp = new Map();
  const bySourceApp = new Map();
  const entries = [];
  for (const [index, row] of manifest.rows.entries()) {
    const entry = planCandidate(row, manifest.source, index, fenced);
    const targetKey = `${entry.uid}\0${entry.appId}`;
    const sourceKey = `${entry.sourceRef}\0${entry.appId}`;
    const priorTarget = byTargetApp.get(targetKey);
    if (priorTarget) {
      if (!sameEntry(priorTarget, entry)) {
        blockConflict(priorTarget, "conflicting_duplicate_row");
        blockConflict(entry, "conflicting_duplicate_row");
      }
      continue;
    }
    const priorSource = bySourceApp.get(sourceKey);
    if (priorSource && (priorSource.uid !== entry.uid || !sameEntry(priorSource, entry))) {
      blockConflict(priorSource, "source_app_maps_to_multiple_targets");
      blockConflict(entry, "source_app_maps_to_multiple_targets");
    }
    byTargetApp.set(targetKey, entry);
    bySourceApp.set(sourceKey, entry);
    entries.push(entry);
  }
  entries.sort((left, right) => `${left.uid}\0${left.appId}`.localeCompare(`${right.uid}\0${right.appId}`));
  const plan = {
    mode: "dry-run",
    schema_version: MANIFEST_SCHEMA_VERSION,
    source: manifest.source,
    max_rows: maxRows,
    total: entries.length,
    stage: entries.filter((entry) => entry.action === "stage").length,
    blocked: entries.filter((entry) => entry.action === "blocked").length,
    entries,
  };
  plan.manifest_sha256 = planManifestHash(plan);
  return plan;
}

function assertPlan(plan) {
  if (!plan || plan.mode !== "dry-run" || plan.schema_version !== MANIFEST_SCHEMA_VERSION || !SHA256.test(plan.manifest_sha256 || "") || !Array.isArray(plan.entries))
    fail("plan is invalid");
  if (planManifestHash(plan) !== plan.manifest_sha256) fail("plan manifest checksum does not match entries");
  for (const entry of plan.entries) {
    if (!SHA256.test(entry.sourceRowSha256 || "") || !SHA256.test(entry.requestFingerprint || ""))
      fail("plan entry fingerprint is invalid");
  }
}

// This is a non-executable operation preview.  It intentionally contains no
// SQL and cannot be passed to wrangler.  A later apply implementation must
// re-check account generation and deletion fences at commit time.
export function renderPersonaAppHistoryOperations(plan) {
  assertPlan(plan);
  return plan.entries.filter((entry) => entry.action === "stage").map((entry) => ({
    idempotency_key: entry.idempotencyKey,
    d1: {
      operation: "insert_public_catalog_if_absent",
      table: "cf_app_catalog",
      key: { id: entry.appId, owner_uid: entry.uid },
      owner_account_generation: entry.targetAccountGeneration,
      data_json_sha256: sha256(entry.publicMetadataJson),
    },
    private: entry.privateEnvelope
      ? { operation: "store_encrypted_envelope", format: entry.privateEnvelope.format, sha256: entry.privateEnvelope.sha256, key_version: entry.privateEnvelope.keyVersion }
      : null,
    r2: entry.imageObject
      ? { operation: "copy_after_generation_check", ...entry.imageObject }
      : null,
    guards: {
      source_export_sha256: entry.sourceExportSha256,
      source_row_sha256: entry.sourceRowSha256,
      source_projection_revision: entry.sourceProjectionRevision,
      account_generation: entry.targetAccountGeneration,
      deletion_fence: "must_be_clear_at_apply_time",
    },
  }));
}

export function verifyPersonaAppHistory(plan, actualInput) {
  assertPlan(plan);
  const actualRows = Array.isArray(actualInput)
    ? actualInput
    : objectValue(actualInput)?.rows;
  if (!Array.isArray(actualRows)) fail("actual input must be an array or object with rows");
  const actualByKey = new Map();
  const duplicateActual = [];
  for (const row of actualRows) {
    if (!row || typeof row !== "object") continue;
    const key = `${row.owner_uid ?? row.uid}\0${row.id ?? row.app_id}`;
    if (actualByKey.has(key)) duplicateActual.push(key);
    actualByKey.set(key, row);
  }
  const missing = [];
  const mismatched = [];
  for (const entry of plan.entries.filter((candidate) => candidate.action === "stage")) {
    const key = `${entry.uid}\0${entry.appId}`;
    const actual = actualByKey.get(key);
    if (!actual) {
      missing.push(key);
      continue;
    }
    const reasons = [];
    if (actual.owner_uid !== entry.uid) reasons.push("owner_uid");
    if (Number(actual.owner_account_generation) !== entry.targetAccountGeneration) reasons.push("account_generation");
    let actualMetadataDigest = "";
    if (typeof actual.data_json === "string") {
      try {
        actualMetadataDigest = sha256(stableJson(JSON.parse(actual.data_json)));
      } catch {
        actualMetadataDigest = "";
      }
    }
    if (actualMetadataDigest !== sha256(entry.publicMetadataJson)) reasons.push("public_metadata");
    if (reasons.length) mismatched.push({ key, reasons });
  }
  return {
    status: missing.length || mismatched.length || duplicateActual.length ? "failed" : "passed",
    manifest_sha256: plan.manifest_sha256,
    checked: plan.stage,
    blocked: plan.blocked,
    missing,
    mismatched,
    duplicate_actual: duplicateActual,
  };
}

async function readJson(filename) {
  if (!filename || filename.startsWith("--")) fail("--input is required");
  const buffer = await readFile(filename);
  if (buffer.byteLength > MAX_INPUT_BYTES) fail(`input exceeds ${MAX_INPUT_BYTES} bytes`);
  try {
    return JSON.parse(buffer.toString("utf8"));
  } catch {
    fail("input is not valid JSON");
  }
}

async function main() {
  const args = process.argv.slice(2);
  const inputIndex = args.indexOf("--input");
  const fencedUids = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--fenced-uid" && args[index + 1]) fencedUids.push(args[++index]);
  }
  const input = await readJson(inputIndex >= 0 ? args[inputIndex + 1] : null);
  const plan = objectValue(input)?.mode === "dry-run" ? input : planPersonaAppHistory(input, { fencedUids });
  process.stdout.write(`${JSON.stringify({ ...plan, operations: renderPersonaAppHistoryOperations(plan) }, null, 2)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 2;
  });
}
