import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { planMemoryArchiveReconciliation } from "../scripts/memory-archive-reconcile.mjs";

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(stableJson).join(",") + "]";
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return "{" + Object.keys(object).sort().map((key) => JSON.stringify(key) + ":" + stableJson(object[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function fixture() {
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collection: "users/{uid}/memories",
      export_sha256: "b".repeat(64),
      exported_at: "2026-09-01T00:00:00Z",
    },
    accounts: [{ uid: "archive-user", account_generation: 2, source_fingerprint: "a".repeat(64) }],
    memories: [{
      uid: "archive-user",
      memory_id: "memory-1",
      memory_tier: "archive",
      content: "A reviewed archive memory",
      version: 1,
      status: "active",
      processing_state: "processed",
      source_state: "active",
      sensitivity_labels: [],
      visibility: "private",
      user_asserted: 0,
      captured_at: 1700000000,
      updated_at: 1700000000,
      expires_at: null,
      ledger_commit_id: null,
      ledger_sequence: null,
      item_revision: 1,
      source_id: "firestore-memory-1",
      evidence: [],
      confidence: null,
      superseded_by: null,
      is_locked: 0,
      account_generation: 2,
      created_at: 1700000000,
      deleted_at: null,
    }],
  };
}

describe("memory archive dry-run reconciliation", () => {
  it("emits a worker-compatible deterministic apply request", () => {
    const plan = planMemoryArchiveReconciliation(fixture());
    expect(plan).toMatchObject({ mode: "dry-run", total: 1, staged: 1, blocked: 0 });
    expect(plan.apply_request.entries).toHaveLength(1);
    expect(plan.apply_request.entries[0]).toMatchObject({ action: "stage", status: "planned", last_error: null });
    const entry = plan.apply_request.entries[0];
    const expectedManifest = sha256(stableJson({
      schema_version: 1,
      source: plan.source,
      entries: [entry.source_row_sha256],
    }));
    expect(plan.manifest_sha256).toBe(expectedManifest);
    expect(plan.apply_request.manifest_sha256).toBe(plan.manifest_sha256);
  });

  it("reports deletion-fenced accounts as blocked and never stages them", () => {
    const plan = planMemoryArchiveReconciliation(fixture(), { fencedUids: ["archive-user"] });
    expect(plan).toMatchObject({ total: 1, staged: 0, blocked: 1 });
    expect(plan.entries[0]).toMatchObject({ action: "blocked", last_error: "account_deletion_fence" });
    expect(plan.apply_request.entries).toHaveLength(0);
  });

  it("rejects sensitive export fields and restricted sensitivity labels", () => {
    const sensitive = fixture();
    (sensitive.memories[0] as Record<string, unknown>).notes = { email: "redacted@example.com" };
    expect(() => planMemoryArchiveReconciliation(sensitive)).toThrow(/unsupported fields|sensitive/);

    const restricted = fixture();
    (restricted.memories[0] as unknown as { sensitivity_labels: string[] }).sensitivity_labels = ["health"];
    expect(() => planMemoryArchiveReconciliation(restricted)).toThrow(/restricted sensitivity/);
  });
});
