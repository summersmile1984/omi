import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import {
  buildAppOwnerDataAttestation,
  parseAppOwnerAttestationJson,
  renderAppOwnerDataAttestationSql,
} from "../scripts/app-owner-attestation.mjs";
import { planChatHistoryReconciliation } from "../scripts/chat-history-reconcile.mjs";
import { planPersonaAppHistory } from "../scripts/persona-app-history-reconcile.mjs";

const SOURCE_UID_HASH = "a".repeat(64);
const SOURCE_UID = `fb-anon-${SOURCE_UID_HASH}`;
const SOURCE_PROOF_HASH = "b".repeat(64);
const SOURCE_PROJECTION_REVISION = "c".repeat(64);
const PERSONA_EXPORT_HASH = "d".repeat(64);
const CHAT_EXPORT_HASH = "e".repeat(64);
const SOURCE_FINGERPRINT = "f".repeat(64);

function personaManifest(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collection: "plugins_data",
      export_sha256: PERSONA_EXPORT_HASH,
    },
    rows: [
      {
        source_uid: SOURCE_UID,
        uid: "target-user",
        app_id: "persona-1",
        source_projection_revision: SOURCE_PROJECTION_REVISION,
        source_fingerprint: "1".repeat(64),
        target_account_generation: 7,
        target_cutover: {
          state: "new",
          checkpoint_phase: "completed",
          destination_backend_bound: true,
          deletion_fenced: false,
        },
        public_metadata: {
          id: "persona-1",
          name: "Public persona",
          description: "Reviewable metadata",
        },
        created_at: 1,
        updated_at: 2,
        ...overrides,
      },
    ],
  };
}

function chatManifest(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collections: ["users/{uid}/chat_sessions", "users/{uid}/messages"],
      export_sha256: CHAT_EXPORT_HASH,
    },
    accounts: [
      {
        uid: "target-user",
        account_generation: 7,
        source_fingerprint: SOURCE_FINGERPRINT,
      },
    ],
    sessions: [
      {
        uid: "target-user",
        id: "session-1",
        title: "Imported session",
        preview: "hello",
        app_id: null,
        message_count: 1,
        starred: false,
        created_at: 1,
        updated_at: 2,
      },
    ],
    messages: [
      {
        uid: "target-user",
        id: "message-1",
        app_id: null,
        created_at: 2,
        message_json: {
          id: "message-1",
          text: "hello",
          sender: "human",
          type: "text",
          chat_session_id: "session-1",
        },
      },
    ],
    ...overrides,
  };
}

type BuildOptions = Parameters<typeof buildAppOwnerDataAttestation>[0];

function options(overrides: Partial<BuildOptions> = {}): BuildOptions {
  return {
    persona: personaManifest(),
    chat: chatManifest(),
    sourceUid: SOURCE_UID,
    sourceProofHash: SOURCE_PROOF_HASH,
    sourceProjectionRevision: SOURCE_PROJECTION_REVISION,
    memoryProjectionCount: 0,
    memoryReencryptionStatus: "not_required",
    ...overrides,
  };
}

