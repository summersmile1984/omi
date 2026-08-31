#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

// This planner only handles an already re-encrypted, already attested phone
// row. It deliberately does not decrypt an export, contact Twilio, read
// Firestore/GCS, or write D1. A future executor must perform those operations
// only after reviewing the generated ledger and re-checking the fences.
const MANIFEST_SCHEMA_VERSION = 1;
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_ROWS = 5_000;
const MAX_CIPHERTEXT_BYTES = 4_096;
const MAX_FRIENDLY_NAME_BYTES = 256;
const SHA256 = /^[0-9a-f]{64}$/;
const E164 = /^\+[1-9]\d{1,14}$/;
const UID = /^[^/\u0000]{1,256}$/;
const RECORD_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SID = /^[A-Za-z]{2}[A-Za-z0-9]{8,160}$/;
const CIPHERTEXT_SEGMENT = /^[A-Za-z0-9_-]+$/;
const CIPHERTEXT_SCHEME = "cloudflare-phone-aes-gcm-v1";
const PROOF_KIND = "verified-e164";
const PROOF_METHOD = "twilio-outgoing-caller-id";
const PROOF_CANONICALIZATION = "E.164";
const FORBIDDEN_KEYS = new Set([
  "access_token",
  "api_key",
  "authorization",
  "bearer",
  "client_secret",
  "credential",
  "credentials",
  "decrypted_phone",
  "decrypted_phone_number",
  "firebase_id_token",
  "id_token",
  "password",
  "e164",
  "e164_number",
  "phone",
  "phone_number",
  "phone_number_plaintext",
  "plaintext",
  "private_key",
  "raw_phone_number",
  "raw_phone",
  "raw_number",
  "refresh_token",
  "secret",
  "token",
  "verification_code",
]);

function fail(message) {
  throw new Error(`phone history reconciliation: ${message}`);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function byteLength(value) {
  return Buffer.byteLength(value, "utf8");
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
  if (encoded === undefined) fail("manifest contains an unsupported value");
  return encoded;
}

function sensitiveField(value, path = "") {
  if (!value || typeof value !== "object") {
    if (
      typeof value === "string" &&
      E164.test(value.trim().replace(/[\s\-().]+/g, ""))
    )
      return path || "value";
    return null;
  }
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const found = sensitiveField(value[index], `${path}[${index}]`);
      if (found) return found;
    }
    return null;
  }
  for (const [key, nested] of Object.entries(value)) {
    const fieldPath = path ? `${path}.${key}` : key;
    if (FORBIDDEN_KEYS.has(key.toLowerCase())) return fieldPath;
    const found = sensitiveField(nested, fieldPath);
    if (found) return found;
  }
  return null;
}

function ensureNoPlaintext(value, context) {
  const sensitive = sensitiveField(value);
  if (sensitive) fail(`${context} contains forbidden field or plaintext E.164 at ${sensitive}`);
}

function uidValue(value, context = "uid") {
  if (typeof value !== "string" || !UID.test(value)) fail(`${context} is invalid`);
  return value;
}

function recordIdValue(value, context = "source_record_id") {
  if (typeof value !== "string" || !RECORD_ID.test(value)) fail(`${context} is invalid`);
  return value;
}

function shaValue(value, field) {
  if (typeof value !== "string" || !SHA256.test(value)) fail(`${field} must be lowercase SHA-256`);
  return value;
}

function epochValue(value, field) {
  if (typeof value === "number" && Number.isSafeInteger(value) && value > 0) return value;
  if (typeof value === "string" && /^\d+$/.test(value.trim())) {
    const parsed = Number(value.trim());
    if (Number.isSafeInteger(parsed) && parsed > 0) return parsed;
  }
  if (typeof value === "string" && value.length <= 128) {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) return Math.floor(parsed / 1_000);
  }
  fail(`${field} must be a positive epoch timestamp or ISO timestamp`);
}

function integerValue(value, field) {
  const parsed = typeof value === "string" && /^\d+$/.test(value.trim())
    ? Number(value.trim())
    : value;
  if (!Number.isSafeInteger(parsed) || parsed < 0) fail(`${field} must be a non-negative integer`);
  return parsed;
}

