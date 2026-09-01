import { readFileSync, statSync } from "node:fs";
import { basename, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import {
  assertProductionConfirmation,
  productionRollbackPlan,
} from "./production-environment.mjs";

assertProductionConfirmation();

const rawPath = process.argv[2];
if (!rawPath) {
  throw new Error("usage: npm run rollback:production -- <snapshot.json>");
}
const snapshotPath = resolve(rawPath);
if ((statSync(snapshotPath).mode & 0o077) !== 0) {
  throw new Error("production rollback snapshot must use mode 0600");
}
const snapshot = JSON.parse(readFileSync(snapshotPath, "utf8"));
const message = `manual production rollback: ${basename(snapshotPath)}`.slice(
  0,
  120,
);
for (const item of productionRollbackPlan(snapshot)) {
  const args =
    item.action === "delete"
      ? ["wrangler", "delete", "--name", item.workerName, "--force"]
      : [
          "wrangler",
          "rollback",
          item.versionId,
          "--name",
          item.workerName,
          "--message",
          message,
          "--yes",
        ];
  const result = spawnSync("npx", args, {
    stdio: ["ignore", "inherit", "inherit"],
    env: process.env,
  });
  if (result.status !== 0) {
    throw new Error(`production rollback failed for ${item.workerName}`);
  }
}
