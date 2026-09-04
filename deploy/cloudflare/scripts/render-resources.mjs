import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { renderResourcePlan } from "./resource-configs.mjs";
import { assertDisjointPlans } from "./resource-input.mjs";
import {
  assertPlanIntegrity,
  materializeResourceBundle,
} from "./resource-bundle.mjs";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
try {
  const { values } = parseArgs({
    options: {
      brand: { type: "string" },
      manifest: { type: "string" },
      inventory: { type: "string" },
      "web-build": { type: "string" },
      output: { type: "string" },
      check: { type: "boolean", default: false },
      compare: { type: "string", multiple: true, default: [] },
    },
  });
  if (
    !values.inventory ||
    !values["web-build"] ||
    !values.output ||
    (!values.brand && !values.manifest)
  )
    throw new Error(
      "Required: --inventory --web-build --output and --brand or --manifest",
    );
  const input = JSON.parse(readFileSync(resolve(values.inventory), "utf8"));
  const command = [
    resolve(root, "../web/profile_input.py"),
    "--target",
    "cloudflare",
    "--stage",
    input.stage,
  ];
  if (values.brand) command.push("--brand", values.brand);
  if (values.manifest) command.push("--manifest", resolve(values.manifest));
  const profile = spawnSync(process.env.PYTHON ?? "python3", command, {
    cwd: root,
    encoding: "utf8",
  });
  if (profile.status !== 0)
    throw new Error(profile.stderr || "brand/profile render failed");
  const projected = JSON.parse(profile.stdout);
  const webManifest = JSON.parse(
    readFileSync(resolve(values["web-build"], "build-manifest.json"), "utf8"),
  );
  const webArtifact = resolve(values["web-build"], "artifact");
  const webConfig = JSON.parse(
    readFileSync(resolve(webArtifact, "wrangler.json"), "utf8"),
  );
  const git = spawnSync("git", ["rev-parse", "HEAD"], {
    cwd: root,
    encoding: "utf8",
  });
  if (git.status !== 0) throw new Error("source commit unavailable");
  const plan = renderResourcePlan({
    root,
    input,
    projected,
    web: { manifest: webManifest, config: webConfig },
    sourceCommit: git.stdout.trim(),
  });
  const comparisons = values.compare.map((path) =>
    JSON.parse(readFileSync(resolve(path), "utf8")),
  );
  for (const compared of comparisons) assertPlanIntegrity(compared);
  assertDisjointPlans([plan, ...comparisons]);
  const migrationCheck = spawnSync(
    process.env.PYTHON ?? "python3",
    [resolve(root, "scripts/verify-resource-migrations.py"), "--root", root],
    { input: JSON.stringify(plan), encoding: "utf8" },
  );
  if (migrationCheck.status !== 0)
    throw new Error(migrationCheck.stderr || "SQL migration fixture failed");
  const result = materializeResourceBundle(plan, {
    root,
    output: values.output,
    webArtifact,
    check: values.check,
  });
  console.log(
    JSON.stringify({
      brand: plan.brand,
      target: plan.target,
      stage: plan.stage,
      plan_digest: plan.plan_digest,
      resources: plan.resources.length,
      ...result,
      ...JSON.parse(migrationCheck.stdout),
    }),
  );
  if (!result.render_valid) process.exitCode = 1;
} catch (error) {
  console.error(`FAIL: ${error.message}`);
  process.exitCode = 1;
}
