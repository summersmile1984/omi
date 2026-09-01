import type { JobsEnv } from "./env";

const MANIFEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export function isolatedCutoverManifestId(env: JobsEnv): string | null {
  const value = env.ACCOUNT_CUTOVER_MANIFEST_ID?.trim();
  return value && MANIFEST_ID.test(value) ? value : null;
}