function boolValue(value, field, fallback = false) {
  if (value === undefined || value === null) return fallback ? 1 : 0;
  if (value === true || value === 1 || value === "1") return 1;
  if (value === false || value === 0 || value === "0") return 0;
  fail(`${field} must be boolean`);
}

function parseCiphertext(value) {
  if (typeof value !== "string" || value.length === 0 || byteLength(value) > MAX_CIPHERTEXT_BYTES)
    return false;
  const parts = value.split(".");
  if (parts.length !== 2 || parts.some((part) => !CIPHERTEXT_SEGMENT.test(part))) return false;
  let iv;
  let encrypted;
  try {
    iv = Buffer.from(parts[0], "base64url");
    encrypted = Buffer.from(parts[1], "base64url");
  } catch {
    return false;
  }
  // The live Jobs owner emits a 12-byte AES-GCM nonce and an authentication
  // tag. The payload is opaque here; no decryption or plaintext comparison is
  // performed by the planner.
  return (
    iv.length === 12 &&
    encrypted.length >= 17 &&
    iv.toString("base64url") === parts[0] &&
    encrypted.toString("base64url") === parts[1]
  );
}

function textValue(value, field, maxBytes) {
  if (typeof value !== "string") fail(`${field} must be a string`);
  if (value.length === 0) return null;
  if (byteLength(value) > maxBytes || /[\0\u0000-\u001f\u007f]/.test(value))
    fail(`${field} is invalid`);
  return value;
}

function sourceValue(input) {
  const source = objectValue(input);
  if (!source) fail("source must be an object");
  ensureNoPlaintext(source, "source");
  if (source.kind !== "firestore") fail("source.kind must be firestore");
  if (source.collection !== "users/{uid}/phone_numbers")
    fail("source.collection must be users/{uid}/phone_numbers");
  if (source.ciphertext_scheme !== CIPHERTEXT_SCHEME)
    fail(`source.ciphertext_scheme must be ${CIPHERTEXT_SCHEME}`);
  if (source.proof_scheme !== "sha256-v1") fail("source.proof_scheme must be sha256-v1");
  const exportSha256 = shaValue(source.export_sha256, "source.export_sha256");
  if (source.exported_at !== undefined) textValue(source.exported_at, "source.exported_at", 128);
  return {
    kind: "firestore",
    collection: "users/{uid}/phone_numbers",
    ciphertext_scheme: CIPHERTEXT_SCHEME,
    proof_scheme: "sha256-v1",
    export_sha256: exportSha256,
    ...(source.exported_at === undefined ? {} : { exported_at: source.exported_at }),
  };
}

function proofValue(value, hash, sourceFingerprint) {
  const proof = objectValue(value);
  if (!proof) return { error: "proof_missing" };
  const sensitive = sensitiveField(proof);
  if (sensitive) return { error: `proof_sensitive_field:${sensitive}` };
  const allowed = new Set([
    "kind",
    "method",
    "canonicalization",
    "verified",
    "value_sha256",
    "source_fingerprint",
    "proof_sha256",
    "attested_at",
  ]);
  const unknown = Object.keys(proof).find((key) => !allowed.has(key));
  if (unknown) return { error: `proof_unknown_field:${unknown}` };
  if (proof.kind !== PROOF_KIND) return { error: "proof_kind_invalid" };
  if (proof.method !== PROOF_METHOD) return { error: "proof_method_invalid" };
  if (proof.canonicalization !== PROOF_CANONICALIZATION)
    return { error: "proof_canonicalization_invalid" };
  if (proof.verified !== true) return { error: "proof_not_verified" };
  if (proof.value_sha256 !== hash) return { error: "proof_hash_mismatch" };
  if (proof.source_fingerprint !== sourceFingerprint)
    return { error: "proof_source_fingerprint_mismatch" };
  if (typeof proof.proof_sha256 !== "string" || !SHA256.test(proof.proof_sha256))
    return { error: "proof_sha256_missing_or_invalid" };
  if (!Number.isSafeInteger(proof.attested_at) || proof.attested_at <= 0)
    return { error: "proof_attested_at_invalid" };
  const expected = sha256(
    stableJson({
      kind: PROOF_KIND,
      method: PROOF_METHOD,
      canonicalization: PROOF_CANONICALIZATION,
      verified: true,
      value_sha256: hash,
      source_fingerprint: sourceFingerprint,
      attested_at: proof.attested_at,
    }),
  );
  if (proof.proof_sha256 !== expected) return { error: "proof_sha256_mismatch" };
  return { proofSha256: proof.proof_sha256, attestedAt: proof.attested_at };
}

