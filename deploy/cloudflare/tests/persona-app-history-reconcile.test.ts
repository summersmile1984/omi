import { describe, expect, it } from "vitest";
import {
  planPersonaAppHistory,
  renderPersonaAppHistoryOperations,
  verifyPersonaAppHistory,
} from "../scripts/persona-app-history-reconcile.mjs";

const sourceHash = "a".repeat(64);
const exportHash = "b".repeat(64);
const sourceRevision = "c".repeat(64);
const sourceFingerprint = "d".repeat(64);
const imageChecksum = "e".repeat(64);
const encryptedEnvelope =
  "v1.AAAAAAAAAAAAAAAA.AAAAAAAAAAAAAAAAAAAAAAAA";

function row(overrides: Record<string, unknown> = {}) {
  return {
    source_uid: `fb-anon-${sourceHash}`,
    uid: "target-user",
    app_id: "persona-1",
    source_projection_revision: sourceRevision,
    source_fingerprint: sourceFingerprint,
    target_account_generation: 7,
    target_cutover: {
      state: "new",
      checkpoint_phase: "completed",
      destination_backend_bound: true,
      deletion_fenced: false,
    },
    public_metadata: {
      id: "persona-1",
      name: "A public persona",
      description: "Bounded public metadata",
      capabilities: ["persona"],
      private: false,
    },
    private_envelope: encryptedEnvelope,
    image_object: {
      source_object_uri: "gs://legacy-bucket/personas/persona-1.png",
      source_generation: "1710000000000000",
      checksum_sha256: imageChecksum,
      size: 128,
      content_type: "image/png",
    },
    created_at: 1,
    updated_at: 2,
    ...overrides,
  };
}

function manifest(rows: unknown[] = [row()]) {
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collection: "plugins_data",
      export_sha256: exportHash,
    },
    rows,
  };
}

