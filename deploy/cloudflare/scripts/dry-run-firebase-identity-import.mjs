#!/usr/bin/env node
// LIFECYCLE: permanent

import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import {
  FirebaseIdentityMigrationError,
  parseFirebaseImportScryptConfig,
  planFirebaseIdentityImport,
  runIdentityImport,
} from "./import-firebase-identities.mjs";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const migrationsDirectory = path.resolve(
  scriptDirectory,
  "../migrations/auth",
);

const SYNTHETIC_HASH_CONFIG = Object.freeze({
  algorithm: "SCRYPT",
  base64_signer_key: Buffer.alloc(64, 0x31).toString("base64"),
  base64_salt_separator: Buffer.from("omi-dry-run").toString("base64"),
  rounds: 8,
  mem_cost: 14,
});

function syntheticSource(email = "identity-dry-run@example.test") {
  return {
    users: [
      {
        localId: "firebase-identity-dry-run",
        email,
        emailVerified: true,
        displayName: "Identity Dry Run",
        passwordHash: Buffer.alloc(64, 0x42).toString("base64"),
        passwordSalt: Buffer.alloc(16, 0x24).toString("base64"),
        createdAt: "1700000000000",
        lastSignedInAt: "1700000100000",
        providerUserInfo: [],
      },
    ],
  };
}

function stableSourceSha256(source) {
  return createHash("sha256")
    .update(JSON.stringify(source))
    .digest("hex");
}

function sqliteClient(database) {
  const execute = (sql, params = []) => {
    const statement = database.prepare(sql);
    if (statement.columns().length) return statement.all(...params);
    statement.run(...params);
    return [];
  };
  return Object.freeze({
    async query(sql, params = []) {
      return execute(sql, params);
    },
    async batch(statements) {
      database.exec("BEGIN IMMEDIATE");
      try {
        const results = statements.map(({ sql, params = [] }) =>
          execute(sql, params),
        );
        database.exec("COMMIT");
        return results;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    },
  });
}

function migratedDatabase() {
  const database = new DatabaseSync(":memory:");
  database.exec("PRAGMA foreign_keys = ON");
  for (const filename of readdirSync(migrationsDirectory)
    .filter((value) => value.endsWith(".sql"))
    .sort()) {
    database.exec(
      readFileSync(path.join(migrationsDirectory, filename), "utf8"),
    );
  }
  return database;
}

async function expectMigrationFailure(operation, pattern) {
  try {
    await operation();
  } catch (error) {
    if (
      error instanceof FirebaseIdentityMigrationError &&
      pattern.test(error.message)
    ) {
      return true;
    }
    throw error;
  }
  throw new Error("identity dry-run expected a fail-closed result");
}

/**
 * Exercise the real Auth migrations and importer in an isolated in-memory D1
 * equivalent. The fixture contains no Firebase, provider, or Cloudflare
 * credentials and performs no network requests.
 */
export async function runFirebaseIdentityDryRun() {
  const database = migratedDatabase();
  const client = sqliteClient(database);
  try {
    const config = parseFirebaseImportScryptConfig(SYNTHETIC_HASH_CONFIG);
    const source = syntheticSource();
    const plan = planFirebaseIdentityImport(source, config);
    const sourceSha256 = stableSourceSha256(source);
    const applied = await runIdentityImport(
      "apply",
      plan,
      sourceSha256,
      client,
      {},
    );
    const verified = await runIdentityImport(
      "verify",
      plan,
      sourceSha256,
      client,
      {},
    );
    const replayed = await runIdentityImport(
      "apply",
      plan,
      sourceSha256,
      client,
      {},
    );

    const conflictingSource = syntheticSource(
      "identity-dry-run-changed@example.test",
    );
    const conflictingPlan = planFirebaseIdentityImport(
      conflictingSource,
      config,
    );
    const sourceConflictRejected = await expectMigrationFailure(
      () =>
        runIdentityImport(
          "apply",
          conflictingPlan,
          stableSourceSha256(conflictingSource),
          client,
          {},
        ),
      /identity import ledger does not match/,
    );

    database
      .prepare(
        "UPDATE cf_firebase_identity_projection SET status = 'revoked' WHERE firebaseUid = ?",
      )
      .run("firebase-identity-dry-run");
    const revokedProjectionRejected = await expectMigrationFailure(
      () =>
        runIdentityImport("verify", plan, sourceSha256, client, {}),
      /identity projection conflicts/,
    );
    database
      .prepare(
        "UPDATE cf_firebase_identity_projection SET status = 'imported' WHERE firebaseUid = ?",
      )
      .run("firebase-identity-dry-run");

    database
      .prepare(
        "UPDATE cf_auth_deletion_fences SET status = 'deleting' WHERE uid = ?",
      )
      .run("firebase-identity-dry-run");
    const deletionFenceRejected = await expectMigrationFailure(
      () =>
        runIdentityImport("verify", plan, sourceSha256, client, {}),
      /deletion fence is not clear/,
    );

    return Object.freeze({
      status: "passed",
      fixture: "synthetic",
      network_requests: 0,
      users: applied.users,
      accounts: applied.accounts,
      canonical_sha256: verified.canonical_sha256,
      idempotent_replay: replayed.status === "already_applied",
      source_conflict_rejected: sourceConflictRejected,
      revoked_projection_rejected: revokedProjectionRejected,
      deletion_fence_rejected: deletionFenceRejected,
    });
  } finally {
    database.close();
  }
}

async function main() {
  try {
    process.stdout.write(`${JSON.stringify(await runFirebaseIdentityDryRun())}\n`);
  } catch (error) {
    const message =
      error instanceof FirebaseIdentityMigrationError
        ? error.message
        : "identity import dry-run failed";
    process.stderr.write(`${JSON.stringify({ error: message })}\n`);
    process.exitCode = 2;
  }
}

if (fileURLToPath(import.meta.url) === path.resolve(process.argv[1] || "")) {
  await main();
}
