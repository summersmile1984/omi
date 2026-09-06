import { spawnSync } from "node:child_process";
import { digest } from "../scripts/resource-input.mjs";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  activeVersion,
  WRANGLER_PROCESS_TIMEOUT_MS,
  WranglerReleaseAdapter,
} from "../scripts/release-wrangler.mjs";
import {
  pendingQualifiers,
  QUALIFIER_PROCESS_TIMEOUT_MS,
  RELEASE_QUALIFIERS,
  runReleaseQualifiers,
} from "../scripts/release-qualification.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const account = "a".repeat(32),
  version = "11111111-1111-4111-8111-111111111111";
const directories = [];
afterEach(() => {
  vi.restoreAllMocks();
  for (const path of directories.splice(0))
    rmSync(path, { force: true, recursive: true });
});
function fixture(options = {}) {
  const directory = mkdtempSync(resolve(tmpdir(), "cf-release-adapter-"));
  directories.push(directory);
  const candidate = {
    account_id: account,
    workers: {
      auth: { config: "workers/auth/wrangler.json", name: "fixture-auth" },
    },
    resource_plan: {
      secrets: { auth: { BETTER_AUTH_SECRET: "FIXTURE_SECRET" } },
      origins: {
        api: "https://api.fixture.invalid",
        web: "https://web.fixture.invalid",
      },
    },
  };
  return new WranglerReleaseAdapter({
    root,
    directory,
    candidate,
    env: {
      CLOUDFLARE_API_TOKEN: "synthetic-api-credential",
      FIXTURE_SECRET: "synthetic-secret-value-".repeat(3),
    },
    ...options,
  });
}
const ok = (result) => Response.json({ success: true, result });
describe("locked Wrangler release adapter", () => {
  it("waits for asynchronous metadata visibility without repeating creation", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(ok({ metadataIndexes: [] }))
      .mockResolvedValueOnce(ok({ metadataIndexes: [] }))
      .mockResolvedValueOnce(
        ok({
          metadataIndexes: [
            { propertyName: "created_at", indexType: "Number" },
          ],
        })
      );
    const spawn = vi.fn(),
      sleep = vi.fn(async () => {});
    const adapter = fixture({ fetchImpl, spawn });
    await expect(
      adapter.waitForPolicy(
        {
          kind: "vectorize",
          name: "fixture-index",
          id: "created_at",
          type: "number",
        },
        { attempts: 3, retryDelayMs: 1, sleep }
      )
    ).resolves.toEqual({ status: "present" });
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(sleep).toHaveBeenCalledTimes(2);
    expect(spawn).not.toHaveBeenCalled();
  });
  it("bounds missing-policy observations and stops immediately on a permission failure", async () => {
    const policy = {
      kind: "vectorize",
      name: "fixture-index",
      id: "created_at",
      type: "number",
    };
    const fetchImpl = vi.fn(() => ok({ metadataIndexes: [] }));
    const sleep = vi.fn(async () => {}),
      adapter = fixture({ fetchImpl });
    await expect(
      adapter.waitForPolicy(policy, { attempts: 2, retryDelayMs: 1, sleep })
    ).rejects.toThrow("deadline");
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    fetchImpl
      .mockClear()
      .mockImplementation(() =>
        Response.json({ success: false }, { status: 403 })
      );
    sleep.mockClear();
    await expect(
      adapter.waitForPolicy(policy, { attempts: 2, retryDelayMs: 1, sleep })
    ).rejects.toThrow("HTTP 403");
    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(sleep).not.toHaveBeenCalled();
  });
  it.each(["number", "Number"])(
    "observes the documented and production Vectorize numeric type %s",
    async (indexType) => {
      const adapter = fixture({
        fetchImpl: vi.fn(() =>
          ok({ metadataIndexes: [{ propertyName: "created_at", indexType }] })
        ),
      });
      await expect(
        adapter.observePolicy({
          kind: "vectorize",
          name: "fixture-index",
          id: "created_at",
          type: "number",
        })
      ).resolves.toEqual({ status: "present" });
    }
  );
  it.each(["String", "Boolean", "NUMBER", "", null, undefined])(
    "rejects a wrong or unknown Vectorize metadata type %s",
    async (indexType) => {
      const adapter = fixture({
        fetchImpl: vi.fn(() =>
          ok({ metadataIndexes: [{ propertyName: "created_at", indexType }] })
        ),
      });
      await expect(
        adapter.observePolicy({
          kind: "vectorize",
          name: "fixture-index",
          id: "created_at",
          type: "number",
        })
      ).rejects.toThrow("Vectorize metadata owner differs");
    }
  );
  it("reports an uncreated Vectorize metadata property as absent", async () => {
    const adapter = fixture({
      fetchImpl: vi.fn(() => ok({ metadataIndexes: [] })),
    });
    await expect(
      adapter.observePolicy({
        kind: "vectorize",
        name: "fixture-index",
        id: "created_at",
        type: "number",
      })
    ).resolves.toEqual({ status: "absent" });
  });
  it("publishes the frozen config, not source, with private secrets and exact transaction annotations", () => {
    let secretPath;
    const spawn = vi.fn((command, args, options) => {
      expect(command).toBe(process.execPath);
      expect(args[0]).toBe(
        resolve(root, "deploy/cloudflare/node_modules/wrangler/bin/wrangler.js")
      );
      expect(args.slice(1, 3)).toEqual(["deploy", "--config"]);
      expect(args).toContain("--no-bundle");
      expect(args).toContain("--strict");
      expect(
        args.slice(args.indexOf("--tag"), args.indexOf("--tag") + 2)
      ).toEqual(["--tag", "transaction:auth"]);
      secretPath = args.at(-1);
      expect(
        JSON.parse(readFileSync(secretPath, "utf8")).BETTER_AUTH_SECRET
      ).toContain("synthetic-secret-value");
      expect(options.stdio).toEqual(["ignore", "pipe", "pipe"]);
      expect(options.env.CLOUDFLARE_ACCOUNT_ID).toBe(account);
      return {
        status: 1,
        stdout: "sensitive response",
        stderr: "synthetic-api-credential",
      };
    });
    const adapter = fixture({ spawn });
    const result = adapter.deploy(
      "auth",
      "transaction:auth",
      "candidate=fixture;artifact=fixture"
    );
    expect(result).toEqual({ exit: 1, signal: null });
    expect(existsSync(secretPath)).toBe(false);
    expect(JSON.stringify(result)).not.toMatch(/secret|sensitive|credential/);
  });
  it("bounds a hanging real launcher and kills its descendant process group without exposing output", () => {
    const kill = vi.spyOn(process, "kill");
    let actual;
    const spawn = vi.fn((command, _args, options) => {
      expect(options.timeout).toBe(WRANGLER_PROCESS_TIMEOUT_MS);
      expect(options.killSignal).toBe("SIGKILL");
      expect(options.detached).toBe(true);
      actual = spawnSync(
        command,
        [
          "-e",
          `
        const { spawn } = require('node:child_process');
        const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
        console.log(child.pid);
        process.on('SIGTERM', () => {});
        setInterval(() => {}, 1000);
      `,
        ],
        { ...options, timeout: 250 }
      );
      return actual;
    });
    const adapter = fixture({ spawn });
    expect(adapter.command(["deploy"])).toEqual({
      exit: null,
      signal: "SIGKILL",
    });
    expect(actual.error.code).toBe("ETIMEDOUT");
    expect(Number(actual.stdout.trim())).toBeGreaterThan(0);
    expect(kill).toHaveBeenCalledWith(-actual.pid, "SIGKILL");
    expect(spawn).toHaveBeenCalledOnce();
  });

  it("rejects valid-looking evidence from a timed-out real qualification process", () => {
    const directory = mkdtempSync(
      resolve(tmpdir(), "cf-release-qualification-timeout-")
    );
    directories.push(directory);
    const candidate = { candidate_digest: "b".repeat(64) };
    const proof = {
      schema_version: 1,
      candidate_digest: candidate.candidate_digest,
      observation_digest: digest({}),
      cases: [{ id: "fixture", result: "pass" }],
    };
    for (const entry of RELEASE_QUALIFIERS) {
      const filename = resolve(directory, entry.path);
      mkdirSync(dirname(filename), { recursive: true });
      writeFileSync(
        filename,
        `console.log(${JSON.stringify(
          JSON.stringify(proof)
        )}); process.on('SIGTERM', () => {}); setInterval(() => {}, 1000);\n`
      );
    }
    let actual;
    const spawn = vi.fn((command, args, options) => {
      expect(options.timeout).toBe(QUALIFIER_PROCESS_TIMEOUT_MS);
      expect(JSON.parse(options.input).candidate_directory).toBe(directory);
      actual = spawnSync(command, args, { ...options, timeout: 250 });
      return actual;
    });
    expect(() =>
      runReleaseQualifiers(directory, candidate, {}, { directory, spawn })
    ).toThrow("stale/incomplete evidence");
    expect(actual.error.code).toBe("ETIMEDOUT");
    expect(spawn).toHaveBeenCalledOnce();
  });

  it("uses explicit account, remote D1 config, and confirmed exact-version rollback arguments", () => {
    const spawn = vi.fn(() => ({ status: 0 })),
      adapter = fixture({ spawn });
    adapter.migrate({ authority: "auth", binding: "AUTH_DB" });
    expect(spawn.mock.calls[0][1].slice(1)).toEqual([
      "d1",
      "migrations",
      "apply",
      "AUTH_DB",
      "--remote",
      "--config",
      resolve(adapter.directory, "migrations/auth.json"),
    ]);
    adapter.rollback("fixture-auth", version, "transaction");
    expect(spawn.mock.calls[1][1].slice(1)).toEqual([
      "rollback",
      version,
      "--name",
      "fixture-auth",
      "--message",
      "release recovery transaction",
      "--yes",
    ]);
    expect(() =>
      adapter.rollback("fixture-auth", "../unknown", "transaction")
    ).toThrow("invalid");
  });
  it("observes exact active versions and annotations through the real API URL contract", async () => {
    const fetchImpl = vi.fn(async (url, options) => {
      expect(options.redirect).toBe("error");
      expect(options.headers.Authorization).toBe(
        "Bearer synthetic-api-credential"
      );
      return url.endsWith("/deployments")
        ? ok({
            deployments: [
              { versions: [{ version_id: version, percentage: 100 }] },
            ],
          })
        : ok({
            annotations: {
              "workers/tag": "transaction:auth",
              "workers/message": "candidate=fixture",
            },
          });
    });
    expect(await fixture({ fetchImpl }).observeWorker("fixture-auth")).toEqual({
      status: "present",
      version,
      tag: "transaction:auth",
      message: "candidate=fixture",
    });
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      `https://api.cloudflare.com/client/v4/accounts/${account}/workers/scripts/fixture-auth/deployments`,
      `https://api.cloudflare.com/client/v4/accounts/${account}/workers/scripts/fixture-auth/versions/${version}`,
    ]);
  });
  it("only accepts the documented Worker-not-found response as absence, never auth/transport failures", async () => {
    const absent = fixture({
      fetchImpl: async () =>
        Response.json(
          { success: false, errors: [{ code: 10007 }] },
          { status: 404 }
        ),
    });
    expect(await absent.observeWorker("fixture-auth")).toEqual({
      status: "absent",
    });
    for (const status of [401, 403, 404, 500]) {
      const adapter = fixture({
        fetchImpl: async () =>
          Response.json(
            {
              success: false,
              errors: [{ code: 10000, message: "synthetic-secret-value" }],
            },
            { status }
          ),
      });
      await expect(adapter.observeWorker("fixture-auth")).rejects.toThrow(
        `HTTP ${status}`
      );
    }
    await expect(
      fixture({
        fetchImpl: async () => {
          throw new Error("synthetic-secret-value");
        },
      }).observeWorker("fixture-auth")
    ).rejects.toThrow("outcome is unknown");
  });
  it("rejects split or malformed versions rather than interpreting them as a first release", () => {
    for (const versions of [
      [],
      [
        { version_id: version, percentage: 50 },
        { version_id: version, percentage: 50 },
      ],
      [{ version_id: "not-a-version", percentage: 100 }],
    ])
      expect(() => activeVersion({ versions })).toThrow("one observed 100%");
  });
  it("creates via an exact response ID, verifies vector dimensions, and never infers absence from truncated lists", async () => {
    const fetchImpl = vi.fn(async (url, options) => {
      expect(url).toBe(
        `https://api.cloudflare.com/client/v4/accounts/${account}/d1/database`
      );
      expect(options.method).toBe("POST");
      expect(JSON.parse(options.body)).toEqual({ name: "fixture-auth" });
      return ok({ uuid: version, name: "fixture-auth" });
    });
    expect(
      await fixture({ fetchImpl }).create({ kind: "d1", name: "fixture-auth" })
    ).toEqual({ created_id: version });
    await expect(
      fixture({
        fetchImpl: async () =>
          ok(
            Array.from({ length: 1000 }, (_, index) => ({
              name: `other-${index}`,
            }))
          ),
      }).observeResource({ kind: "d1", name: "missing" })
    ).rejects.toThrow("pagination is ambiguous");
    await expect(
      fixture({
        fetchImpl: async () =>
          ok([
            { name: "vectors", config: { dimensions: 1536, metric: "cosine" } },
          ]),
      }).observeResource({
        kind: "vectorize",
        name: "vectors",
        dimensions: 1024,
        metric: "cosine",
      })
    ).rejects.toThrow("dimensions");
  });
  it("retains Vectorize metadata and one-day R2 lifecycle policies with concrete locked command arguments", () => {
    const spawn = vi.fn(() => ({ status: 0 })),
      adapter = fixture({ spawn });
    adapter.addPolicy({
      kind: "vectorize",
      name: "vectors",
      id: "created_at",
      type: "number",
    });
    adapter.addPolicy({
      kind: "r2",
      name: "assets",
      id: "expiry",
      prefix: "cf-sync/",
      seconds: 86400,
    });
    expect(spawn.mock.calls.map(([, args]) => args.slice(1))).toEqual([
      [
        "vectorize",
        "create-metadata-index",
        "vectors",
        "--propertyName",
        "created_at",
        "--type",
        "number",
      ],
      [
        "r2",
        "bucket",
        "lifecycle",
        "add",
        "assets",
        "expiry",
        "cf-sync/",
        "--expire-days",
        "1",
      ],
    ]);
  });
  it("enforces seven-day temporary expiry and a non-expiring permanent frame bucket", async () => {
    const spawn = vi.fn(() => ({ status: 0 }));
    let rules = [];
    const adapter = fixture({ spawn, fetchImpl: async () => ok({ rules }) });
    const temporary = {
      kind: "r2",
      name: "temporary-frames",
      id: "expiry",
      prefix: "",
      seconds: 604800,
      exclusive: true,
    };
    adapter.addPolicy(temporary);
    expect(spawn.mock.calls[0][1].slice(-2)).toEqual(["--expire-days", "7"]);
    const permanent = {
      kind: "r2",
      name: "permanent-frames",
      id: "retain",
      retain: true,
    };
    expect(await adapter.observePolicy(permanent)).toEqual({
      status: "present",
    });
    rules = [
      {
        id: "expiry",
        enabled: true,
        conditions: { prefix: "" },
        deleteObjectsTransition: { condition: { type: "Age", maxAge: 604800 } },
      },
    ];
    expect(await adapter.observePolicy(temporary)).toEqual({
      status: "present",
    });
    await expect(adapter.observePolicy(permanent)).rejects.toThrow(
      "must not expire"
    );
    rules.push({
      ...rules[0],
      id: "shorter",
      deleteObjectsTransition: { condition: { type: "Age", maxAge: 86400 } },
    });
    await expect(adapter.observePolicy(temporary)).rejects.toThrow(
      "unowned expiration"
    );
    expect(() => adapter.addPolicy(permanent)).toThrow(
      "explicit positive whole-day"
    );
  });
  it("observes bucket-wide expiry with the live API's omitted prefix without accepting changed scope", async () => {
    const policy = {
      kind: "r2",
      name: "temporary-frames",
      id: "expire-temporary-frames",
      prefix: "",
      seconds: 604800,
      exclusive: true,
    };
    // Exact non-secret rule shape observed after the Eddy provisioning event
    // on 2026-09-06; the locked Wrangler writer omits an empty prefix.
    let conditions = {};
    const adapter = fixture({
      fetchImpl: async () =>
        ok({
          rules: [
            {
              id: policy.id,
              enabled: true,
              conditions,
              deleteObjectsTransition: {
                condition: { type: "Age", maxAge: 604800 },
              },
            },
          ],
        }),
    });
    expect(await adapter.observePolicy(policy)).toEqual({ status: "present" });
    conditions = { prefix: "" };
    expect(await adapter.observePolicy(policy)).toEqual({ status: "present" });
    for (conditions of [
      undefined,
      null,
      [],
      { prefix: null },
      { prefix: 0 },
      { other: "unknown" },
      { prefix: "images/" },
    ]) {
      await expect(adapter.observePolicy(policy)).rejects.toThrow(
        "R2 lifecycle"
      );
    }
    conditions = {};
    await expect(
      adapter.observePolicy({ ...policy, prefix: "images/" })
    ).rejects.toThrow("owner differs");
  });
  it("reads only the D1 migration ledger and refuses incomplete query envelopes", async () => {
    const fetchImpl = vi.fn(async (_url, options) => {
      const sql = JSON.parse(options.body).sql;
      return ok([
        {
          success: true,
          results: [
            {
              name: sql.includes("sqlite_master")
                ? "d1_migrations"
                : "0001.sql",
            },
          ],
        },
      ]);
    });
    expect(
      await fixture({ fetchImpl }).migrationLedger({ database_id: version })
    ).toEqual(["0001.sql"]);
    expect(
      fetchImpl.mock.calls.every(([, options]) => options.method === "POST")
    ).toBe(true);
    await expect(
      fixture({ fetchImpl: async () => ok([{ results: [] }]) }).migrationLedger(
        { database_id: version }
      )
    ).rejects.toThrow("incomplete");
  });
  it.each(["html", "not-ready", "503"])(
    "does not accept %s as release readiness",
    async (kind) => {
      const fetchImpl = async () =>
        kind === "html"
          ? new Response("<html>ok</html>")
          : Response.json(
              { status: kind === "503" ? "ready" : "ok" },
              { status: kind === "503" ? 503 : 200 }
            );
      await expect(
        fixture({ fetchImpl }).readiness({ attempts: 1 })
      ).rejects.toThrow("readiness");
    }
  );
  it("checks both profile-owned public readiness URLs", async () => {
    const fetchImpl = vi.fn(async () => Response.json({ status: "ready" }));
    expect(await fixture({ fetchImpl }).readiness()).toEqual({
      edge: "ready",
      web: "ready",
    });
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      "https://api.fixture.invalid/ready",
      "https://web.fixture.invalid/api/worker-ready",
    ]);
  });
  it("retries readiness propagation with a controllable delay and unchanged public owners", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockImplementation(async () => Response.json({ status: "ready" }));
    const sleep = vi.fn(async () => {});
    await fixture({ fetchImpl }).readiness({ sleep });
    expect(sleep).toHaveBeenCalledExactlyOnceWith(2000);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });
  it("does not accept an operator-written approved file in place of missing product/schema runners", () => {
    const directory = mkdtempSync(
      resolve(tmpdir(), "cf-release-qualification-")
    );
    directories.push(directory);
    writeFileSync(
      resolve(directory, "approved.json"),
      JSON.stringify({ approved: true })
    );
    expect(pendingQualifiers(directory)).toEqual([
      "CF-4",
      "CI-1",
      "prior-schema",
    ]);
    const spawn = vi.fn();
    expect(() => runReleaseQualifiers(directory, {}, {}, { spawn })).toThrow(
      "pending"
    );
    expect(spawn).not.toHaveBeenCalled();
  });
});
