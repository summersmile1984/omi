const D1_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const STAGING_D1_MIGRATIONS = Object.freeze([
  Object.freeze({
    databaseName: "omi-cf-auth-staging",
    binding: "AUTH_DB",
    migrationsDir: "migrations/auth",
  }),
  Object.freeze({
    databaseName: "omi-cf-app-staging",
    binding: "APP_DB",
    migrationsDir: "migrations/app",
  }),
]);

export const PRODUCTION_D1_MIGRATIONS = Object.freeze([
  Object.freeze({
    databaseName: "omi-cf-auth-production",
    binding: "AUTH_DB",
    migrationsDir: "migrations/auth",
  }),
  Object.freeze({
    databaseName: "omi-cf-app-production",
    binding: "APP_DB",
    migrationsDir: "migrations/app",
  }),
]);

export function resolveD1DatabaseId(rawList, databaseName) {
  let databases;
  try {
    databases = JSON.parse(rawList);
  } catch {
    throw new Error("wrangler d1 list returned invalid JSON");
  }
  if (!Array.isArray(databases)) {
    throw new Error("wrangler d1 list returned an invalid database list");
  }
  const matches = databases.filter(
    (database) => database?.name === databaseName,
  );
  if (matches.length !== 1) {
    throw new Error(
      `expected exactly one D1 database named ${databaseName}, found ${matches.length}`,
    );
  }
  const databaseId = matches[0]?.uuid;
  if (typeof databaseId !== "string" || !D1_UUID.test(databaseId)) {
    throw new Error(`D1 database ${databaseName} has an invalid UUID`);
  }
  return databaseId;
}

export function createD1MigrationConfig({
  databaseName,
  databaseId,
  binding,
  migrationsDir,
}) {
  if (
    !databaseName ||
    !binding ||
    !migrationsDir ||
    !D1_UUID.test(databaseId)
  ) {
    throw new Error("invalid D1 migration config input");
  }
  return {
    name: `omi-cf-${binding.toLowerCase().replaceAll("_", "-")}-migration-helper`,
    compatibility_date: "2026-08-27",
    d1_databases: [
      {
        binding,
        database_name: databaseName,
        database_id: databaseId,
        migrations_dir: migrationsDir,
      },
    ],
  };
}

export function resolveStagingD1Migrations(rawList, resolveDirectory) {
  return resolveD1Migrations(rawList, resolveDirectory, STAGING_D1_MIGRATIONS);
}

export function resolveProductionD1Migrations(rawList, resolveDirectory) {
  return resolveD1Migrations(rawList, resolveDirectory, PRODUCTION_D1_MIGRATIONS);
}

function resolveD1Migrations(rawList, resolveDirectory, migrations) {
  if (typeof resolveDirectory !== "function") {
    throw new Error("D1 migration directory resolver is required");
  }
  return migrations.map((migration) => ({
    ...migration,
    databaseId: resolveD1DatabaseId(rawList, migration.databaseName),
    migrationsDir: resolveDirectory(migration.migrationsDir),
  }));
}

export function assertNoPendingD1Migrations(output, databaseName) {
  const normalized = String(output).replaceAll(
    /\u001b\[[0-?]*[ -/]*[@-~]/g,
    "",
  );
  if (!normalized.includes("No migrations to apply!")) {
    throw new Error(`D1 database ${databaseName} has unapplied migrations`);
  }
}
