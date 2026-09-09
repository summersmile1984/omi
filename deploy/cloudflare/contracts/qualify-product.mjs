import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import {
  qualificationContext,
  qualificationProof,
  readQualificationInput,
} from "./qualification-context.mjs";
import { runCloudflareRegression } from "./product-regression.mjs";
import { runHostedCloudflare } from "./hosted-product.mjs";

export async function qualifyProduct(
  context,
  { local = runCloudflareRegression, hosted = runHostedCloudflare } = {}
) {
  context.verify();
  const phase = context.observations.release_phase;
  if (!["candidate", "deployed"].includes(phase))
    throw new Error("restored versions need their own product execution");
  const cases = await (phase === "candidate"
    ? local(context)
    : hosted(context));
  if (!cases.length) throw new Error("no product cases executed");
  context.verify();
  return qualificationProof(context.candidate, context.observations, cases);
}

if (
  process.argv[1] &&
  pathToFileURL(resolve(process.argv[1])).href === import.meta.url
) {
  try {
    const root = fileURLToPath(new URL("../../../", import.meta.url));
    console.log(
      JSON.stringify(
        await qualifyProduct(
          qualificationContext(root, await readQualificationInput())
        )
      )
    );
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
