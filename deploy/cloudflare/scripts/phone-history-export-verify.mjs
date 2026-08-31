#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { planPhoneHistory } from "./phone-history-reconcile.mjs";

const MAX_EXPORT_BYTES = 8 * 1024 * 1024;
const MAX_ROWS = 5_000;
const MAX_APPLY_ENTRIES = 100;
const SHA256 = /^[0-9a-f]{64}$/;

function fail(message) {
  throw new Error(`phone history export verification: ${message}`);
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function parseBytes(bytes) {
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
  if (!object || object.schema_version !== 1 || !Array.isArray(object.rows))
    fail("export must contain schema_version=1 and rows");
  if (
    !source ||
    source.kind !== "firestore" ||
    source.collection !== "users/{uid}/phone_numbers" ||
    source.ciphertext_scheme !== "cloudflare-phone-aes-gcm-v1" ||
    source.proof_scheme !== "sha256-v1" ||
    typeof source.export_sha256 !== "string" ||
    !SHA256.test(source.export_sha256)
  )
    fail("export source must contain the attested phone export checksum and schemes");
  return { object, source };
}

/**
 * Verify a bounded, already re-encrypted phone export and turn it into the
 * existing planner input. The checksum here covers the exact UTF-8 manifest
 * bytes supplied to this tool. `source.export_sha256` remains the independent
 * checksum of the original Firestore export because the phone row fingerprint
 * is intentionally bound to that source checksum; conflating the two would
 * create a self-referential checksum when the source metadata is embedded in
 * this JSON file.
 */
export function verifyPhoneHistoryExport(
  bytes,
  { expectedSha256 = null, maxRows = MAX_ROWS, fencedUids = [] } = {},
) {
  const { object, source } = parseBytes(bytes);
  const computedSha256 = sha256(bytes);
  if (
    expectedSha256 !== null &&
    (!SHA256.test(expectedSha256) || expectedSha256 !== computedSha256)
  )
    fail("export checksum does not match --expected-sha256");
  const plan = planPhoneHistory(object, { maxRows, fencedUids });
  return {
    // `verified` means the operator supplied an independent checksum for this
    // exact manifest file. The source checksum is reported separately below.
    verified: expectedSha256 !== null,
    export_sha256: computedSha256,
    source_export_sha256: source.export_sha256,
    export_bytes: bytes.byteLength,
    plan,
  };
}

async function responseJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/**
 * Execute an all-stage plan through the gated Jobs review/apply API. The
 * endpoint receives only opaque plan identifiers, never the encrypted row
 * payload or operator key. The caller must explicitly provide both the
 * checksum verification and the admin key.
 */
export async function applyReviewedPhoneHistoryPlan(
  plan,
  { endpoint, adminKey, fetchImpl = globalThis.fetch },
) {
  if (
    !plan ||
    plan.mode !== "dry-run" ||
    plan.blocked !== 0 ||
    plan.stage !== plan.entries?.length ||
    plan.entries.length < 1 ||
    plan.entries.length > MAX_APPLY_ENTRIES
  )
    fail(`only an all-stage plan with 1-${MAX_APPLY_ENTRIES} entries may be applied`);
  if (!endpoint || typeof endpoint !== "string")
    fail("apply endpoint is required");
  if (!adminKey || typeof adminKey !== "string") fail("admin key is required");
  if (typeof fetchImpl !== "function")
    fail("fetch implementation is unavailable");

  const reviewEndpoint = endpoint.replace(/\/$/, "");
  const headers = {
    "content-type": "application/json",
    "secret-key": adminKey,
  };
  const reviewed = await fetchImpl(reviewEndpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({
      manifest_sha256: plan.manifest_sha256,
      entries: plan.entries.map((entry) => ({
        uid: entry.uid,
        import_id: entry.importId,
        plan_hash: entry.planHash,
      })),
    }),
  });
  const reviewedBody = await responseJson(reviewed);
  if (reviewed.status !== 201 || !reviewedBody?.review_id)
    fail(`review endpoint returned HTTP ${reviewed.status}`);

  const applied = await fetchImpl(
    `${reviewEndpoint}/${encodeURIComponent(String(reviewedBody.review_id))}/apply`,
    { method: "POST", headers: { "secret-key": adminKey } },
  );
  const appliedBody = await responseJson(applied);
  if (applied.status !== 200 || appliedBody?.status !== "applied")
    fail(`apply endpoint returned HTTP ${applied.status}`);
  return {
    review_id: String(reviewedBody.review_id),
    status: "applied",
    manifest_sha256: appliedBody.manifest_sha256 ?? plan.manifest_sha256,
    entry_count: appliedBody.entry_count,
    applied_count: appliedBody.applied_count,
    already_applied_count: appliedBody.already_applied_count,
  };
}

function argument(args, name) {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : null;
}

async function main() {
  const args = process.argv.slice(2);
  const filename = argument(args, "--export");
  if (!filename || filename.startsWith("--")) fail("--export is required");
  const bytes = await readFile(filename);
  const expectedSha256 = argument(args, "--expected-sha256");
  const fencedUids = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === "--fenced-uid" && args[index + 1])
      fencedUids.push(args[++index]);
  }
  const result = verifyPhoneHistoryExport(bytes, {
    expectedSha256,
    fencedUids,
  });
  if (args.includes("--apply")) {
    if (!result.verified) fail("--apply requires --expected-sha256");
    const endpoint = argument(args, "--apply");
    const adminKeyEnv = argument(args, "--admin-key-env") || "ADMIN_KEY";
    if (!endpoint || endpoint.startsWith("--"))
      fail("--apply requires the review endpoint URL");
    const adminKey = process.env[adminKeyEnv];
    result.apply = await applyReviewedPhoneHistoryPlan(result.plan, {
      endpoint,
      adminKey,
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
