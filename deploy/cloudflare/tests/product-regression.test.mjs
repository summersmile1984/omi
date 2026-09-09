import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readProductReport,
  runCloudflareRegression,
  PRODUCT_SUITES,
} from "../contracts/product-regression.mjs";
import { regressCandidate } from "../../../contracts/deployment/regress.mjs";
import { failedCoreCases } from "../contracts/local-target.mjs";
const directories = [];
afterEach(() => {
  for (const dir of directories.splice(0))
    rmSync(dir, { recursive: true, force: true });
});
const directory = () => {
  const path = mkdtempSync(resolve(tmpdir(), "product-qualification-test-"));
  directories.push(path);
  return path;
};
function context(phase = "candidate") {
  return {
    root: resolve(import.meta.dirname, "../../.."),
    candidate: { brand: "eddy", candidate_digest: "a".repeat(64) },
    observations: { release_phase: phase },
    verify: vi.fn(),
  };
}

it("reports failed core identities and statuses without private error text", () => {
  const trace_dir = directory();
  writeFileSync(
    resolve(trace_dir, "core-results.json"),
    JSON.stringify({
      cases: [
        {
          id: "auth.admission",
          result: "fail",
          error: "private value returned HTTP 401; expected 200",
        },
        { id: "memory.edit", result: "fail", error: "private response" },
        { id: "unsafe identifier", result: "fail", error: "private value" },
        { id: "task.create", result: "pass" },
      ],
    })
  );
  expect(failedCoreCases({ trace_dir })).toEqual([
    { id: "auth.admission", status: "returned HTTP 401; expected 200" },
    { id: "memory.edit" },
  ]);
  expect(failedCoreCases({ trace_dir: resolve(trace_dir, "missing") })).toEqual(
    []
  );
});
const report = (id) => ({
  schema_version: 1,
  brand_id: "eddy",
  target: "cloudflare",
  passed: true,
  cases: [{ id, result: "pass" }],
});
describe("candidate product regression", () => {
  it("runs all existing suites on frozen bytes and closes the target", async () => {
    const ctx = context(),
      output = directory(),
      trace = resolve(output, "target/trace");
    mkdirSync(trace, { recursive: true });
    const target = {
      metadata: { trace_dir: trace },
      verifyPayload: vi.fn(),
      close: vi.fn(),
      command: vi.fn(async (suite) =>
        writeFileSync(
          resolve(trace, `${suite}-results.json`),
          JSON.stringify(report(suite))
        )
      ),
    };
    const start = vi.fn(async () => target);
    expect(await runCloudflareRegression(ctx, { output, start })).toEqual(
      PRODUCT_SUITES.map((id) => `${id}:${id}`)
    );
    expect(start).toHaveBeenCalledWith({
      output: resolve(output, "target"),
      candidateContext: ctx,
    });
    expect(target.command.mock.calls.map((args) => args[0])).toEqual(
      PRODUCT_SUITES
    );
    expect(target.command.mock.calls[0][2][0]).toBe(
      resolve(ctx.root, "contracts/deployment/core.py")
    );
    expect(target.verifyPayload).toHaveBeenCalledOnce();
    expect(target.close).toHaveBeenCalledOnce();
  });
  it("stops after failure and awaits target cleanup", async () => {
    const close = vi.fn(),
      command = vi.fn(async () => {
        throw new Error("suite failed");
      });
    await expect(
      runCloudflareRegression(context(), {
        output: directory(),
        start: async () => ({ command, close }),
      })
    ).rejects.toThrow("suite failed");
    expect(command).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });
  it.each([
    {},
    { passed: false },
    { cases: [] },
    { brand_id: "other" },
    { target: "self_hosted" },
    { cases: [{ id: "case", result: "fail" }] },
    {
      cases: [
        { id: "case", result: "pass" },
        { id: "case", result: "pass" },
      ],
    },
  ])("rejects incomplete and wrong-owner reports %j", (patch) => {
    const path = resolve(directory(), "result.json");
    writeFileSync(
      path,
      JSON.stringify(
        Object.keys(patch).length ? { ...report("case"), ...patch } : {}
      )
    );
    expect(() =>
      readProductReport(path, { target: "cloudflare", brand: "eddy" })
    ).toThrow("incomplete evidence");
  });
  it("refuses report symlinks", () => {
    const dir = directory(),
      original = resolve(dir, "source.json"),
      link = resolve(dir, "report.json");
    writeFileSync(original, JSON.stringify(report("case")));
    symlinkSync(original, link);
    expect(() =>
      readProductReport(link, { target: "cloudflare", brand: "eddy" })
    ).toThrow("ordinary file");
  });
  it("requires matching cases from both targets and waits for both teardowns", async () => {
    const ctx = context();
    expect(
      await regressCandidate(ctx, {
        cloudflare: async () => ["core:case"],
        server: async () => ["case"],
      })
    ).toMatchObject({
      passed: true,
      release_qualified: false,
      cases: [
        { id: "cloudflare:core:case", result: "pass" },
        { id: "server:case", result: "pass" },
      ],
    });
    await expect(
      regressCandidate(ctx, {
        cloudflare: async () => ["core:case"],
        server: async () => ["different"],
      })
    ).rejects.toThrow("same HTTP cases");
    let cleaned = false;
    await expect(
      regressCandidate(ctx, {
        cloudflare: async () => {
          throw new Error("CF failed");
        },
        server: async () => {
          await Promise.resolve();
          cleaned = true;
          return ["case"];
        },
      })
    ).rejects.toThrow("CF failed");
    expect(cleaned).toBe(true);
  });
  it("does not treat deployed observations or empty suites as local proof", async () => {
    await expect(regressCandidate(context("deployed"))).rejects.toThrow(
      "candidate phase"
    );
    await expect(
      regressCandidate(context(), {
        cloudflare: async () => [],
        server: async () => [],
      })
    ).rejects.toThrow("no executed cases");
  });
});
