import { isAbsolute, resolve } from "node:path";
import { lstatSync } from "node:fs";
import { digest } from "./resource-input.mjs";
import { git, readJson, verifyCandidateArtifacts } from "./release-files.mjs";
import { assertJournal } from "./release-transaction.mjs";

const infrastructure = [
  "resources", "migrations", "migration_lineage", "origins", "dependencies",
  "platform_bindings", "secrets", "deploy_order", "rollback_order",
];
const absent = () => ({ status: "absent" });
const version = (value) => Object.fromEntries(
  ["status", "version", "tag", "message"].filter((key) => key in value)
    .map((key) => [key, value[key]]),
);

export function uploadedArtifactDigest(candidate, role) {
  const files = Object.entries(candidate.artifact_files.workers ?? {})
    .filter(([name]) => name.startsWith(`${role}/`));
  if (!files.some(([name]) => name === `${role}/wrangler.json`))
    throw new Error("continuation requires the retained Worker file manifest");
  // The existing frozen publisher excludes its generated README (which contains
  // a build timestamp) and root source map from upload. Config/assets/vendor and
  // all executable module bytes remain exact; diagnostic timestamps are not code.
  return digest(Object.fromEntries(files.filter(([name]) =>
    name !== `${role}/modules/README.md` &&
    !(name.startsWith(`${role}/modules/`) && name.endsWith(".map") &&
      !name.slice(`${role}/modules/`.length).includes("/")),
  )));
}

// This is retained release history, never a substitute for the new candidate's
// exact-source CI/product qualification. The failed run 34373519437 owns the
// motivating partial first release: full SQL, three Workers, failed Core upload.
export function readContinuation(reference, { root, candidate }) {
  const chain = [], seen = new Set();
  let next = reference;
  while (next) {
    const directory = next.journal_directory;
    if (!isAbsolute(directory ?? "") || seen.has(directory) || chain.length >= 20)
      throw new Error("invalid or cyclic first-release continuation history");
    seen.add(directory);
    for (const path of [directory, resolve(directory, "journal.json"), `${directory}-candidate`]) {
      const stat = lstatSync(path);
      if (stat.isSymbolicLink()) throw new Error("continuation history cannot use symlinks");
    }
    const prior = verifyCandidateArtifacts(`${directory}-candidate`);
    const journal = readJson(resolve(directory, "journal.json"));
    assertJournal(prior, journal);
    for (const event of journal.events.filter((entry) => entry.id.startsWith("deploy:") && entry.state === "confirmed")) {
      const role = event.id.slice("deploy:".length), worker = prior.workers[role];
      if (!worker || !/^[a-z][a-z-]*$/.test(role) || worker.config !== `workers/${role}/wrangler.json` ||
          readJson(resolve(`${directory}-candidate`, worker.config)).upload_source_maps === true)
        throw new Error("continuation requires the existing no-source-map upload contract");
    }
    if (
      prior.candidate_digest !== next.candidate_digest ||
      journal.journal_digest !== next.journal_digest ||
      journal.phase !== "apply" || journal.release_ready !== false ||
      !["qualifying", "deploying", "reconciliation_required", "recovery_required"].includes(journal.state)
    ) throw new Error("continuation requires unchanged incomplete release history");
    const qualifiedBefore = Object.fromEntries(
      Object.entries(journal.before).filter(([key]) => !key.startsWith("sql:")),
    );
    if (["CF-4", "CI-1", "prior-schema"].some((id) =>
      !journal.qualification.some((proof) => proof.id === id && proof.exit === 0 &&
        proof.candidate_digest === prior.candidate_digest &&
        proof.observation_digest === digest(qualifiedBefore))))
      throw new Error("retained release did not complete its original admission");
    if (["brand", "stage", "account_id"].some((key) => prior[key] !== candidate[key]) ||
        digest(prior.inventory) !== digest(candidate.inventory) ||
        infrastructure.some((key) => digest(prior.resource_plan[key]) !== digest(candidate.resource_plan[key])) ||
        digest(Object.keys(prior.workers).sort()) !== digest(Object.keys(candidate.workers).sort()))
      throw new Error("continuation cannot change infrastructure, SQL or Worker membership");
    if (prior.source.working_diff_sha256 !== digest("") ||
        git(root, ["rev-parse", `${prior.source.commit}^{tree}`]) !== prior.source.tree)
      throw new Error("continuation source must be a retained clean Git commit");
    git(root, ["merge-base", "--is-ancestor", prior.source.commit, candidate.source.commit]);
    chain.unshift({ directory, candidate: prior, journal });
    next = journal.before.continuation;
  }
  if (!chain.length) throw new Error("continuation history is empty");
  if (chain[0].candidate.resource_plan.migrations.some(({ authority }) =>
    digest(chain[0].journal.before[`sql:${authority}`]) !== digest([])))
    throw new Error("continuation must originate in the observed empty migration authorities");
  return chain;
}

