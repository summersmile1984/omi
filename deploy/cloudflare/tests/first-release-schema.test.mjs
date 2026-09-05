import {
  cpSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { resolve } from "node:path";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  executeFrozenSchema,
  qualifyFirstRelease,
  SCHEMA_QUERY,
} from "../contracts/qualify-prior-schema.mjs";
import { WranglerReleaseAdapter } from "../scripts/release-wrangler.mjs";
import { digest, WORKERS } from "../scripts/resource-input.mjs";

const root = resolve(import.meta.dirname, "../../..");
const directories = [];
afterEach(() =>
  directories
    .splice(0)
    .forEach((path) => rmSync(path, { recursive: true, force: true }))
);
function fixture(phase = "candidate") {
  const directory = mkdtempSync(resolve(tmpdir(), "cf-first-schema-"));
  directories.push(directory);
  cpSync(
    resolve(root, "deploy/cloudflare/migrations"),
    resolve(directory, "sql"),
    { recursive: true }
  );
  const migrations = ["auth", "app"].map((authority, i) => ({
    authority,
    directory: `migrations/${authority}`,
    database_name: `schema-${authority}`,
    database_id: `${i + 1}1111111-1111-4111-8111-111111111111`,
    files: readdirSync(resolve(directory, "sql", authority))
      .filter((name) => name.endsWith(".sql"))
      .sort()
      .map((name) => ({
        name,
        sha256: digest(
          readFileSync(resolve(directory, "sql", authority, name))
        ),
      })),
  }));
  const resources = migrations.map((row) => ({
    kind: "d1",
    key: `d1:${row.authority}`,
    name: row.database_name,
  }));
  const candidate = {
    account_id: "a".repeat(32),
    candidate_digest: "b".repeat(64),
    workers: Object.fromEntries(
      WORKERS.map((role) => [
        role,
        { name: `schema-${role}`, sha256: digest(role) },
      ])
    ),
    resource_plan: { resources, migrations },
    inventory: {
      d1_ids: Object.fromEntries(
        migrations.map((row) => [row.authority, row.database_id])
      ),
    },
  };
  const absence = Object.fromEntries(
    Object.values(candidate.workers).map(({ name }) => [
      name,
      { status: "absent" },
    ])
  );
  const current =
    phase === "candidate"
      ? structuredClone(absence)
      : Object.fromEntries(
          Object.entries(candidate.workers).map(([role, { name, sha256 }]) => [
            name,
            {
              status: "present",
              version: "33333333-3333-4333-8333-333333333333",
              tag: `fixture:${role}`,
              message: `candidate=${candidate.candidate_digest};artifact=${sha256}`,
            },
          ])
        );
  const observations = {
    ...structuredClone(current),
    release_phase: phase,
    ...(phase === "deployed" ? { prior_versions: absence } : {}),
  };
  const context = { root, directory, candidate, observations, verify: vi.fn() };
  // Execute the actual frozen SQL sequence and its legacy fixtures.
  const schemas = executeFrozenSchema(context);
  const fault = {};
  const fetchImpl = vi.fn(async (input, init) => {
    const url = new URL(input);
    if (fault.permission)
      return Response.json({ success: false }, { status: 403 });
    const ok = (result) => Response.json({ success: true, result });
    if (url.pathname.endsWith("/deployments")) {
      const name = url.pathname.split("/").at(-2),
        state = current[name];
      if (!state) throw new Error("unowned Worker request");
      return state.status === "absent"
        ? Response.json(
            { success: false, errors: [{ code: 10007 }] },
            { status: 404 }
          )
        : ok({
            deployments: [
              { versions: [{ version_id: state.version, percentage: 100 }] },
            ],
          });
    }
    if (url.pathname.includes("/versions/")) {
      const name = url.pathname.split("/").at(-3),
        state = current[name];
      return ok({
        annotations: {
          "workers/tag": state.tag,
          "workers/message": state.message,
        },
      });
    }
    if (url.pathname.endsWith("/d1/database"))
      return ok(
        migrations.map((row) => ({
          name: row.database_name,
          uuid: row.database_id,
        }))
      );
    if (url.pathname.endsWith("/query")) {
      const id = url.pathname.split("/").at(-2),
        authority = migrations.find((row) => row.database_id === id);
      if (!authority) throw new Error("unowned D1 request");
      const { sql } = JSON.parse(init.body);
      let rows;
      if (sql === SCHEMA_QUERY)
        rows =
          phase === "candidate"
            ? fault.nonempty
              ? [{ type: "table", name: "legacy_user" }]
              : []
            : structuredClone(
                schemas.find((row) => row.authority === authority.authority)
                  .schema_catalog
              );
      else if (sql.includes("type='table' AND name='d1_migrations'"))
        rows = phase === "candidate" ? [] : [{ name: "d1_migrations" }];
      else if (sql === "SELECT name FROM d1_migrations ORDER BY id")
        rows = authority.files.map(({ name }) => ({ name }));
      else if (sql === "PRAGMA foreign_key_check")
        rows = fault.foreignKey ? [{ table: "session" }] : [];
      else throw new Error("unexpected or mutating SQL");
      if (fault.schemaDrift && sql === SCHEMA_QUERY && rows.length)
        rows[0].sql += " -- unqualified drift";
      if (
        fault.ledgerDrift &&
        sql === "SELECT name FROM d1_migrations ORDER BY id"
      )
        rows.pop();
      return ok([{ success: true, results: rows }]);
    }
    throw new Error("unexpected or mutating Cloudflare operation");
  });
  const adapter = new WranglerReleaseAdapter({
    ...context,
    fetchImpl,
    env: { CLOUDFLARE_API_TOKEN: "synthetic-fixture" },
  });
  return { context, adapter, schemas, fault, fetchImpl, current };
}

