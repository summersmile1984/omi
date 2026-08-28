import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { rollbackPlan } from "./deployment-state.mjs";
import { verifyStagingHealth } from "./deploy-health.mjs";

const snapshotPath = process.argv[2];
if (!snapshotPath) {
  throw new Error(
    "usage: npm run rollback:staging -- <deployment-snapshot.json>",
  );
}
const snapshot = JSON.parse(await readFile(snapshotPath, "utf8"));
const failures = [];
for (const { workerName, versionId } of rollbackPlan(snapshot)) {
  const result = spawnSync(
    "npx",
    [
      "wrangler",
      "rollback",
      versionId,
      "--name",
      workerName,
      "--message",
      `staging rollback from ${snapshotPath}`,
      "--yes",
    ],
    { stdio: "inherit" },
  );
  if (result.status !== 0) failures.push(workerName);
}
if (failures.length) {
  throw new Error(`rollback failed for: ${failures.join(", ")}`);
}
const restoredHealth = await verifyStagingHealth({
  targets: [
    {
      name: "edge",
      url: "https://omi-cf-edge-staging.summersmile1984.workers.dev/health",
    },
    {
      name: "web",
      url: "https://omi-web-app-staging.summersmile1984.workers.dev/login",
    },
  ],
});
console.log(
  `Cloudflare staging rollback complete from ${snapshotPath}: ${JSON.stringify(restoredHealth)}.`,
);