function sourceRowFingerprint(source, normalized) {
  return sha256(
    stableJson({
      collection: source.collection,
      export_sha256: source.export_sha256,
      uid: normalized.uid,
      source_record_id: normalized.sourceRecordId,
      phone_number_hash: normalized.phoneNumberHash,
      twilio_sid: normalized.twilioSid,
      friendly_name: normalized.friendlyName,
      verified_at: normalized.verifiedAt,
      is_primary: normalized.isPrimary,
      account_generation: normalized.accountGeneration,
      created_at: normalized.createdAt,
      updated_at: normalized.updatedAt,
    }),
  );
}

function sourceRowHashValue(entry) {
  return sha256(
    stableJson({
      uid: entry.uid,
      source_record_id: entry.sourceRecordId || null,
      phone_number_id: entry.phoneNumberId || null,
      phone_number_hash: entry.phoneNumberHash,
      phone_number_ciphertext: entry.phoneNumberCiphertext,
      proof_sha256: entry.proofSha256,
      source_fingerprint: entry.sourceFingerprint,
      twilio_sid: entry.twilioSid,
      friendly_name: entry.friendlyName,
      verified_at: entry.verifiedAt,
      is_primary: entry.isPrimary,
      account_generation: entry.accountGeneration,
      created_at: entry.createdAt,
      updated_at: entry.updatedAt,
    }),
  );
}

function planHashValue(entry, action, status, lastError) {
  return sha256(
    stableJson({
      import_id: entry.importId,
      source_row_sha256: entry.sourceRowSha256,
      action,
      status,
      last_error: lastError,
    }),
  );
}

