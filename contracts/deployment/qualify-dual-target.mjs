import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { regressCandidate, runServerRegression } from "./regress.mjs";
import { runHostedCore } from "../../deploy/cloudflare/contracts/hosted-product.mjs";
import {
  qualificationContext,
  qualificationProof,
  readQualificationInput,
} from "../../deploy/cloudflare/contracts/qualification-context.mjs";

export async function qualifyDualTarget(
  context,
  {
    candidate = regressCandidate,
    server = runServerRegression,
    hosted = runHostedCore,
    spawn = spawnSync,
  } = {}
) {
  context.verify();
  const run = process.env.FORK_RELEASE_CI_RUN_ID;
  if (!/^\d+$/.test(run || ""))
    throw new Error("release requires its exact-source full CI run");
  const ci = spawn(
    resolve(context.root, "backend/.venv/bin/python"),
    [
      resolve(context.root, "scripts/fork/release_ci.py"),
      "ci",
      "--run-id",
      run,
      "--sha",
      context.candidate.source.commit,
    ],
    { encoding: "utf8", timeout: 60000 }
  );
  if (ci.status !== 0)
    throw new Error("full platform/brand CI source verification failed");
  let cases;
  if (context.observations.release_phase === "candidate") {
    cases = (await candidate(context)).cases.map((row) => row.id);
  } else if (context.observations.release_phase === "deployed") {
    const os = await server(context),
      cf = hosted(context).filter((id) => !id.startsWith("cleanup."));
    if (JSON.stringify([...os].sort()) !== JSON.stringify([...cf].sort()))
      throw new Error(
        "deployed Cloudflare and Server did not pass identical common business cases"
      );
    cases = [
      ...os.map((id) => `server:${id}`),
      ...cf.map((id) => `cloudflare:${id}`),
    ];
  } else
    throw new Error(
      "restored versions require separate dual-target acceptance"
    );
  if (!cases.length)
    throw new Error("dual-target qualification executed no cases");
  context.verify();
  return qualificationProof(context.candidate, context.observations, [
    "ci.exact-source-platform-brand-contracts",
    ...cases,
  ]);
}

if (
  process.argv[1] &&
  pathToFileURL(resolve(process.argv[1])).href === import.meta.url
) {
  try {
    const root = fileURLToPath(new URL("../../", import.meta.url));
    console.log(
      JSON.stringify(
        await qualifyDualTarget(
          qualificationContext(root, await readQualificationInput())
        )
      )
    );
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
