import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import {
  rollbackCommandArgs,
  rollbackPlan,
  ROLLBACK_STDIO,
} from "./deployment-state.mjs";
import { stagingHealthTargets, verifyStagingHealth } from "./deploy-health.mjs";

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
    rollbackCommandArgs({ workerName, versionId }, snapshotPath),
    { stdio: ROLLBACK_STDIO },
  );
  if (result.status !== 0) failures.push(workerName);
}
if (failures.length) {
  throw new Error(`rollback failed for: ${failures.join(", ")}`);
}
const restoredHealth = await verifyStagingHealth({
  targets: stagingHealthTargets(),
});
console.log(
  `Cloudflare staging rollback complete from ${snapshotPath}: ${JSON.stringify(restoredHealth)}.`,
);