describe("first-release schema qualification", () => {
  it("executes frozen SQL, preserves a legacy principal and only qualifies observed absence/empty databases", async () => {
    const f = fixture();
    const proof = await qualifyFirstRelease(f.context, f.adapter);
    expect(proof.scope).toBe("first-release-only");
    expect(proof.cases).toHaveLength(5);
    expect(proof.cases.every((row) => row.result === "pass")).toBe(true);
    expect(
      f.schemas.every(
        (row) => row.legacy_row_preserved && row.reentry_applied === 0
      )
    ).toBe(true);
    expect(f.context.verify).toHaveBeenCalledTimes(2);
  });
  it("checks real SQL catalogs, complete ledgers and foreign keys after the exact candidate is deployed", async () => {
    const f = fixture("deployed");
    const proof = await qualifyFirstRelease(f.context, f.adapter);
    expect(proof.observation_digest).toBe(digest(f.context.observations));
    expect(proof.cases.map((row) => row.id)).toContain(
      "first-release.deployed-frozen-schema.app"
    );
  });
  it.each(["schemaDrift", "ledgerDrift", "foreignKey"])(
    "rejects deployed %s",
    async (fault) => {
      const f = fixture("deployed");
      f.fault[fault] = true;
      await expect(qualifyFirstRelease(f.context, f.adapter)).rejects.toThrow(
        /schema differs|foreign key/
      );
    }
  );
  it("does not adopt an existing database or treat denied observation as first-release absence", async () => {
    const f = fixture();
    f.fault.nonempty = true;
    await expect(qualifyFirstRelease(f.context, f.adapter)).rejects.toThrow(
      "observed empty"
    );
    f.fault.permission = true;
    await expect(qualifyFirstRelease(f.context, f.adapter)).rejects.toThrow(
      "HTTP 403"
    );
  });
  it("requires a retained-version harness for an existing Worker and rejects concurrent version drift", async () => {
    const f = fixture(),
      name = f.context.candidate.workers.auth.name;
    f.context.observations[name] = { status: "present", version: "prior" };
    await expect(qualifyFirstRelease(f.context, f.adapter)).rejects.toThrow(
      "prior-version compatibility"
    );
    expect(f.fetchImpl).toHaveBeenCalledTimes(WORKERS.indexOf("auth"));
    f.context.observations[name] = { status: "absent" };
    f.current[name] = {
      status: "present",
      version: "33333333-3333-4333-8333-333333333333",
      tag: null,
      message: null,
    };
    await expect(qualifyFirstRelease(f.context, f.adapter)).rejects.toThrow(
      "Worker changed"
    );
  });
  it("fails if frozen SQL bytes change, even when source SQL remains valid", () => {
    const f = fixture(),
      authority = f.context.candidate.resource_plan.migrations[0];
    writeFileSync(
      resolve(
        f.context.directory,
        "sql",
        authority.authority,
        authority.files[0].name
      ),
      "SELECT 1;"
    );
    expect(() => executeFrozenSchema(f.context)).toThrow(
      "fixture execution failed"
    );
  });
});
