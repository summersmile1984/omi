import { mkdtempSync, openSync, closeSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { LocalProcesses } from "../../deploy/cloudflare/contracts/local-process.mjs";
import {
  readProductReport,
  runCloudflareRegression,
} from "../../deploy/cloudflare/contracts/product-regression.mjs";
import { qualificationContext } from "../../deploy/cloudflare/contracts/qualification-context.mjs";

import {
  verifyCandidate,
  writeJson,
} from "../../deploy/cloudflare/scripts/release-files.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

export async function runServerRegression(context) {
  const output = mkdtempSync(resolve(tmpdir(), "eddy-server-product-"));
  process.stderr.write(`Server regression evidence: ${output}\n`);
  const processes = new LocalProcesses();
  const fd = openSync(resolve(output, "runner.log"), "wx", 0o600);
  try {
    const result = await processes.run(
      resolve(context.root, "backend/.venv/bin/python"),
      [
        resolve(context.root, "deploy/self-host/ci/product.py"),
        "--output",
        resolve(output, "target"),
        "--brand-id",
        context.candidate.brand,
        "--port",
        process.env.SELF_HOST_CI_PORT || "34800",
        "--self-test",
        ...(process.env.SELF_HOST_CI_RUNTIME_IMAGE
          ? ["--runtime-image", process.env.SELF_HOST_CI_RUNTIME_IMAGE]
          : []),
      ],
      {
        cwd: context.root,
        env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
        timeout: 14 * 60 * 1000,
        stdio: ["ignore", fd, fd],
      }
    );
    if (result.status !== 0)
      throw new Error(
        `Server product regression failed; inspect ${output}/runner.log`
      );
    return readProductReport(resolve(output, "target/core-results.json"), {
      target: "self_hosted",
      brand: context.candidate.brand,
    });
  } finally {
    closeSync(fd);
    await processes.close();
  }
}

// Existing product owners run on both targets. The shared comparison covers
// core.py; the CF suite additionally exercises recording, chat and share.
// This result is a regression report, never a complete release attestation.
export async function regressCandidate(
  context,
  { cloudflare = runCloudflareRegression, server = runServerRegression } = {}
) {
  if (context.observations.release_phase !== "candidate")
    throw new Error("local regression requires the candidate phase");
  context.verify();
  const results = await Promise.allSettled([
    cloudflare(context),
    server(context),
  ]);
  for (const result of results)
    if (result.status === "rejected") throw result.reason;
  const [cf, os] = results.map((result) => result.value);
  if (!cf.length || !os.length)
    throw new Error("deployment target returned no executed cases");
  if (
    JSON.stringify(
      cf
        .filter((id) => id.startsWith("core:"))
        .map((id) => id.slice(5))
        .sort()
    ) !== JSON.stringify([...os].sort())
  )
    throw new Error("deployment targets did not pass the same HTTP cases");
  context.verify();
  return {
    schema_version: 1,
    candidate_digest: context.candidate.candidate_digest,
    brand_id: context.candidate.brand,
    passed: true,
    release_qualified: false,
    scope: "cloudflare-core-recording-chat-share-and-server-core",
    cases: [
      ...cf.map((id) => ({ id: `cloudflare:${id}`, result: "pass" })),
      ...os.map((id) => ({ id: `server:${id}`, result: "pass" })),
    ],
  };
}

if (
  process.argv[1] &&
  pathToFileURL(resolve(process.argv[1])).href === import.meta.url
) {
  try {
    const { values } = parseArgs({
      options: { candidate: { type: "string" } },
    });
    if (!values.candidate) throw new Error("--candidate directory is required");
    const directory = resolve(values.candidate);
    const context = qualificationContext(root, {
      candidate_directory: directory,
      candidate: verifyCandidate(directory, root),
      observations: { release_phase: "candidate" },
    });
    const output = mkdtempSync(resolve(tmpdir(), "eddy-dual-regression-"));
    process.stderr.write(`Combined regression report: ${output}/result.json\n`);
    const result = await regressCandidate(context);
    writeJson(output, "result.json", result);
    process.stdout.write(JSON.stringify(result) + "\n");
  } catch (error) {
    process.stderr.write(error.message + "\n");
    process.exitCode = 1;
  }
}
