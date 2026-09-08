import { parseEnv } from "node:util";

// Read generated 256-bit fixture credentials with Wrangler's dotenv syntax.
// Never include the source file, credential name or value in an error.
export function fixtureSecret(contents, name) {
  const value = parseEnv(contents)[name];
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value))
    throw new Error("local fixture credential is missing or invalid");
  return value;
}
