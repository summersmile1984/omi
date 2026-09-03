import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  planPhoneHistory,
  renderPhoneHistoryLedgerSql,
  renderPhoneHistoryVerifySql,
  verifyPhoneHistory,
} from "../scripts/phone-history-reconcile.mjs";

const exportSha256 = "b".repeat(64);
const hash = "a".repeat(64);
const ciphertext = `${Buffer.alloc(12).toString("base64url")}.${Buffer.alloc(32).toString("base64url")}`;

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, nested]) => `${JSON.stringify(key)}:${stableJson(nested)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

const source = {
  kind: "firestore",
  collection: "users/{uid}/phone_numbers",
  ciphertext_scheme: "cloudflare-phone-aes-gcm-v1",
  proof_scheme: "sha256-v1",
  export_sha256: exportSha256,
};

function row(overrides: Record<string, unknown> = {}) {
  const base = {
    uid: "phone-user",
    source_record_id: "phone-1",
    phone_number_id: "phone-1",
    phone_number_hash: hash,
    phone_number_ciphertext: ciphertext,
    twilio_sid: "PN1234567890",
    friendly_name: "home",
    verified_at: 1_700_000_000,
    is_primary: true,
    account_generation: 3,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_001,
    status: "verified",
    ...overrides,
  };
  const sourceFingerprint = sha256(
    stableJson({
      collection: source.collection,
      export_sha256: source.export_sha256,
      uid: base.uid,
      source_record_id: base.source_record_id,
      phone_number_hash: base.phone_number_hash,
      twilio_sid: base.twilio_sid,
      friendly_name: base.friendly_name,
      verified_at: base.verified_at,
      is_primary: 1,
      account_generation: base.account_generation,
      created_at: base.created_at,
      updated_at: base.updated_at,
    }),
  );
  const proofSha256 = sha256(
    stableJson({
      kind: "verified-e164",
      method: "twilio-outgoing-caller-id",
      canonicalization: "E.164",
      verified: true,
      value_sha256: base.phone_number_hash,
      source_fingerprint: sourceFingerprint,
      attested_at: 1_700_000_002,
    }),
  );
  const result: Record<string, unknown> = {
    ...base,
    source_fingerprint: sourceFingerprint,
    proof: {
      kind: "verified-e164",
      method: "twilio-outgoing-caller-id",
      canonicalization: "E.164",
      verified: true,
      value_sha256: base.phone_number_hash,
      source_fingerprint: sourceFingerprint,
      proof_sha256: proofSha256,
      attested_at: 1_700_000_002,
    },
  };
  if (Object.prototype.hasOwnProperty.call(overrides, "proof")) result.proof = overrides.proof;
  return result;
}

function manifest(rows: Array<Record<string, unknown>> = [row()]) {
  return { schema_version: 1, source, rows };
}

function sqliteWithMigrations() {
  const database = new DatabaseSync(":memory:");
  const directory = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../migrations/app",
  );
  for (const filename of readdirSync(directory)
    .filter((value) => value.endsWith(".sql"))
    .sort()) {
    database.exec(readFileSync(path.join(directory, filename), "utf8"));
  }
  return database;
}

describe("Phone historical reconciliation planner", () => {
  it("plans only an attested encrypted row and emits fenced idempotent ledger SQL", () => {
    const plan = planPhoneHistory(manifest());
    expect(plan).toMatchObject({
      mode: "dry-run",
      schema_version: 1,
      total: 1,
      stage: 1,
      blocked: 0,
      source: { export_sha256: exportSha256 },
    });
    expect(plan.manifest_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(plan.entries[0]).toMatchObject({
      uid: "phone-user",
      action: "stage",
      status: "planned",
      phoneNumberHash: hash,
      phoneNumberCiphertext: ciphertext,
      accountGeneration: 3,
      proofSha256: expect.stringMatching(/^[0-9a-f]{64}$/),
    });
    const sql = renderPhoneHistoryLedgerSql(plan, 1_700_000_010);
    expect(sql).toContain("cf_phone_number_import_ledger");
    expect(sql).toContain("ON CONFLICT(uid, import_id) DO UPDATE SET");
    expect(sql).toContain("cf_account_deletion_intents");
    expect(sql).toContain("cf_account_deletion_tombstones");
    expect(sql).toContain("destination_backend_bound = 1");
    expect(sql).not.toMatch(/\b(?:BEGIN|COMMIT|SAVEPOINT)\b[^\n]*;/);
    expect(sql).not.toContain("+");
    expect(renderPhoneHistoryVerifySql(plan)).toContain("source_fingerprint");
  });

  it("requires proof and rejects plaintext, pending rows, and legacy ciphertext", () => {
    expect(() => planPhoneHistory(manifest([row({ phone_number: "+15551234567" })]))).toThrow(
      "plaintext E.164",
    );
    expect(() => planPhoneHistory(manifest([row({ raw: "+1 (555) 123-4567" })]))).toThrow(
      "plaintext E.164",
    );

    const incomplete = planPhoneHistory(
      manifest([
        row({ proof: undefined }),
        row({ source_record_id: "pending", phone_number_id: "pending", status: "pending" }),
        row({ source_record_id: "legacy", phone_number_id: "legacy", phone_number_ciphertext: Buffer.alloc(40).toString("base64") }),
      ]),
    );
    expect(incomplete).toMatchObject({ total: 3, stage: 0, blocked: 3 });
    expect(incomplete.entries.map((entry) => [entry.sourceRecordId, entry.lastError])).toEqual([
      ["legacy", "ciphertext_missing_or_invalid"],
      ["pending", "status_not_verified"],
      ["phone-1", "proof_missing"],
    ]);
    expect(renderPhoneHistoryLedgerSql(incomplete)).not.toContain(
      "INSERT INTO cf_phone_number_import_ledger",
    );
  });

  it("deduplicates identical rows and blocks hash, SID, and source collisions", () => {
    const same = planPhoneHistory(manifest([row(), row()]));
    expect(same).toMatchObject({ total: 1, stage: 1, blocked: 0 });

    const hashConflict = planPhoneHistory(
      manifest([
        row(),
        row({ source_record_id: "phone-2", phone_number_id: "phone-2" }),
      ]),
    );
    expect(hashConflict).toMatchObject({ total: 2, stage: 0, blocked: 2 });
    expect(hashConflict.entries.every((entry) => entry.lastError?.includes("conflicting_phone_hash_claim"))).toBe(true);

    const sourceConflict = planPhoneHistory(
      manifest([row(), row({ phone_number_hash: "c".repeat(64) })]),
    );
    expect(sourceConflict).toMatchObject({ total: 2, stage: 0, blocked: 2 });
    expect(sourceConflict.entries.every((entry) => entry.lastError?.includes("conflicting_duplicate_row"))).toBe(true);

    const sidConflict = planPhoneHistory(
      manifest([
        row(),
        row({ source_record_id: "phone-2", phone_number_id: "phone-2", phone_number_hash: "c".repeat(64) }),
      ]),
    );
    expect(sidConflict).toMatchObject({ total: 2, stage: 0, blocked: 2 });
    expect(sidConflict.entries.every((entry) => entry.lastError?.includes("conflicting_twilio_sid_claim"))).toBe(true);
  });

  it("keeps deletion-fenced rows out of D1 and verifies an applied ledger row", () => {
    const plan = planPhoneHistory(manifest());
    const database = sqliteWithMigrations();
    try {
      database.exec(
        "INSERT INTO cf_account_cutover (uid, state, checkpoint_phase, destination_backend_bound, account_generation, updated_at) VALUES ('phone-user', 'new', 'completed', 1, 3, 1700000000)",
      );
      database.exec(renderPhoneHistoryLedgerSql(plan, 1_700_000_010));
      database.exec(renderPhoneHistoryLedgerSql(plan, 1_700_000_010));
      const actual = database
        .prepare("SELECT * FROM cf_phone_number_import_ledger WHERE uid = 'phone-user'")
        .all();
      expect(actual).toHaveLength(1);
      expect(verifyPhoneHistory(plan, actual)).toMatchObject({
        status: "passed",
        checked: 1,
        missing: [],
        mismatched: [],
      });

      database.exec(
        "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES ('fenced-user', 'delete-phone', 'pending', 'quiescing', 1, 1, 1)",
      );
      const fencedPlan = planPhoneHistory(manifest([row({ uid: "fenced-user" })]), { fencedUids: ["fenced-user"] });
      expect(fencedPlan.entries[0]).toMatchObject({ action: "blocked", lastError: "account_deletion_fence" });
      expect(renderPhoneHistoryLedgerSql(fencedPlan)).not.toContain("INSERT INTO cf_phone_number_import_ledger");

      const unplannedFenced = planPhoneHistory(manifest([row({ uid: "phone-user", source_record_id: "phone-2", phone_number_id: "phone-2", phone_number_hash: "c".repeat(64) })]));
      database.exec(
        "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES ('phone-user', 'delete-phone-user', 'pending', 'quiescing', 1, 1, 1)",
      );
      database.exec(renderPhoneHistoryLedgerSql(unplannedFenced, 1_700_000_010));
      expect(
        database.prepare("SELECT COUNT(*) AS count FROM cf_phone_number_import_ledger WHERE uid = 'phone-user'").get(),
      ).toMatchObject({ count: 1 });
    } finally {
      database.close();
    }
  });
});
