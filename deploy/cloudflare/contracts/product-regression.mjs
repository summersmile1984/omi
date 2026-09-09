import { mkdtempSync, lstatSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { startLocalTarget } from "./local-target.mjs";

export const PRODUCT_SUITES = Object.freeze([
  "core",
  "recording",
  "chat",
  "share",
]);

export function readProductReport(path, { target, brand }) {
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 4 * 1024 * 1024)
    throw new Error("product report must be a bounded ordinary file");
  const report = JSON.parse(readFileSync(path, "utf8"));
  if (
    report.schema_version !== 1 ||
    report.passed !== true ||
    report.target !== target ||
    report.brand_id !== brand ||
    !Array.isArray(report.cases) ||
    !report.cases.length ||
    report.cases.some(
      (row) =>
        row.result !== "pass" ||
        typeof row.id !== "string" ||
        !/^[a-zA-Z0-9_.:-]{1,100}$/.test(row.id)
    ) ||
    new Set(report.cases.map((row) => row.id)).size !== report.cases.length
  )
    throw new Error(
      "product regression failed or returned incomplete evidence"
    );
  return report.cases.map((row) => row.id);
}

export async function runCloudflareRegression(
  context,
  {
    suites = PRODUCT_SUITES,
    start = startLocalTarget,
    output = mkdtempSync(resolve(tmpdir(), "eddy-cf-product-")),
  } = {}
) {
  if (!suites.length || suites.some((suite) => !PRODUCT_SUITES.includes(suite)))
    throw new Error("unknown product regression suite");
  context.verify();
  process.stderr.write(`Cloudflare regression evidence: ${output}\n`);
  const target = await start({
    output: resolve(output, "target"),
    candidateContext: context,
  });
  const cases = [];
  try {
    for (const suite of suites) {
      const python = resolve(
        context.root,
        "deploy/cloudflare/python/api-core/.venv/bin/python"
      );
      const script =
        suite === "core"
          ? resolve(context.root, "contracts/deployment/core.py")
          : resolve(context.root, `deploy/cloudflare/contracts/${suite}.mjs`);
      await target.command(
        suite,
        suite === "core" ? python : process.execPath,
        [script, "--metadata", resolve(output, "target/metadata.json")]
      );
      const ids = readProductReport(
        resolve(target.metadata.trace_dir, `${suite}-results.json`),
        { target: "cloudflare", brand: context.candidate.brand }
      );
      cases.push(...ids.map((id) => `${suite}:${id}`));
    }
    target.verifyPayload();
    context.verify();
  } finally {
    await target.close();
  }
  return cases;
}
