import { isAbsolute } from "node:path";
import { verifyCandidate } from "../scripts/release-files.mjs";
import { digest } from "../scripts/resource-input.mjs";

export async function readQualificationInput(stream = process.stdin) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of stream) {
    bytes += Buffer.byteLength(chunk);
    if (bytes > 16 * 1024 * 1024)
      throw new Error("qualification input exceeds its bound");
    chunks.push(Buffer.from(chunk));
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

// The release owner supplies this directory, never an operator-authored proof.
// Each runner reopens it and checks both artifacts and the current source.
export function qualificationContext(root, input) {
  const { candidate_directory: directory, candidate, observations } = input;
  if (typeof directory !== "string" || !isAbsolute(directory))
    throw new Error("qualification requires an absolute candidate directory");
  const verify = () => {
    const frozen = verifyCandidate(directory, root);
    if (digest(frozen) !== digest(candidate))
      throw new Error("qualification input differs from the frozen candidate");
  };
  verify();
  if (
    !observations ||
    !["candidate", "deployed", "restore"].includes(observations.release_phase)
  )
    throw new Error("qualification requires an explicit release phase");
  return { root, directory, candidate, observations, verify };
}

export function qualificationProof(candidate, observations, cases) {
  return {
    schema_version: 1,
    candidate_digest: candidate.candidate_digest,
    observation_digest: digest(observations),
    cases: cases.map((id) => ({ id, result: "pass" })),
  };
}
