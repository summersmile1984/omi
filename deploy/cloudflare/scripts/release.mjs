import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  openSync,
  closeSync,
  unlinkSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import {
  prepareRelease,
  dryRunConfig,
  verifyFrozenPayload,
} from "./release-build.mjs";
import { readJson, verifyCandidate, writeJson } from "./release-files.mjs";
import {
  pendingQualifiers,
  runReleaseQualifiers,
} from "./release-qualification.mjs";
import { WranglerReleaseAdapter } from "./release-wrangler.mjs";
import { regressCandidate } from "../../../contracts/deployment/regress.mjs";
import { qualificationContext } from "../contracts/qualification-context.mjs";
import { continuationContext } from "./release-continuation.mjs";
import {
  applyRelease,
  createJournal,
  provisionResources,
  recoveryPlan,
  restoreRelease,
} from "./release-transaction.mjs";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
try {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      stage: { type: "string" },
      brand: { type: "string" },
      manifest: { type: "string" },
      inventory: { type: "string" },
      output: { type: "string" },
      candidate: { type: "string" },
      journal: { type: "string" },
      authorize: { type: "string" },
      "continue-from": { type: "string" },
    },
  });
  if (positionals.length !== 1)
    throw new Error(
      "select prepare, check, dry-run, provision, apply, recovery-plan or restore"
    );
  const [action] = positionals;
  if (values["continue-from"] && action !== "apply")
    throw new Error("--continue-from is only valid for an apply transaction");
  if (action === "prepare") {
    if (!values.output || !values.inventory)
      throw new Error(
        "prepare requires --stage --inventory --output and --brand or --manifest"
      );
    const result = prepareRelease({ root, ...values });
    const context = qualificationContext(root, {
      candidate_directory: resolve(values.output),
      candidate: result,
      observations: { release_phase: "candidate" },
    });
    const regression = await regressCandidate(context);
    const regressionDirectory = mkdtempSync(
      resolve(tmpdir(), "eddy-release-regression-")
    );
    writeJson(regressionDirectory, "result.json", regression);
    context.verify();
    console.log(
      JSON.stringify({
        candidate_digest: result.candidate_digest,
        local_verified: true,
        product_regression_passed: true,
        product_cases: regression.cases.length,
        product_report: resolve(regressionDirectory, "result.json"),
        release_ready: false,
        pending: result.pending,
      })
    );
  } else {
    if (!values.candidate) throw new Error("--candidate directory is required");
    const directory = resolve(values.candidate),
      candidate = verifyCandidate(directory, root);
    if (values.stage && values.stage !== candidate.stage)
      throw new Error("command stage differs from candidate");
    if (action === "check")
      console.log(
        JSON.stringify({
          candidate_digest: candidate.candidate_digest,
          local_verified: true,
          release_ready: false,
          pending: candidate.pending,
          pending_runners: pendingQualifiers(root),
        })
      );
    else if (action === "dry-run") {
      if (!values.output || existsSync(resolve(values.output)))
        throw new Error("dry-run requires a fresh --output");
      mkdirSync(resolve(values.output), { recursive: true });
      for (const [role, worker] of Object.entries(candidate.workers)) {
        dryRunConfig(
          root,
          resolve(directory, worker.config),
          resolve(values.output, role),
          resolve(values.output, `${role}.log`)
        );
        verifyFrozenPayload(
          resolve(directory, worker.artifact),
          resolve(values.output, role)
        );
      }
      verifyCandidate(directory, root);
      console.log(
        JSON.stringify({
          workers: Object.keys(candidate.workers).length,
          dry_run: true,
          release_ready: false,
        })
      );
    } else {
      if (!["provision", "apply", "recovery-plan", "restore"].includes(action))
        throw new Error("unknown release operation");
      if (values.authorize !== candidate.candidate_digest)
        throw new Error(
          "remote operation requires explicit --authorize <exact-candidate-digest>"
        );
      if (!values.journal)
        throw new Error(
          "remote operations require a separate --journal directory"
        );
      if (
        ["apply", "restore"].includes(action) &&
        pendingQualifiers(root).length
      )
        throw new Error(
          `release qualification pending: ${pendingQualifiers(root).join(", ")}`
        );
      const journalDirectory = resolve(values.journal),
        path = resolve(journalDirectory, "journal.json");
      const adapter = new WranglerReleaseAdapter({
        root,
        directory,
        candidate,
      });
      const persist = (journal) =>
        writeJson(journalDirectory, "journal.json", journal);
      const qualify = async (observations) =>
        runReleaseQualifiers(root, candidate, observations, { directory });
      const verify = () => verifyCandidate(directory, root);
      const continuation = values["continue-from"]
        ? continuationContext(root, candidate, resolve(values["continue-from"]))
        : undefined;
      if (action === "recovery-plan") {
        console.log(
          JSON.stringify(
            await recoveryPlan(candidate, readJson(path), adapter),
            null,
            2
          )
        );
      } else {
        let journal;
        if (action === "restore") journal = readJson(path);
        else {
          if (existsSync(journalDirectory))
            throw new Error(
              "a new transaction needs a fresh journal directory"
            );
          mkdirSync(journalDirectory, { recursive: true });
          journal = createJournal(candidate, action);
          persist(journal);
        }
        // Exclusive process ownership; a crash leaves the lock for explicit
        // inspection. Never silently remove it or replay remote operations.
        const locks = [];
        try {
          for (const directory of [...(continuation?.lockDirectories ?? []), journalDirectory]) {
            const path = resolve(directory, "transaction.lock");
            locks.push({ path, fd: openSync(path, "wx", 0o600) });
          }
          if (action === "provision")
            await provisionResources({ candidate, journal, adapter, persist });
          else if (action === "apply")
            await applyRelease({
              candidate,
              journal,
              adapter,
              persist,
              qualify,
              verify,
              continuation,
            });
          else
            await restoreRelease({
              candidate,
              journal,
              adapter,
              persist,
              qualify,
              verify,
            });
        } finally {
          for (const { path, fd } of locks.reverse()) {
            closeSync(fd);
            unlinkSync(path);
          }
        }
        console.log(
          JSON.stringify({
            state: journal.state,
            release_ready: journal.release_ready,
            journal: path,
          })
        );
      }
    }
  }
} catch (error) {
  console.error(`FAIL: ${error.message}`);
  process.exitCode = 1;
}
