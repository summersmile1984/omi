const D1_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

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
