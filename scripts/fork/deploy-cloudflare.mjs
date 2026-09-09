import { readFileSync, mkdirSync, cpSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    delivery: { type: "string" },
    "journal-root": { type: "string" },
  },
});
try {
  const journalRoot = values["journal-root"];
  if (
    !journalRoot ||
    !isAbsolute(journalRoot) ||
    journalRoot.includes("/_work/")
  )
    throw new Error(
      "release journal requires a persistent absolute directory outside the runner checkout"
    );
  const directory = resolve(values.delivery, "unpacked/cloudflare");
  const receipt = JSON.parse(
    readFileSync(resolve(values.delivery, "delivery.json"))
  );
  const candidate = JSON.parse(
    readFileSync(resolve(directory, "candidate.json"))
  );
  if (
    candidate.candidate_digest !== receipt.candidate_digest ||
    candidate.source.commit !== receipt.commit
  )
    throw new Error("candidate differs from the verified delivery");
  const secrets = JSON.parse(process.env.RELEASE_SECRETS_JSON || "{}");
  const env = {
    ...process.env,
    FORK_RELEASE_CI_RUN_ID: String(receipt.ci_run_id),
  };
  delete env.RELEASE_SECRETS_JSON;
  for (const name of new Set(
    Object.values(candidate.inventory.secret_refs).flatMap((row) =>
      Object.values(row)
    )
  )) {
    if (
      typeof name !== "string" ||
      !/^[A-Z][A-Z0-9_]+$/.test(name) ||
      typeof secrets[name] !== "string" ||
      !secrets[name]
    )
      throw new Error("a required deployment secret reference is unresolved");
    env[name] = secrets[name];
  }
  if (!env.CLOUDFLARE_API_TOKEN)
    throw new Error("Cloudflare deployment token is missing");
  mkdirSync(journalRoot, { recursive: true, mode: 0o700 });
  const journal = resolve(
    journalRoot,
    `${receipt.stage}-${receipt.commit}-${randomUUID()}`
  );
  // Recovery must retain the accepted bytes after Actions removes runner.temp.
  // A unique transaction owns this copy; the release owner revalidates it.
  const retained = `${journal}-candidate`;
  cpSync(directory, retained, {
    recursive: true,
    verbatimSymlinks: true,
    errorOnExist: true,
    force: false,
  });
  const result = spawnSync(
    process.execPath,
    [
      "deploy/cloudflare/scripts/release.mjs",
      "apply",
      "--candidate",
      retained,
      "--journal",
      journal,
      "--authorize",
      candidate.candidate_digest,
    ],
    { env, stdio: "inherit", timeout: 150 * 60 * 1000 }
  );
  if (result.status !== 0)
    throw new Error(`Cloudflare release failed; retained journal: ${journal}`);
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
}
