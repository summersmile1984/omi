import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { digest } from "../scripts/resource-input.mjs";
import { fileTree, writeJson } from "../scripts/release-files.mjs";
import { applyRelease, createJournal } from "../scripts/release-transaction.mjs";
import { continuationContext } from "../scripts/release-continuation.mjs";

const directories = [];
afterEach(() => directories.splice(0).forEach((path) => rmSync(path, { recursive: true, force: true })));
const seal = (value, field) => {
  delete value[field];
  value[field] = digest(value);
  return value;
};

async function fixture() {
  const root = mkdtempSync(resolve(tmpdir(), "cf-continuation-"));
  directories.push(root);
  const git = (...args) => execFileSync("git", args, { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  git("init", "-q");
  writeFileSync(resolve(root, "source.txt"), "first\n");
  git("add", "source.txt");
  git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "first");
  const plan = seal({
    brand: "fixture", stage: "beta", account_id: "a".repeat(32), target: "cloudflare",
    resources: [], migrations: [{ authority: "auth", files: [{ name: "0001.sql", sha256: digest("sql") }] }],
    migration_lineage: [], origins: {}, dependencies: {}, platform_bindings: {}, secrets: {},
    deploy_order: ["fixture-auth", "fixture-core"], rollback_order: ["fixture-core", "fixture-auth"],
  }, "plan_digest");
  const candidate = (core) => seal({
    schema_version: 1, release_ready: false, brand: plan.brand, stage: plan.stage, account_id: plan.account_id,
    inventory: { d1_ids: {} }, resource_plan: structuredClone(plan), artifact_files: {},
    workers: {
      auth: { name: "fixture-auth", config: "workers/auth/wrangler.json", sha256: digest("auth") },
      core: { name: "fixture-core", config: "workers/core/wrangler.json", sha256: digest(core) },
    },
    source: { commit: git("rev-parse", "HEAD"), tree: git("rev-parse", "HEAD^{tree}"), working_diff_sha256: digest("") },
  }, "candidate_digest");
  const retain = (value, name) => {
    const directory = resolve(root, name), payload = `${directory}-candidate`;
    mkdirSync(directory);
    for (const [role, worker] of Object.entries(value.workers)) {
      const directory = resolve(payload, "workers", role);
      mkdirSync(resolve(directory, "modules"), { recursive: true });
      writeFileSync(resolve(directory, "modules/index.js"), role === "auth" ? "auth" : worker.sha256);
      writeFileSync(resolve(directory, "modules/README.md"), `Generated in ${name}`);
      writeFileSync(resolve(directory, "modules/index.js.map"), `Build debug path ${name}`);
      writeJson(directory, "wrangler.json", { name: worker.name, no_bundle: true });
      worker.sha256 = digest(fileTree(directory));
    }
    value.artifact_files = { workers: fileTree(resolve(payload, "workers")) };
    seal(value, "candidate_digest");
    writeJson(payload, "candidate.json", value);
    return directory;
  };
  const previous = candidate("broken-core"), previousDirectory = retain(previous, "previous");
  const states = { "fixture-auth": { status: "absent" }, "fixture-core": { status: "absent" } };
  let ledger = [], count = 0, fail = true;
  const adapter = {
    observeWorker: vi.fn(async (name) => structuredClone(states[name])),
    observeResource: vi.fn(), preconditions: vi.fn(async () => {}),
    migrationLedger: vi.fn(async () => [...ledger]),
    migrate: vi.fn(async () => { ledger = ["0001.sql"]; return { exit: 0 }; }),
    deploy: vi.fn(async (role, tag, message) => {
      if (role === "core" && fail) return { exit: 1 };
      states[`fixture-${role}`] = { status: "present", version: `version-${++count}`, tag, message };
      return { exit: 0 };
    }),
    readiness: vi.fn(async () => ({ ready: true })),
  };
  const qualify = (value) => vi.fn(async (observations) => ["CF-4", "CI-1", "prior-schema"].map((id) => ({
    id, exit: 0, candidate_digest: value.candidate_digest, observation_digest: digest(observations),
  })));
  const previousJournal = createJournal(previous, "apply");
  await expect(applyRelease({
    candidate: previous, journal: previousJournal, adapter, qualify: qualify(previous), verify: vi.fn(),
    persist: (value) => writeJson(previousDirectory, "journal.json", value),
  })).rejects.toThrow("reconciliation");
  writeFileSync(resolve(root, "source.txt"), "repaired\n");
  git("add", "source.txt");
  git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "repair");
  const next = candidate("repaired-core"), nextDirectory = retain(next, "next");
  const execute = async (value = next, directory = nextDirectory, predecessor = previousDirectory) => {
    const continuation = continuationContext(root, value, predecessor);
    const journal = createJournal(value, "apply"), qualification = qualify(value);
    const options = {
      candidate: value, journal, adapter, qualify: qualification, verify: vi.fn(), continuation,
      persist: (value) => writeJson(directory, "journal.json", value),
    };
    await applyRelease(options);
    return options;
  };
  return { root, previous, previousDirectory, previousJournal, next, nextDirectory, states, adapter,
    execute, retain, candidate, succeed: () => { fail = false; } };
}