export function assertContinuationBasis(candidate, chain, observations) {
  const states = Object.fromEntries(Object.values(candidate.workers).map(({ name }) => [name, absent()]));
  const owners = {};
  for (const [index, { candidate: prior, journal }] of chain.entries()) {
    for (const { name } of Object.values(prior.workers))
      if (digest(version(journal.before[name] ?? {})) !== digest(states[name]))
        throw new Error("continuation does not start at the observed first-release history");
    const deployed = new Set();
    for (const event of journal.events) {
      if (event.id.startsWith("migrate:")) continue;
      if (!event.id.startsWith("deploy:")) throw new Error("continuation cannot adopt recovery mutations");
      const role = event.id.slice("deploy:".length), worker = prior.workers[role];
      if (!worker || deployed.has(role)) throw new Error("continuation has ambiguous Worker mutations");
      deployed.add(role);
      const expectedTag = `${journal.transaction}:${role}`;
      const expectedMessage = `candidate=${prior.candidate_digest};artifact=${worker.sha256}`;
      if (event.state === "confirmed") {
        const observed = event.observation;
        if (observed?.status !== "present" || observed.name !== worker.name ||
            observed.owned_by_transaction !== true || observed.tag !== expectedTag ||
            observed.message !== expectedMessage || !observed.version)
          throw new Error("continuation Worker ownership is incomplete");
        states[worker.name] = version(observed);
        owners[worker.name] = uploadedArtifactDigest(prior, role);
      } else if (["unknown", "in_flight"].includes(event.state)) {
        // Only an unchanged active version (including observed absence) proves
        // that an ambiguous upload did not replace serving code. An owned but
        // unconfirmed version needs separate trigger/domain reconciliation.
        const after = chain[index + 1]?.journal.before ?? observations;
        if (digest(after[worker.name]) !== digest(states[worker.name]))
          throw new Error("unconfirmed upload changed the active Worker");
      } else throw new Error("unknown continuation mutation state");
    }
  }
  for (const [role, { name }] of Object.entries(candidate.workers)) {
    if (digest(observations[name]) !== digest(states[name]))
      throw new Error("Worker changed outside the retained release owner");
    if (states[name].status === "present" && owners[name] !== uploadedArtifactDigest(candidate, role))
      throw new Error("continuation must preserve every already published artifact");
  }
}

export function continuationContext(root, candidate, journalDirectory) {
  const journal = readJson(resolve(journalDirectory, "journal.json"));
  const reference = {
    journal_directory: resolve(journalDirectory),
    journal_digest: journal.journal_digest,
    candidate_digest: journal.candidate_digest,
  };
  const chain = readContinuation(reference, { root, candidate });
  return {
    reference,
    lockDirectories: chain.map((entry) => entry.directory).sort(),
    verify(observations) {
      assertContinuationBasis(candidate, readContinuation(reference, { root, candidate }), observations);
    },
  };
}