describe("offline app-owner attestation operator", () => {
  it("aggregates planner manifests into review JSON and read-only SQL", () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValue(new Error("offline operator must not fetch"));
    try {
      const review = buildAppOwnerDataAttestation(options());
      expect(review).toMatchObject({
        schema_version: 1,
        kind: "app_owner_data_attestation_review",
        status: "ready_for_review",
        attestation: {
          source_uid: SOURCE_UID,
          source_uid_hash: SOURCE_UID_HASH,
          source_proof_hash: SOURCE_PROOF_HASH,
          source_projection_revision: SOURCE_PROJECTION_REVISION,
          target_uid: "target-user",
          target_account_generation: 7,
          app_projection_count: 1,
          memory_projection_count: 0,
          memory_reencryption_status: "not_required",
          memory_reencryption_revision: null,
        },
        safety: {
          firestore_connected: false,
          d1_connected: false,
          admin_endpoint_called: false,
          memory_reencryption_performed: false,
        },
      });
      expect(review.attestation.data_projection_revision).toMatch(
        /^[0-9a-f]{64}$/,
      );
      expect(review.evidence.persona).toMatchObject({
        total: 1,
        staged: 1,
        blocked: 0,
        sourceExportSha256: PERSONA_EXPORT_HASH,
      });
      expect(review.evidence.chat).toMatchObject({
        total: 2,
        staged: 2,
        blocked: 0,
        sourceExportSha256: CHAT_EXPORT_HASH,
      });
      // The review artifact contains planner digests, not public message or
      // persona payloads, and no source secret/token.
      expect(JSON.stringify(review)).not.toContain("Public persona");
      expect(JSON.stringify(review)).not.toContain("hello");

      const sql = renderAppOwnerDataAttestationSql(review);
      expect(sql).toContain("offline_only_no_write");
      expect(sql).toContain("memory_constraint_ok");
      const executableLines = sql
        .split("\n")
        .filter((line) => !line.trim().startsWith("--"))
        .join("\n");
      expect(executableLines).not.toMatch(/\b(?:INSERT|UPDATE|DELETE)\b/i);
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      fetch.mockRestore();
    }
  });

  it("rejects incomplete or conflicting planner evidence before producing attestation", () => {
    expect(() =>
      buildAppOwnerDataAttestation(
        options({
          persona: personaManifest({ persona_prompt: "must not be copied" }),
        }),
      ),
    ).toThrow("persona plan is incomplete");
    expect(() =>
      buildAppOwnerDataAttestation(
        options({ sourceUid: `fb-anon-${"9".repeat(64)}` }),
      ),
    ).toThrow("sourceUid does not match persona planner outputs");
    expect(() =>
      buildAppOwnerDataAttestation(
        options({
          chat: chatManifest({
            accounts: [
              {
                uid: "other-user",
                account_generation: 7,
                source_fingerprint: SOURCE_FINGERPRINT,
              },
            ],
          }),
        }),
      ),
    ).toThrow("uid is not listed in accounts");
  });

  it("rechecks planner output integrity instead of trusting mutable row metadata", () => {
    const personaPlan = planPersonaAppHistory(personaManifest()) as Record<
      string,
      any
    >;
    personaPlan.entries[0].sourceExportSha256 = "9".repeat(64);
    expect(() =>
      buildAppOwnerDataAttestation(options({ persona: personaPlan })),
    ).toThrow("source export does not match plan");

    const chatPlan = planChatHistoryReconciliation(chatManifest()) as Record<
      string,
      any
    >;
    chatPlan.entries[0].fileIds = ["unverified-file"];
    expect(() =>
      buildAppOwnerDataAttestation(options({ chat: chatPlan })),
    ).toThrow("unverified file references");
  });

  it("enforces explicit memory evidence without fabricating re-encryption", () => {
    expect(() =>
      buildAppOwnerDataAttestation(
        options({
          memoryProjectionCount: 0,
          memoryReencryptionStatus: "completed",
          memoryReencryptionRevision: "1".repeat(64),
        }),
      ),
    ).toThrow("zero memory rows require");
    expect(() =>
      buildAppOwnerDataAttestation(
        options({
          memoryProjectionCount: 2,
          memoryReencryptionStatus: "not_required",
        }),
      ),
    ).toThrow("non-zero memory rows require");
    expect(() =>
      buildAppOwnerDataAttestation(
        options({
          memoryProjectionCount: 2,
          memoryReencryptionStatus: "completed",
        }),
      ),
    ).toThrow("memoryReencryptionRevision");

    const review = buildAppOwnerDataAttestation(
      options({
        memoryProjectionCount: 2,
        memoryReencryptionStatus: "completed",
        memoryReencryptionRevision: "2".repeat(64),
      }),
    );
    expect(review.attestation).toMatchObject({
      memory_projection_count: 2,
      memory_reencryption_status: "completed",
      memory_reencryption_revision: "2".repeat(64),
    });
    expect(review.safety.memory_reencryption_performed).toBe(false);
  });

  it("keeps the implementation offline and free of D1/Firestore clients", () => {
    const source = readFileSync(
      new URL("../scripts/app-owner-attestation.mjs", import.meta.url),
      "utf8",
    );
    expect(source).not.toContain("node:sqlite");
    expect(source).not.toMatch(/\bfetch\s*\(/);
    expect(source).not.toContain("fetch(");
    expect(source).toContain("admin_endpoint_called: false");
  });

  it("rejects malformed UTF-8 input instead of replacing bytes", () => {
    expect(() =>
      parseAppOwnerAttestationJson(
        new Uint8Array([0x7b, 0x22, 0x78, 0x22, 0x3a, 0xc3, 0x28, 0x7d]),
        "manifest",
      ),
    ).toThrow("manifest is not valid UTF-8 JSON");
  });
});