function planCandidate(row, source, index) {
  if (!row || typeof row !== "object" || Array.isArray(row)) fail(`row ${index + 1} must be an object`);
  ensureNoPlaintext(row, `row ${index + 1}`);
  const errors = [];
  const uid = typeof row.uid === "string" ? row.uid : "";
  if (!UID.test(uid)) errors.push("uid_invalid");
  const sourceRecordId = row.source_record_id ?? row.id;
  if (typeof sourceRecordId !== "string" || !RECORD_ID.test(sourceRecordId)) errors.push("source_record_id_invalid");
  const phoneNumberId = row.phone_number_id ?? row.id ?? sourceRecordId;
  if (typeof phoneNumberId !== "string" || !RECORD_ID.test(phoneNumberId)) errors.push("phone_number_id_invalid");
  const hash = typeof row.phone_number_hash === "string" ? row.phone_number_hash : "";
  if (!SHA256.test(hash)) errors.push("phone_number_hash_missing_or_invalid");
  const ciphertext = typeof row.phone_number_ciphertext === "string" ? row.phone_number_ciphertext : "";
  if (!parseCiphertext(ciphertext)) errors.push("ciphertext_missing_or_invalid");
  const twilioSid = row.twilio_sid === undefined || row.twilio_sid === null || row.twilio_sid === ""
    ? null
    : typeof row.twilio_sid === "string" && SID.test(row.twilio_sid)
      ? row.twilio_sid
      : "invalid";
  if (twilioSid === "invalid") errors.push("twilio_sid_invalid");
  const friendlyName = row.friendly_name === undefined || row.friendly_name === null
    ? null
    : textValue(row.friendly_name, "friendly_name", MAX_FRIENDLY_NAME_BYTES);
  const verifiedAt = (() => {
    try {
      return epochValue(row.verified_at, "verified_at");
    } catch {
      errors.push("verified_at_invalid");
      return null;
    }
  })();
  const createdAt = (() => {
    try {
      return epochValue(row.created_at ?? row.verified_at, "created_at");
    } catch {
      errors.push("created_at_invalid");
      return null;
    }
  })();
  const updatedAt = (() => {
    try {
      return epochValue(row.updated_at ?? row.verified_at, "updated_at");
    } catch {
      errors.push("updated_at_invalid");
      return null;
    }
  })();
  const generation = (() => {
    try {
      return integerValue(row.account_generation ?? row.generation, "account_generation");
    } catch {
      errors.push("account_generation_invalid");
      return null;
    }
  })();
  const isPrimary = (() => {
    try {
      return boolValue(row.is_primary, "is_primary");
    } catch {
      errors.push("is_primary_invalid");
      return 0;
    }
  })();
  const verificationStatus = row.status ?? row.verification_status;
  if (verificationStatus !== undefined && verificationStatus !== "verified" && verificationStatus !== "completed")
    errors.push("status_not_verified");
  if (row.verified === false) errors.push("status_not_verified");
  const sourceFingerprint = typeof row.source_fingerprint === "string" ? row.source_fingerprint : "";
  if (!SHA256.test(sourceFingerprint)) errors.push("source_fingerprint_missing_or_invalid");
  let proofSha256 = null;
  let attestedAt = null;
  if (SHA256.test(hash) && SHA256.test(sourceFingerprint)) {
    const proof = proofValue(row.proof, hash, sourceFingerprint);
    if (proof.error) errors.push(proof.error);
    else {
      proofSha256 = proof.proofSha256;
      attestedAt = proof.attestedAt;
    }
  } else if (!row.proof) {
    errors.push("proof_missing");
  }
  if (sourceRecordId && uid && hash && verifiedAt !== null && createdAt !== null && updatedAt !== null && generation !== null) {
    const expectedFingerprint = sourceRowFingerprint(source, {
      uid,
      sourceRecordId,
      phoneNumberHash: hash,
      twilioSid: twilioSid === "invalid" ? null : twilioSid,
      friendlyName,
      verifiedAt,
      isPrimary,
      accountGeneration: generation,
      createdAt,
      updatedAt,
    });
    if (sourceFingerprint !== expectedFingerprint) errors.push("source_fingerprint_mismatch");
  }
  const importId = SHA256.test(sourceFingerprint)
    ? sha256(`${source.export_sha256}\0${uid}\0${sourceRecordId || ""}\0${sourceFingerprint}`)
    : sha256(`${source.export_sha256}\0${uid}\0${sourceRecordId || ""}\0missing-source-fingerprint`);
  const sourceRowSha256 = sourceRowHashValue({
    uid,
    sourceRecordId: sourceRecordId || "",
    phoneNumberId: phoneNumberId || "",
    phoneNumberHash: SHA256.test(hash) ? hash : null,
    phoneNumberCiphertext: parseCiphertext(ciphertext) ? ciphertext : null,
    proofSha256,
    sourceFingerprint: SHA256.test(sourceFingerprint) ? sourceFingerprint : null,
    twilioSid: twilioSid === "invalid" ? null : twilioSid,
    friendlyName,
    verifiedAt,
    isPrimary,
    accountGeneration: generation,
    createdAt,
    updatedAt,
  });
  const action = errors.length ? "blocked" : "stage";
  const status = errors.length ? "blocked" : "planned";
  const lastError = errors.length ? errors.join(",") : null;
  const planHash = planHashValue({ importId, sourceRowSha256 }, action, status, lastError);
  return {
    uid,
    sourceRecordId: sourceRecordId || "",
    phoneNumberId: phoneNumberId || "",
    importId,
    phoneNumberHash: SHA256.test(hash) ? hash : null,
    phoneNumberCiphertext: parseCiphertext(ciphertext) ? ciphertext : null,
    proofSha256,
    proofAttestedAt: attestedAt,
    sourceFingerprint: SHA256.test(sourceFingerprint) ? sourceFingerprint : null,
    sourceExportSha256: source.export_sha256,
    twilioSid: twilioSid === "invalid" ? null : twilioSid,
    friendlyName,
    verifiedAt,
    isPrimary,
    accountGeneration: generation,
    createdAt,
    updatedAt,
    sourceRowSha256,
    planHash,
    action,
    status,
    lastError,
    _errors: errors,
  };
}

function block(entry, reason) {
  if (!entry._errors.includes(reason)) entry._errors.push(reason);
  entry.action = "blocked";
  entry.status = "blocked";
  entry.lastError = entry._errors.join(",");
  entry.planHash = planHashValue(entry, entry.action, entry.status, entry.lastError);
}

