import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { digest } from "./resource-input.mjs";
import { runReleaseProcess } from "./release-wrangler.mjs";

export const QUALIFIER_PROCESS_TIMEOUT_MS = 15 * 60 * 1000;

// Each future owner must implement its runner in the same candidate source.
// There is intentionally no operator-supplied command or approval-file option.
export const RELEASE_QUALIFIERS = Object.freeze([
  { id: "CF-4", path: "deploy/cloudflare/contracts/qualify-product.mjs" },
  { id: "CI-1", path: "contracts/deployment/qualify-dual-target.mjs" },
  {
    id: "prior-schema",
    path: "deploy/cloudflare/contracts/qualify-prior-schema.mjs",
  },
]);
export function pendingQualifiers(root) {
  return RELEASE_QUALIFIERS.filter(
    (entry) => !existsSync(resolve(root, entry.path))
  ).map((entry) => entry.id);
}
export function runReleaseQualifiers(
  root,
  candidate,
  observations,
  { directory, spawn = spawnSync } = {}
) {
  const pending = pendingQualifiers(root);
  if (pending.length)
    throw new Error(`release qualification pending: ${pending.join(", ")}`);
  if (typeof directory !== "string" || !isAbsolute(directory))
    throw new Error("qualification requires the frozen candidate directory");
  const observationDigest = digest(observations);
  return RELEASE_QUALIFIERS.map((entry) => {
    const result = runReleaseProcess(
      process.execPath,
      [resolve(root, entry.path)],
      {
        timeout: QUALIFIER_PROCESS_TIMEOUT_MS,
        cwd: root,
        encoding: "utf8",
        maxBuffer: 16 * 1024 * 1024,
        input: JSON.stringify({
          candidate_directory: directory,
          candidate,
          observations,
        }),
        env: { ...process.env, CI: "true" },
      },
      { spawn }
    );
    let proof;
    try {
      proof = JSON.parse(result.stdout);
    } catch {
      throw new Error(`${entry.id} did not produce a qualification contract`);
    }
    if (
      result.status !== 0 ||
      proof.schema_version !== 1 ||
      proof.candidate_digest !== candidate.candidate_digest ||
      proof.observation_digest !== observationDigest ||
      !Array.isArray(proof.cases) ||
      !proof.cases.length ||
      proof.cases.some(
        (item) =>
          typeof item.id !== "string" ||
          !/^[a-zA-Z0-9_.:-]{1,128}$/.test(item.id) ||
          item.result !== "pass"
      )
    )
      throw new Error(
        `${entry.id} failed or produced stale/incomplete evidence`
      );
    return {
      id: entry.id,
      command: ["node", entry.path],
      runner_sha256: digest(readFileSync(resolve(root, entry.path))),
      exit: 0,
      candidate_digest: candidate.candidate_digest,
      observation_digest: observationDigest,
      proof_sha256: digest(proof),
      cases: proof.cases.map(({ id, result }) => ({ id, result })),
    };
  });
}