describe("observed first-release continuation", () => {
  it("keeps migrated data and published bytes, then qualifies and publishes a fresh candidate", async () => {
    const f = await fixture();
    const oldVersion = f.states["fixture-auth"].version;
    expect(f.next.workers.auth.sha256).not.toBe(f.previous.workers.auth.sha256);
    f.succeed();
    const result = await f.execute();
    expect(f.adapter.migrate).toHaveBeenCalledTimes(1);
    expect(result.qualify).toHaveBeenCalledTimes(2);
    expect(result.qualify.mock.calls[0][0].continuation.candidate_digest).toBe(f.previous.candidate_digest);
    expect(f.states["fixture-auth"].version).not.toBe(oldVersion);
    expect(result.journal.release_ready).toBe(true);
    expect(f.previousJournal.release_ready).toBe(false);
  });
  it("can reconcile another failed attempt without losing the original absence and version history", async () => {
    const f = await fixture();
    await expect(f.execute()).rejects.toThrow("reconciliation");
    const next = f.candidate("another-core-repair"), directory = f.retain(next, "third");
    f.succeed();
    const result = await f.execute(next, directory, f.nextDirectory);
    expect(result.journal.release_ready).toBe(true);
    expect(f.adapter.migrate).toHaveBeenCalledTimes(1);
  });
  it.each(["external-version", "unconfirmed-upload", "published-bytes", "sql", "journal-integrity", "completed-release"])(
    "refuses %s before any further remote mutation", async (fault) => {
      const f = await fixture();
      const before = f.adapter.deploy.mock.calls.length;
      if (fault === "external-version") f.states["fixture-auth"].version = "external";
      if (fault === "unconfirmed-upload") f.states["fixture-core"] = { status: "present", version: "ambiguous" };
      if (fault === "published-bytes") f.next.artifact_files.workers["auth/modules/index.js"] = digest("different-auth");
      if (fault === "sql") f.next.resource_plan.migrations[0].files[0].sha256 = digest("different-sql");
      if (fault === "journal-integrity") {
        f.previousJournal.state = "deploying";
        writeJson(f.previousDirectory, "journal.json", f.previousJournal);
      }
      if (fault === "completed-release") {
        f.previousJournal.state = "completed";
        f.previousJournal.release_ready = true;
        seal(f.previousJournal, "journal_digest");
        writeJson(f.previousDirectory, "journal.json", f.previousJournal);
      }
      f.succeed();
      await expect(f.execute()).rejects.toThrow();
      expect(f.adapter.deploy).toHaveBeenCalledTimes(before);
      expect(f.adapter.migrate).toHaveBeenCalledTimes(1);
    },
  );
});