function sourceRowHash(entry) {
  return sourceRowHashValue(entry);
}

function manifestHash(plan) {
  return sha256(
    stableJson({
      schema_version: MANIFEST_SCHEMA_VERSION,
      source: plan.source,
      rows: plan.entries.map((entry) => entry.sourceRowSha256),
    }),
  );
}

function assertPlan(plan) {
  if (!plan || plan.mode !== "dry-run" || plan.schema_version !== MANIFEST_SCHEMA_VERSION || !SHA256.test(plan.manifest_sha256 || "") || !Array.isArray(plan.entries))
    fail("plan is invalid");
  for (const entry of plan.entries) {
    if (sourceRowHash(entry) !== entry.sourceRowSha256) fail("plan row checksum does not match entry");
  }
  if (manifestHash(plan) !== plan.manifest_sha256) fail("plan manifest checksum does not match entries");
}

export function planPhoneHistory(
  input,
  { maxRows = MAX_ROWS, fencedUids = [], now = Math.floor(Date.now() / 1_000) } = {},
) {
  const manifest = objectValue(input);
  if (!manifest || !Array.isArray(manifest.rows)) fail("input must be a manifest object with rows");
  if (manifest.schema_version !== MANIFEST_SCHEMA_VERSION) fail(`schema_version must be ${MANIFEST_SCHEMA_VERSION}`);
  const source = sourceValue(manifest.source);
  if (!Number.isSafeInteger(maxRows) || maxRows < 1 || maxRows > MAX_ROWS) fail(`maximum rows must be between 1 and ${MAX_ROWS}`);
  if (!Number.isSafeInteger(now) || now <= 0) fail("now must be a positive timestamp");
  if (manifest.rows.length > maxRows || manifest.rows.length > MAX_ROWS) fail(`maximum ${Math.min(maxRows, MAX_ROWS)} rows per run`);
  const fenced = new Set(fencedUids);
  const byRecord = new Map();
  for (const [index, row] of manifest.rows.entries()) {
    const entry = planCandidate(row, source, index);
    if (fenced.has(entry.uid)) block(entry, "account_deletion_fence");
    const key = `${entry.uid}\0${entry.sourceRecordId}`;
    const prior = byRecord.get(key);
    if (!prior) byRecord.set(key, entry);
    else if (prior.sourceRowSha256 !== entry.sourceRowSha256) {
      block(prior, "conflicting_duplicate_row");
      block(entry, "conflicting_duplicate_row");
      byRecord.set(`${key}\0conflict\0${entry.importId}`, entry);
    }
  }
  const entries = [...byRecord.values()];
  const claims = new Map();
  for (const entry of entries) {
    if (entry.action !== "stage") continue;
    for (const [claimType, claimValue] of [
      ["phone_hash", entry.phoneNumberHash],
      ["twilio_sid", entry.twilioSid],
    ]) {
      if (!claimValue) continue;
      const key = `${claimType}\0${claimValue}`;
      const prior = claims.get(key);
      if (prior && prior !== entry) {
        block(prior, `conflicting_${claimType}_claim`);
        block(entry, `conflicting_${claimType}_claim`);
      } else {
        claims.set(key, entry);
      }
    }
  }
  const cleanEntries = entries
    .sort((left, right) => `${left.uid}\0${left.sourceRecordId}\0${left.importId}`.localeCompare(`${right.uid}\0${right.sourceRecordId}\0${right.importId}`))
    .map(({ _errors, ...entry }) => entry);
  const plan = {
    mode: "dry-run",
    schema_version: MANIFEST_SCHEMA_VERSION,
    source,
    manifest_sha256: null,
    max_rows: maxRows,
    total: cleanEntries.length,
    stage: cleanEntries.filter((entry) => entry.action === "stage").length,
    blocked: cleanEntries.filter((entry) => entry.action === "blocked").length,
    entries: cleanEntries,
  };
  plan.manifest_sha256 = manifestHash(plan);
  return plan;
}

function sqlLiteral(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") return String(value);
  return `'${String(value).replaceAll("'", "''")}'`;
}

