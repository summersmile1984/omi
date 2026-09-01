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
  let prerequisitesOk = true;
  for (const queueName of item.queueConsumers) {
    const consumerRemoval = spawnSync(
      "npx",
      ["wrangler", "queues", "consumer", "remove", queueName, item.workerName],
      {
        stdio: ["ignore", "inherit", "inherit"],
        env: process.env,
      },
    );
    if (consumerRemoval.status !== 0) prerequisitesOk = false;
  }
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
  if (!prerequisitesOk || result.status !== 0) {
    const status = spawnSync(
      "npx",
      [
        "wrangler",
        "deployments",
        "status",
        "--name",
        item.workerName,
        "--json",
      ],
      {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
        env: process.env,
      },
    );
    if (
      item.action !== "delete" ||
      status.status === 0 ||
      !String(status.stderr).includes("[code: 10007]")
    ) {
      throw new Error(`production rollback failed for ${item.workerName}`);
    }
  }
}
