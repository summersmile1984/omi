import { describe, expect, it } from "vitest";
import {
  planChatFileReconciliation,
  renderChatFileLedgerSql,
  renderChatFileR2Plan,
} from "../scripts/chat-file-reconcile.mjs";

const checksum = "a".repeat(64);

function legacyFile(overrides: Record<string, unknown> = {}) {
  return {
    user_id: "user-1",
    id: "legacy-file-1",
    name: "notes.txt",
    mime_type: "text/plain",
    size: 4,
    checksum_sha256: checksum,
    openai_file_id: "file-provider-1",
    gcs_uri: "gs://legacy-chat-files/user-1/notes.txt",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_001,
    ...overrides,
  };
}

describe("chat-file historical reconciliation planner", () => {
  it("builds an idempotent D1 ledger and private R2 copy plan", () => {
    const plan = planChatFileReconciliation(
      [legacyFile(), legacyFile()],
      { now: 1_700_000_010 },
    );
    expect(plan).toMatchObject({ mode: "dry-run", total: 1, stage: 1, blocked: 0 });
    expect(plan.entries[0]).toMatchObject({
      uid: "user-1",
      sourceFileId: "legacy-file-1",
      providerFileId: "file-provider-1",
      storageKey: "user-1/legacy-file-1",
      action: "stage",
      status: "planned",
      checksum,
    });
    expect(plan.entries[0].importId).toMatch(/^[0-9a-f]{64}$/);
    const sql = renderChatFileLedgerSql(plan, 1_700_000_011);
    expect(sql).toContain("cf_chat_file_import_ledger");
    expect(sql).toContain("ON CONFLICT(uid, import_id) DO UPDATE SET");
    expect(sql).toContain("file-provider-1");
    expect(sql).not.toContain("authorization");
    expect(renderChatFileR2Plan(plan)).toEqual([
      {
        source_object_uri: "gs://legacy-chat-files/user-1/notes.txt",
        destination_key: "user-1/legacy-file-1",
        checksum_sha256: checksum,
        size: 4,
        provider_file_id: "file-provider-1",
        status: "not_started",
      },
    ]);
  });

  it("blocks missing provider/checksum metadata and fenced accounts", () => {
    const incomplete = planChatFileReconciliation([
      legacyFile({ checksum_sha256: undefined, openai_file_id: undefined }),
    ]);
    expect(incomplete).toMatchObject({ total: 1, stage: 0, blocked: 1 });
    expect(incomplete.entries[0]).toMatchObject({
      action: "blocked",
      status: "blocked",
      lastError: "checksum_missing,provider_id_missing",
    });
    expect(renderChatFileLedgerSql(incomplete)).toContain("'blocked'");
    expect(renderChatFileR2Plan(incomplete)).toEqual([]);

    const fenced = planChatFileReconciliation([legacyFile()], {
      fencedUids: ["user-1"],
    });
    expect(fenced.entries[0]).toMatchObject({
      action: "blocked",
      status: "blocked",
      lastError: "account_deletion_fence",
      providerFileId: null,
    });
    expect(renderChatFileLedgerSql(fenced)).toContain("'account_deletion_fence'");
  });

  it("rejects unsafe source URIs and bounded input", () => {
    expect(() =>
      planChatFileReconciliation([legacyFile({ gcs_uri: "https://evil.example/file" })]),
    ).toThrow("credential-free gs:// URI");
    expect(() =>
      planChatFileReconciliation([legacyFile(), legacyFile({ id: "legacy-file-2" })], { maxRows: 1 }),
    ).toThrow("maximum 1 rows");
    const oversized = planChatFileReconciliation([
      legacyFile({ size: 50 * 1024 * 1024 + 1 }),
    ]);
    expect(oversized).toMatchObject({
      total: 1,
      stage: 0,
      blocked: 1,
      entries: expect.anything(),
    });
    expect(renderChatFileLedgerSql(oversized)).toContain("size_invalid");
  });

  it("blocks provider and destination collisions before SQL execution", () => {
    const plan = planChatFileReconciliation([
      legacyFile(),
      legacyFile({
        id: "legacy-file-2",
        checksum_sha256: "b".repeat(64),
      }),
      legacyFile({
        user_id: "user-2",
      }),
    ]);
    expect(plan).toMatchObject({ total: 3, stage: 0, blocked: 3 });
    expect(plan.entries.every((entry) => entry.action === "blocked")).toBe(true);
    expect(plan.entries.map((entry) => entry.lastError)).toEqual([
      "conflicting_provider_claim",
      "conflicting_provider_claim",
      "conflicting_provider_claim",
    ]);
    expect(renderChatFileR2Plan(plan)).toEqual([]);
  });
});
