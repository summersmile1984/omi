import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { digest, WORKERS } from "../scripts/resource-input.mjs";
import { WranglerReleaseAdapter } from "../scripts/release-wrangler.mjs";
import {
  qualificationContext,
  qualificationProof,
  readQualificationInput,
} from "./qualification-context.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
export const SCHEMA_QUERY =
  "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' AND name NOT IN ('_cf_KV','d1_migrations') ORDER BY type,name";

async function query(adapter, authority, sql) {
  const response = await adapter.api(
    `d1/database/${authority.database_id}/query`,
    { method: "POST", body: { sql } }
  );
  const result = response.result;
  if (
    !Array.isArray(result) ||
    result.length !== 1 ||
    result[0].success !== true ||
    !Array.isArray(result[0].results)
  )
    throw new Error("schema query did not return a complete D1 result");
  return result[0].results;
}

export function executeFrozenSchema(context, { spawn = spawnSync } = {}) {
  const result = spawn(
    resolve(context.root, "backend/.venv/bin/python"),
    [
      resolve(
        context.root,
        "deploy/cloudflare/scripts/verify-resource-migrations.py"
      ),
      "--root",
      resolve(context.root, "deploy/cloudflare"),
      "--sql-root",
      resolve(context.directory, "sql"),
      "--include-schema",
    ],
    {
      cwd: context.root,
      encoding: "utf8",
      timeout: 60000,
      maxBuffer: 8 * 1024 * 1024,
      // Keep the SQL child in the qualifier's release-owned process group.
      // Its own timeout handles a standalone invocation; this script has no children.
      detached: false,
      killSignal: "SIGKILL",
      input: JSON.stringify(context.candidate.resource_plan),
      env: { PATH: process.env.PATH, LANG: "en_US.UTF-8" },
    }
  );
  if (result.status !== 0)
    throw new Error("frozen SQL fixture execution failed");
  let proof;
  try {
    proof = JSON.parse(result.stdout);
  } catch {
    throw new Error("frozen SQL fixture returned invalid evidence");
  }
  const fixtures = proof.sql_fixture;
  if (
    !Array.isArray(fixtures) ||
    fixtures.length !== 2 ||
    fixtures
      .map((item) => item.authority)
      .sort()
      .join(",") !== "app,auth"
  )
    throw new Error("frozen SQL requires both schema authorities");
  for (const item of fixtures) {
    const authority = context.candidate.resource_plan.migrations.find(
      (row) => row.authority === item.authority
    );
    if (
      !authority ||
      item.sql_files !== authority.files.length ||
      !item.legacy_row_preserved ||
      item.reentry_applied !== 0 ||
      !Array.isArray(item.schema_catalog) ||
      !item.schema_catalog.length
    )
      throw new Error("frozen SQL fixture evidence is incomplete");
  }
  return fixtures;
}

// First deployment has no retained Worker to execute. This is established by
// fresh Cloudflare observations and empty data authorities, never inferred from
// a missing local receipt. Updates/restore require a real retained-version
// harness and are refused here rather than silently certified by a SQL fixture.
export async function qualifyFirstRelease(
  context,
  adapter,
  { schema = executeFrozenSchema } = {}
) {
  const { candidate, observations } = context;
  context.verify();
  const phase = observations.release_phase;
  if (!["candidate", "deployed"].includes(phase))
    throw new Error(
      "retained-version rollback qualification is not implemented"
    );
  if (
    Object.keys(candidate.workers).sort().join(",") !==
    [...WORKERS].sort().join(",")
  )
    throw new Error("schema qualification requires the complete Worker set");
  const prior =
    phase === "deployed" ? observations.prior_versions : observations;
  for (const { name, sha256 } of Object.values(candidate.workers)) {
    if (!prior?.[name] || digest(prior[name]) !== digest({ status: "absent" }))
      throw new Error(
        "retained Worker requires executable prior-version compatibility evidence"
      );
    const current = await adapter.observeWorker(name);
    if (
      digest(current) !== digest(observations[name]) ||
      current.status !== (phase === "candidate" ? "absent" : "present")
    )
      throw new Error("Worker changed during schema qualification");
    if (
      phase === "deployed" &&
      current.message !==
        `candidate=${candidate.candidate_digest};artifact=${sha256}`
    )
      throw new Error(
        "deployed Worker does not carry this candidate's artifact identity"
      );
  }
  const cases = ["first-release.observed-prior-worker-absence"];
  const fixtures = schema(context);
  for (const authority of candidate.resource_plan.migrations) {
    const resource = candidate.resource_plan.resources.find(
      (row) => row.key === `d1:${authority.authority}`
    );
    const observed = resource && (await adapter.observeResource(resource));
    if (
      observed?.status !== "present" ||
      observed.id !== authority.database_id ||
      authority.database_id !== candidate.inventory.d1_ids[authority.authority]
    )
      throw new Error("schema database does not match the candidate authority");
    const rows = await query(adapter, authority, SCHEMA_QUERY);
    const ledger = await adapter.migrationLedger(authority);
    const expected = fixtures.find(
      (row) => row.authority === authority.authority
    );
    if (phase === "candidate") {
      if (rows.length || ledger.length)
        throw new Error(
          "first deployment requires observed empty business schemas and migration ledgers"
        );
      cases.push(`first-release.empty-authority.${authority.authority}`);
    } else {
      if (
        !expected ||
        digest(rows) !== digest(expected.schema_catalog) ||
        digest(ledger) !== digest(authority.files.map((file) => file.name))
      )
        throw new Error("deployed schema differs from the executed frozen SQL");
      if ((await query(adapter, authority, "PRAGMA foreign_key_check")).length)
        throw new Error("deployed D1 has invalid foreign key references");
      cases.push(`first-release.deployed-frozen-schema.${authority.authority}`);
    }
    cases.push(
      `first-release.frozen-sql-and-legacy-fixture.${authority.authority}`
    );
  }
  context.verify();
  return {
    ...qualificationProof(candidate, observations, cases),
    scope: "first-release-only",
  };
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  try {
    const context = qualificationContext(root, await readQualificationInput());
    const adapter = new WranglerReleaseAdapter(context);
    console.log(JSON.stringify(await qualifyFirstRelease(context, adapter)));
  } catch (error) {
    console.error(`Schema qualification failed: ${error.message}`);
    process.exitCode = 1;
  }
}
