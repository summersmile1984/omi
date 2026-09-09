import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

// Read-only inspection of this fixture's actual Wrangler SQLite stores. It
// never seeds records, invokes a deletion processor, or accesses a remote DB.
// The application registry supplies every product identity column; counts come
// from persisted data, independently of HTTP admission/deletion responses.
export async function localPrivacyObserver(metadata) {
  const state = resolve(dirname(metadata.trace_dir), "state/v3");
  const compiled = await build({
    entryPoints: [
      resolve(
        dirname(fileURLToPath(import.meta.url)),
        "../workers/jobs/account-deletion-residual.ts",
      ),
    ],
    bundle: true,
    write: false,
    platform: "node",
    format: "esm",
  });
  const registry = await import(
    `data:text/javascript;base64,${Buffer.from(
      compiled.outputFiles[0].text,
    ).toString("base64")}`
  );
  const prefixes = [
    ...new Set([
      ...registry.ACCOUNT_DELETION_R2_PREFIX_PATTERNS,
      ...registry.ACCOUNT_DELETION_CONVERSATION_RECORDING_PREFIX_PATTERNS,
      ...registry.ACCOUNT_DELETION_CHAT_FILES_PREFIX_PATTERNS,
      ...registry.ACCOUNT_DELETION_SPEECH_PROFILE_PREFIX_PATTERNS,
    ]),
  ];
  return (uid, memoryId) => {
    assert(registry.validAccountDeletionUid(uid));
    assert(memoryId === undefined || (typeof memoryId === "string" && memoryId.length > 0 && memoryId.length <= 256));
    const result = {
      app: {},
      auth: {},
      r2_objects: 0,
      app_tombstones: 0,
      pending_deletions: 0,
    };
    let appFound = false,
      authFound = false;
    for (const kind of ["d1", "r2"]) {
      const directory = resolve(state, kind);
      for (const file of readdirSync(directory, { recursive: true }).filter(
        (file) => file.endsWith(".sqlite"),
      )) {
        const database = new DatabaseSync(resolve(directory, file), {
          readOnly: true,
        });
        try {
          const tables = new Set(
            database
              .prepare("SELECT name FROM sqlite_master WHERE type='table'")
              .all()
              .map((row) => row.name),
          );
          const count = (table, column) => {
            assert(
              /^[a-zA-Z_][a-zA-Z0-9_]*$/.test(table) &&
                /^[a-zA-Z_][a-zA-Z0-9_]*$/.test(column),
            );
            return database
              .prepare(
                `SELECT COUNT(*) AS n FROM "${table}" WHERE "${column}" = ?`,
              )
              .get(uid).n;
          };
          if (kind === "d1" && tables.has("cf_conversations")) {
            assert(!appFound, "ambiguous local App DB");
            appFound = true;
            for (const {
              table,
              column,
            } of registry.ACCOUNT_DELETION_D1_SURFACES)
              result.app[`${table}.${column}`] = count(table, column);
            result.app_tombstones = count(
              "cf_account_deletion_tombstones",
              "uid",
            );
            result.pending_deletions = count(
              "cf_account_deletion_intents",
              "uid",
            );
            if (memoryId !== undefined) {
              const owned = (sql) => database.prepare(sql).get(uid, memoryId).n;
              result.memory_cleanup = {
                rows: owned("SELECT count(*) AS n FROM cf_memories WHERE uid = ? AND id = ?"),
                artifacts: owned("SELECT count(*) AS n FROM cf_memory_vector_artifacts WHERE uid = ? AND source_id = ?"),
                mappings: owned("SELECT count(*) AS n FROM cf_vector_projection_state WHERE uid = ? AND source_id = ? AND projection_kind = 'memory'"),
                requests: owned("SELECT count(*) AS n FROM cf_memory_privacy_deletions d, json_each(d.targets_json) t WHERE d.uid = ? AND json_extract(t.value, '$.id') = ?"),
              };
            }
          }
          if (kind === "d1" && tables.has("user")) {
            assert(!authFound, "ambiguous local Auth DB");
            authFound = true;
            result.auth["user.id"] = count("user", "id");
            result.auth["verification.value"] = count("verification", "value");
            for (const table of tables) {
              // The durable revocation fence is intentionally retained by Auth.
              if (table === "cf_auth_deletion_fences") continue;
              assert(/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(table));
              for (const { name } of database
                .prepare(`PRAGMA table_info("${table}")`)
                .all())
                if (["uid", "userId", "betterAuthUserId"].includes(name))
                  result.auth[`${table}.${name}`] = count(table, name);
            }
          }
          if (kind === "r2" && tables.has("_mf_objects")) {
            for (const pattern of prefixes) {
              const prefix = pattern.replace("{uid}", uid);
              result.r2_objects += database
                .prepare(
                  "SELECT COUNT(*) AS n FROM _mf_objects WHERE substr(key, 1, ?) = ?",
                )
                .get(prefix.length, prefix).n;
            }
          }
        } finally {
          database.close();
        }
      }
    }
    assert(appFound && authFound, "missing real local database authority");
    return result;
  };
}
