import { describe, expect, it } from "vitest";
import {
  planAudioReconciliation,
  renderAudioLedgerSql,
  renderAudioR2Plan,
} from "../scripts/audio-reconcile.mjs";

const checksum = "a".repeat(64);

function chunk(overrides: Record<string, unknown> = {}) {
  return {
    uid: "audio-user",
    conversation_id: "conversation-1",
    source_object_uri:
      "gs://legacy-private-sync/chunks/audio-user/conversation-1/1000.000.batch.bin",
    source_generation: "1700000000000000",
    checksum_sha256: checksum,
    size: 32_000,
    ...overrides,
  };
}

describe("historical audio GCS reconciliation planner", () => {
  it("plans an immutable R2 copy and deduplicates an identical export row", () => {
    const plan = planAudioReconciliation([chunk(), chunk()], { now: 1_700_000_010 });
    expect(plan).toMatchObject({ mode: "dry-run", total: 1, stage: 1, blocked: 0 });
    expect(plan.entries[0]).toMatchObject({
      uid: "audio-user",
      conversationId: "conversation-1",
      sourceObjectName: "chunks/audio-user/conversation-1/1000.000.batch.bin",
      destinationKey: "chunks/audio-user/conversation-1/1000.000.batch.bin",
      sourceGeneration: "1700000000000000",
      checksum,
      sourceKind: "pcm",
      encrypted: false,
      batch: true,
      startTimestamp: 1000,
      endTimestamp: 1000,
      action: "stage",
      status: "planned",
    });
    const copyPlan = renderAudioR2Plan(plan);
    expect(copyPlan).toEqual([
      expect.objectContaining({
        source_object_uri:
          "gs://legacy-private-sync/chunks/audio-user/conversation-1/1000.000.batch.bin",
        source_generation: "1700000000000000",
        destination_key: "chunks/audio-user/conversation-1/1000.000.batch.bin",
        checksum_sha256: checksum,
        if_generation_match: "1700000000000000",
        status: "not_started",
      }),
    ]);
    const sql = renderAudioLedgerSql(plan, 1_700_000_011);
    expect(sql).toContain("cf_audio_chunk_import_ledger");
    expect(sql).toContain("ON CONFLICT(uid, import_id) DO UPDATE SET");
    expect(sql).toContain("1700000000000000");
    expect(sql).not.toContain("authorization");
  });

  it("blocks rows without immutable generation/checksum and unsupported codecs", () => {
    const plan = planAudioReconciliation([
      chunk({ source_generation: undefined, checksum_sha256: undefined }),
      chunk({
        source_object_uri:
          "gs://legacy-private-sync/chunks/audio-user/conversation-1/1000.000.flac",
      }),
    ]);
    expect(plan).toMatchObject({ total: 2, stage: 0, blocked: 2 });
    expect(plan.entries.map((entry) => entry.lastError)).toEqual([
      "checksum_missing,source_generation_missing",
      "unsupported_chunk_name",
    ]);
    expect(renderAudioR2Plan(plan)).toEqual([]);
  });

  it("rejects unsafe URI identity and blocks fenced accounts", () => {
    expect(() =>
      planAudioReconciliation([
        chunk({
          source_object_uri:
            "gs://legacy-private-sync/chunks/other-user/conversation-1/1000.000.bin",
        }),
      ]),
    ).toThrow("identity does not match");

    const fenced = planAudioReconciliation([chunk()], {
      fencedUids: ["audio-user"],
    });
    expect(fenced.entries[0]).toMatchObject({
      action: "blocked",
      status: "blocked",
      lastError: "account_deletion_fence",
    });
    expect(renderAudioLedgerSql(fenced)).toContain("account_deletion_fence");
  });

  it("blocks destination collisions across source buckets", () => {
    const plan = planAudioReconciliation([
      chunk(),
      chunk({
        source_object_uri:
          "gs://another-private-sync/chunks/audio-user/conversation-1/1000.000.batch.bin",
        checksum_sha256: "b".repeat(64),
        source_generation: "1700000000000001",
      }),
    ]);
    expect(plan).toMatchObject({ total: 2, stage: 0, blocked: 2 });
    expect(plan.entries.every((entry) => entry.lastError === "conflicting_destination_claim")).toBe(true);
    expect(renderAudioR2Plan(plan)).toEqual([]);
  });

  it("accepts encrypted Opus and ranged batch names while enforcing size bounds", () => {
    const plan = planAudioReconciliation([
      chunk({
        source_object_uri:
          "gs://legacy-private-sync/chunks/audio-user/conversation-1/1000.000-1010.500.batch.enc",
        size: 100,
      }),
      chunk({
        source_object_uri:
          "gs://legacy-private-sync/chunks/audio-user/conversation-1/1000.000.opus.enc",
        size: 16 * 1024 * 1024 + 1,
      }),
    ]);
    expect(plan.entries[0]).toMatchObject({
      sourceKind: "pcm",
      encrypted: true,
      batch: true,
      startTimestamp: 1000,
      endTimestamp: 1010.5,
      action: "stage",
    });
    expect(plan.entries[1]).toMatchObject({
      sourceKind: "opus",
      encrypted: true,
      batch: false,
      action: "blocked",
      lastError: "opus_size_invalid",
    });
  });
});
