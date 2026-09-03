import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  assertNoPendingD1Migrations,
  createD1MigrationConfig,
  resolveStagingD1Migrations,
} from "./d1-migrations.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function runQuiet(command, args) {
  const result = spawnSync(command, args, {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  return {
    ok: result.status === 0,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
  };
}

const listed = runQuiet("npx", ["wrangler", "d1", "list", "--json"]);
if (!listed.ok) {
  throw new Error(
    `unable to resolve staging D1 UUIDs: ${listed.stderr.trim() || "wrangler failed"}`,
  );
}

const migrations = resolveStagingD1Migrations(listed.stdout, (directory) =>
  resolve(root, directory),
);
for (const migration of migrations) {
  const temporaryDirectory = mkdtempSync(join(tmpdir(), "omi-cf-d1-verify-"));
  const configPath = resolve(temporaryDirectory, "wrangler.jsonc");
  writeFileSync(
    configPath,
    `${JSON.stringify(createD1MigrationConfig(migration), null, 2)}\n`,
    { mode: 0o600, flag: "wx" },
  );
  try {
    const result = runQuiet("npx", [
      "wrangler",
      "d1",
      "migrations",
      "list",
      migration.binding,
      "--remote",
      "--config",
      configPath,
    ]);
    if (!result.ok) {
      throw new Error(
        `unable to verify ${migration.databaseName} migrations: ${result.stderr.trim() || "wrangler failed"}`,
      );
    }
    assertNoPendingD1Migrations(
      `${result.stdout}\n${result.stderr}`,
      migration.databaseName,
    );
    console.log(`${migration.databaseName}: no unapplied migrations`);
  } finally {
    rmSync(temporaryDirectory, { recursive: true, force: true });
  }
}
