#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { planWrappedHistory } from "./wrapped-history-reconcile.mjs";

const MAX_EXPORT_BYTES = 8 * 1024 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;

function fail(message) {
  throw new Error(`wrapped history export verification: ${message}`);
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
    source.collection !== "users/{uid}/wrapped/{year}"
  )
    fail("export source must identify users/{uid}/wrapped/{year}");
  return { object, source };
}

/**
 * Verify the bytes of a bounded Firestore export and turn them into the
 * existing planner input. The checksum is deliberately computed over the
 * original bytes, before JSON parsing or key reordering. An operator supplied
 * expected checksum is required for an export to be considered attested.
 */
export function verifyWrappedHistoryExport(
  bytes,
  { expectedSha256 = null, maxRows = 5_000 } = {},
) {
  const { object, source } = parseBytes(bytes);
  const computedSha256 = sha256(bytes);
  if (
    expectedSha256 !== null &&
    (!SHA256.test(expectedSha256) || expectedSha256 !== computedSha256)
  )
    fail("export checksum does not match --expected-sha256");
  if (
    source.export_sha256 !== undefined &&
    source.export_sha256 !== computedSha256
  )
    fail("source.export_sha256 does not match the original export bytes");
  const manifest = {
    schema_version: 1,
    source: {
      kind: "firestore",
      collection: "users/{uid}/wrapped/{year}",
      export_sha256: computedSha256,
      ...(typeof source.exported_at === "string"
        ? { exported_at: source.exported_at }
        : {}),
    },
    rows: object.rows,
  };
  const plan = planWrappedHistory(manifest, { maxRows });
  return {
    verified: expectedSha256 !== null,
    export_sha256: computedSha256,
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
 * Execute the already verified plan through the gated Jobs review/apply API.
 * The caller must explicitly provide the endpoint and an admin key; no key is
 * accepted from the plan or serialized into the result.
 */
export async function applyReviewedWrappedHistoryPlan(
  plan,
  { endpoint, adminKey, fetchImpl = globalThis.fetch },
) {
  if (
    !plan ||
    plan.mode !== "dry-run" ||
    plan.blocked !== 0 ||
    plan.stage !== plan.entries?.length
  )
    fail("only an all-stage, unblocked planner result may be applied");
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
    body: JSON.stringify(plan),
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
    manifest_sha256: appliedBody.manifest_sha256,
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
  const result = verifyWrappedHistoryExport(bytes, { expectedSha256 });
  if (args.includes("--apply")) {
    if (!result.verified) fail("--apply requires --expected-sha256");
    const endpoint = argument(args, "--apply");
    const adminKeyEnv = argument(args, "--admin-key-env") || "ADMIN_KEY";
    if (!endpoint || endpoint.startsWith("--"))
      fail("--apply requires the review endpoint URL");
    const adminKey = process.env[adminKeyEnv];
    result.apply = await applyReviewedWrappedHistoryPlan(result.plan, {
      endpoint,
      adminKey,
    });
  }
  const output = JSON.stringify(result, null, 2) + "\n";
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