export function renderPhoneHistoryLedgerSql(plan, now = Math.floor(Date.now() / 1_000)) {
  assertPlan(plan);
  if (!Number.isSafeInteger(now) || now <= 0) fail("now must be a positive timestamp");
  const statements = plan.entries.filter((entry) => entry.action === "stage").map((entry) => {
    if (!entry.phoneNumberHash || !entry.phoneNumberCiphertext || !entry.proofSha256 || !entry.sourceFingerprint || entry.accountGeneration === null || entry.verifiedAt === null || entry.createdAt === null || entry.updatedAt === null)
      fail("staged entry is missing verified phone metadata");
    return (
      "INSERT INTO cf_phone_number_import_ledger (uid, import_id, source_record_id, phone_number_id, " +
      "phone_number_hash, phone_number_ciphertext, proof_sha256, source_fingerprint, source_export_sha256, " +
      "twilio_sid, friendly_name, verified_at, is_primary, account_generation, plan_hash, manifest_sha256, action, status, " +
      "last_error, created_at, updated_at) " +
      `SELECT ${sqlLiteral(entry.uid)}, ${sqlLiteral(entry.importId)}, ${sqlLiteral(entry.sourceRecordId)}, ` +
      `${sqlLiteral(entry.phoneNumberId)}, ${sqlLiteral(entry.phoneNumberHash)}, ${sqlLiteral(entry.phoneNumberCiphertext)}, ` +
      `${sqlLiteral(entry.proofSha256)}, ${sqlLiteral(entry.sourceFingerprint)}, ${sqlLiteral(entry.sourceExportSha256)}, ` +
      `${sqlLiteral(entry.twilioSid)}, ${sqlLiteral(entry.friendlyName)}, ${entry.verifiedAt}, ${entry.isPrimary}, ` +
      `${entry.accountGeneration}, ${sqlLiteral(entry.planHash)}, ${sqlLiteral(plan.manifest_sha256)}, 'stage', 'planned', NULL, ${entry.createdAt}, ${now} ` +
      "WHERE EXISTS (SELECT 1 FROM cf_account_cutover WHERE uid = " +
      `${sqlLiteral(entry.uid)} AND state = 'new' AND checkpoint_phase = 'completed' ` +
      `AND destination_backend_bound = 1 AND account_generation = ${entry.accountGeneration}) ` +
      "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = " +
      `${sqlLiteral(entry.uid)}) ` +
      "AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = " +
      `${sqlLiteral(entry.uid)} AND expires_at > ${now}) ` +
      "ON CONFLICT(uid, import_id) DO UPDATE SET " +
      "status = CASE WHEN cf_phone_number_import_ledger.plan_hash = excluded.plan_hash " +
      "THEN cf_phone_number_import_ledger.status ELSE 'blocked' END, " +
      "action = CASE WHEN cf_phone_number_import_ledger.plan_hash = excluded.plan_hash " +
      "THEN cf_phone_number_import_ledger.action ELSE 'blocked' END, " +
      "last_error = CASE WHEN cf_phone_number_import_ledger.plan_hash = excluded.plan_hash " +
      "THEN excluded.last_error ELSE 'conflicting_duplicate_plan' END, " +
      "manifest_sha256 = CASE WHEN cf_phone_number_import_ledger.plan_hash = excluded.plan_hash " +
      "THEN excluded.manifest_sha256 ELSE cf_phone_number_import_ledger.manifest_sha256 END, " +
      "updated_at = excluded.updated_at;"
    );
  });
  return [
    "-- Generated by deploy/cloudflare/scripts/phone-history-reconcile.mjs; review before applying.",
    `-- source manifest SHA-256: ${plan.manifest_sha256}`,
    "-- This SQL only records a completed, already-attested phone snapshot in the import ledger.",
    "-- It never calls Twilio or inserts cf_phone_numbers; a separate reviewed Jobs executor supplies the transaction.",
    "-- No BEGIN/COMMIT is emitted.",
    ...statements,
    "",
  ].join("\n");
}