describe("Persona/App historical replay planner", () => {
  it("plans public D1 metadata, opaque private envelope, and guarded R2 copy without writing", () => {
    const plan = planPersonaAppHistory(manifest());
    expect(plan).toMatchObject({
      mode: "dry-run",
      schema_version: 1,
      total: 1,
      stage: 1,
      blocked: 0,
      source: { export_sha256: exportHash },
    });
    const entry = plan.entries[0];
    expect(entry).toMatchObject({
      sourceRef: `fb-anon-${sourceHash}`,
      sourceUidHash: sourceHash,
      uid: "target-user",
      appId: "persona-1",
      targetAccountGeneration: 7,
      action: "stage",
      privateEnvelope: {
        format: "v1.compact-aes-gcm",
        sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
      },
      imageObject: {
        sourceObjectUri: "gs://legacy-bucket/personas/persona-1.png",
        sourceGeneration: "1710000000000000",
        destinationKey: "cf-app-logos/target-user/persona-1/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      },
      sourceRowSha256: expect.stringMatching(/^[0-9a-f]{64}$/),
      requestFingerprint: expect.stringMatching(/^[0-9a-f]{64}$/),
    });
    expect(JSON.stringify(plan)).not.toContain(encryptedEnvelope);
    const operations = renderPersonaAppHistoryOperations(plan);
    expect(operations).toHaveLength(1);
    expect(operations[0]).toMatchObject({
      d1: {
        operation: "insert_public_catalog_if_absent",
        table: "cf_app_catalog",
        owner_account_generation: 7,
      },
      private: { operation: "store_encrypted_envelope" },
      r2: { operation: "copy_after_generation_check" },
      guards: {
        source_export_sha256: exportHash,
        account_generation: 7,
        deletion_fence: "must_be_clear_at_apply_time",
      },
    });
    // The operation preview is deliberately descriptive, not executable SQL.
    expect(JSON.stringify(operations)).not.toMatch(/\b(?:INSERT|UPDATE|DELETE)\s+INTO\b/i);
  });

  it("rejects plaintext private fields and refuses raw source identities", () => {
    const plan = planPersonaAppHistory(
      manifest([
        row({ persona_prompt: "must not be copied" }),
        row({ source_uid: "raw-firebase-uid", app_id: "persona-raw" }),
      ]),
    );
    expect(plan).toMatchObject({ total: 2, stage: 0, blocked: 2 });
    expect(plan.entries[0].lastError).toContain(
      "plaintext_private_field:persona_prompt",
    );
    expect(plan.entries[1]).toMatchObject({
      sourceRef: "",
      sourceUidHash: null,
      action: "blocked",
    });
    expect(plan.entries[1].lastError).toContain("source_uid_not_opaque");
    expect(renderPersonaAppHistoryOperations(plan)).toEqual([]);
  });

  it("blocks missing export/generation/fence attestations and unsafe image sources", () => {
    expect(() =>
      planPersonaAppHistory({
        ...manifest(),
        source: { kind: "firestore", collection: "plugins_data" },
      }),
    ).toThrow("source.export_sha256");

    const plan = planPersonaAppHistory(
      manifest([
        row({
          target_account_generation: undefined,
          target_cutover: {
            state: "migrating",
            checkpoint_phase: "completed",
            destination_backend_bound: true,
            deletion_fenced: false,
          },
          image_object: {
            source_object_uri: "https://evil.example/logo.png",
            source_generation: "1",
            checksum_sha256: imageChecksum,
            size: 128,
            content_type: "image/png",
          },
        }),
      ]),
    );
    expect(plan).toMatchObject({ stage: 0, blocked: 1 });
    expect(plan.entries[0].lastError).toContain("target_cutover_not_new");
    expect(plan.entries[0].lastError).toContain("image_object_source_uri_invalid");
  });

  it("deduplicates identical rows but blocks target/source conflicts and fenced accounts", () => {
    const same = row();
    const conflict = row({ public_metadata: { ...row().public_metadata, name: "Changed" } });
    const otherTarget = row({ uid: "other-user" });
    const plan = planPersonaAppHistory(
      manifest([same, { ...same }, conflict, otherTarget]),
      { fencedUids: ["other-user"] },
    );
    // The identical duplicate is collapsed into its target-app claim; the
    // conflicting copy blocks that canonical claim instead of producing a
    // second operation.
    expect(plan.total).toBe(2);
    expect(plan.stage).toBe(0);
    expect(plan.blocked).toBe(2);
    expect(plan.entries.filter((entry) => entry.lastError?.includes("conflicting_duplicate_row"))).toHaveLength(1);
    expect(plan.entries.find((entry) => entry.uid === "other-user")?.lastError).toContain("account_deletion_fence");
  });

  it("verifies a reviewed plan against a D1 export and catches duplicates or drift", () => {
    const plan = planPersonaAppHistory(manifest());
    const entry = plan.entries[0];
    const actual = [
      {
        id: entry.appId,
        owner_uid: entry.uid,
        owner_account_generation: entry.targetAccountGeneration,
        // Different key order is harmless; semantic JSON is canonicalized.
        data_json: JSON.stringify({
          private: false,
          capabilities: ["persona"],
          description: "Bounded public metadata",
          name: "A public persona",
          id: "persona-1",
        }),
      },
    ];
    expect(verifyPersonaAppHistory(plan, actual)).toMatchObject({
      status: "passed",
      checked: 1,
      missing: [],
      mismatched: [],
      duplicate_actual: [],
    });
    expect(
      verifyPersonaAppHistory(plan, [...actual, actual[0]]),
    ).toMatchObject({ status: "failed", duplicate_actual: ["target-user\0persona-1"] });
    expect(
      verifyPersonaAppHistory(plan, [{ ...actual[0], owner_account_generation: 8 }]),
    ).toMatchObject({ status: "failed", mismatched: [{ reasons: ["account_generation"] }] });
  });
});
