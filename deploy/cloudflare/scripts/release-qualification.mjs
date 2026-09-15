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

const OUTPUT_SNIPPET_LIMIT = 400;

function snippet(value) {
  const text = String(value ?? "")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  return text.length > OUTPUT_SNIPPET_LIMIT
    ? `${text.slice(0, OUTPUT_SNIPPET_LIMIT)}…`
    : text;
}

// The 2026-09-11 and 2026-09-12 beta deliveries both recorded exactly
// "CF-4 did not produce a qualification contract". A qualifier that crashes writes its
// reason to stderr and exits non-zero, which is indistinguishable from one that printed
// nothing at all once that stderr is dropped -- and the wrapper dropped it, so the same
// failure had to be re-diagnosed from a bare journal summary twice. The child's own record
// is the only diagnosis available here. `releaseFailure` binds this message into the
// journal and the job log, redacts known release values from it, and caps its length.
function qualifierOutput(result, { stdout = true } = {}) {
  const parts = [];
  if (result.error) parts.push(`spawn ${result.error.code ?? result.error.message}`);
  else if (result.signal) parts.push(`killed by ${result.signal}`);
  else if (result.status !== 0) parts.push(`exit status ${result.status}`);
  const stderr = snippet(result.stderr);
  if (stderr) parts.push(`stderr: ${stderr}`);
  const output = stdout ? snippet(result.stdout) : "";
  if (output) parts.push(`stdout: ${output}`);
  return parts.length ? ` (${parts.join("; ")})` : "";
}

function proofProblems(proof, candidate, observationDigest) {
  if (proof === null || typeof proof !== "object")
    return ["the contract is not an object"];
  const problems = [];
  if (proof.schema_version !== 1)
    problems.push(`schema_version is ${JSON.stringify(proof.schema_version)}`);
  if (proof.candidate_digest !== candidate.candidate_digest)
    problems.push("candidate_digest does not match the candidate");
  if (proof.observation_digest !== observationDigest)
    problems.push("observation_digest is stale");
  if (!Array.isArray(proof.cases) || !proof.cases.length)
    problems.push("no cases were reported");
  else if (
    proof.cases.some(
      (item) =>
        typeof item.id !== "string" ||
        !/^[a-zA-Z0-9_.:-]{1,128}$/.test(item.id) ||
        item.result !== "pass"
    )
  )
    problems.push("a case is malformed or did not pass");
  return problems;
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
  // A delivery qualifies twice -- before the deploy and again against what was published --
  // and the journal records only the first proof. Naming the phase is what tells an operator
  // which of the two runs failed.
  const phase = observations?.release_phase ?? "unknown";
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
      throw new Error(
        `${entry.id} did not produce a qualification contract (phase=${phase})${qualifierOutput(result)}`
      );
    }
    const problems = proofProblems(proof, candidate, observationDigest);
    if (result.status !== 0) problems.unshift(`exit status ${result.status}`);
    if (problems.length)
      throw new Error(
        `${entry.id} failed or produced stale/incomplete evidence (phase=${phase}; ${problems.join(
          "; "
        )})${qualifierOutput(result, { stdout: false })}`
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