export function renderPhoneHistoryVerifySql(plan) {
  assertPlan(plan);
  const entries = plan.entries.filter((entry) => entry.action === "stage");
  if (!entries.length) return "-- No staged phone rows; nothing to verify.\n";
  const predicates = entries.map((entry) => `(uid = ${sqlLiteral(entry.uid)} AND import_id = ${sqlLiteral(entry.importId)})`);
  return [
    "-- Compare returned rows with the dry-run plan and then run --verify with a JSON export.",
    `-- source manifest SHA-256: ${plan.manifest_sha256}`,
    "SELECT uid, import_id, source_record_id, phone_number_id, phone_number_hash, proof_sha256, source_fingerprint, source_export_sha256, twilio_sid, verified_at, is_primary, account_generation, plan_hash, action, status, last_error, created_at, updated_at",
    `FROM cf_phone_number_import_ledger WHERE ${predicates.join(" OR ")} ORDER BY uid, source_record_id;`,
    "",
  ].join("\n");
}

function actualRowsValue(input) {
  if (Array.isArray(input)) return input;
  if (objectValue(input) && Array.isArray(input.rows)) return input.rows;
  fail("actual input must be an array or an object with rows");
}

export function verifyPhoneHistory(plan, actualInput) {
  assertPlan(plan);
  const actualRows = actualRowsValue(actualInput);
  const actualByKey = new Map();
  const duplicateActual = [];
  for (const row of actualRows) {
    if (!row || typeof row !== "object") continue;
    const key = `${row.uid}\0${row.import_id ?? row.importId}`;
    if (actualByKey.has(key)) duplicateActual.push(key);
    actualByKey.set(key, row);
  }
  const missing = [];
  const mismatched = [];
  const fencedPresent = [];
  for (const entry of plan.entries) {
    const key = `${entry.uid}\0${entry.importId}`;
    const actual = actualByKey.get(key);
    if (entry.action === "blocked") {
      if (entry.lastError?.includes("account_deletion_fence") && actual) fencedPresent.push(key);
      continue;
    }
    if (!actual) {
      missing.push(key);
      continue;
    }
    const reasons = [];
    const sensitive = sensitiveField(actual);
    if (sensitive) reasons.push(`sensitive_field:${sensitive}`);
    if (!(["planned", "applied"].includes(actual.status))) reasons.push("status");
    if (actual.action !== "stage") reasons.push("action");
    if (actual.phone_number_hash !== entry.phoneNumberHash) reasons.push("phone_number_hash");
    if (actual.phone_number_ciphertext !== entry.phoneNumberCiphertext) reasons.push("ciphertext");
    if (actual.proof_sha256 !== entry.proofSha256) reasons.push("proof_sha256");
    if (actual.source_fingerprint !== entry.sourceFingerprint) reasons.push("source_fingerprint");
    if (actual.source_export_sha256 !== entry.sourceExportSha256) reasons.push("source_export_sha256");
    if (actual.plan_hash !== entry.planHash) reasons.push("plan_hash");
    if (Number(actual.account_generation) !== entry.accountGeneration) reasons.push("account_generation");
    if (Number(actual.verified_at) !== entry.verifiedAt) reasons.push("verified_at");
    if (reasons.length) mismatched.push({ key, reasons });
  }
  const ok = !missing.length && !mismatched.length && !fencedPresent.length && !duplicateActual.length;
  return {
    status: ok ? "passed" : "failed",
    manifest_sha256: plan.manifest_sha256,
    checked: plan.entries.filter((entry) => entry.action === "stage").length,
    blocked: plan.entries.filter((entry) => entry.action === "blocked").length,
    missing,
    mismatched,
    fenced_present: fencedPresent,
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
  const actualIndex = args.indexOf("--actual");
  const input = await readJson(inputIndex >= 0 ? args[inputIndex + 1] : null);
  const fencedUids = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--fenced-uid" && args[index + 1]) fencedUids.push(args[++index]);
  }
  const plan = objectValue(input) && input.mode === "dry-run" && Array.isArray(input.entries)
    ? input
    : planPhoneHistory(input, { fencedUids });
  if (args.includes("--verify")) {
    const actual = await readJson(actualIndex >= 0 ? args[actualIndex + 1] : null);
    const result = verifyPhoneHistory(plan, actual);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    if (result.status !== "passed") process.exitCode = 2;
    return;
  }
  process.stdout.write(`${JSON.stringify({ ...plan, sql: renderPhoneHistoryLedgerSql(plan), verify_sql: renderPhoneHistoryVerifySql(plan) }, null, 2)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 2;
  });
}
