import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import {
  applyChatHistoryPlan,
  signChatHistoryPlan,
  verifyChatHistoryExport,
} from "../scripts/chat-history-export-verify.mjs";

const SIGNING_SECRET = "chat-history-export-signing-secret-0123456789";

function exportObject() {
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collections: [
        "users/{uid}/chat_sessions",
        "users/{uid}/messages",
      ],
      exported_at: "2026-09-01T00:00:00Z",
    },
    accounts: [
      {
        uid: "export-user",
        account_generation: 3,
        source_fingerprint: "a".repeat(64),
      },
    ],
    sessions: [
      {
        uid: "export-user",
        id: "session-1",
        title: "Imported session",
        preview: "hello",
        app_id: null,
        message_count: 1,
        starred: false,
        created_at: 1_700_000_000,
        updated_at: 1_700_000_001,
      },
    ],
    messages: [
      {
        uid: "export-user",
        id: "message-1",
        app_id: null,
        created_at: 1_700_000_001,
        message_json: {
          id: "message-1",
          text: "hello",
          sender: "human",
          type: "text",
          chat_session_id: "session-1",
        },
      },
    ],
  };
}

function exportBytes() {
  return new TextEncoder().encode(JSON.stringify(exportObject()));
}

function checksum(bytes: Uint8Array) {
  return createHash("sha256").update(bytes).digest("hex");
}

describe("chat history export verification tool", () => {
  it("hashes the original bytes and reuses the canonical planner", () => {
    const bytes = exportBytes();
    const result = verifyChatHistoryExport(bytes, {
      expectedSha256: checksum(bytes),
    });
    expect(result).toMatchObject({
      verified: true,
      export_sha256: checksum(bytes),
      export_bytes: bytes.byteLength,
    });
    expect(result.plan).toMatchObject({
      mode: "reviewed-plan",
      total: 2,
      stage: 2,
      blocked: 0,
      source: { export_sha256: checksum(bytes) },
    });
  });

  it("stays dry-run without an expected checksum and rejects malformed exports", () => {
    const bytes = exportBytes();
    expect(verifyChatHistoryExport(bytes).verified).toBe(false);
    expect(() =>
      verifyChatHistoryExport(bytes, { expectedSha256: "0".repeat(64) }),
    ).toThrow("does not match");
    expect(() =>
      verifyChatHistoryExport(new Uint8Array([0xff, 0xfe])),
    ).toThrow("UTF-8 JSON");
    const sourceHash = {
      ...exportObject(),
      source: {
        ...exportObject().source,
        export_sha256: "0".repeat(64),
      },
    };
    const embeddedHashBytes = new TextEncoder().encode(
      JSON.stringify(sourceHash),
    );
    expect(() => verifyChatHistoryExport(embeddedHashBytes)).toThrow(
      "source.export_sha256",
    );
  });

  it("creates the endpoint HMAC and applies only through explicit caller action", async () => {
    const bytes = exportBytes();
    const verification = verifyChatHistoryExport(bytes, {
      expectedSha256: checksum(bytes),
    });
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = vi.fn(
      async (url: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ url: String(url), init });
        return new Response(
          JSON.stringify({
            status: "applied",
            batch_id: signChatHistoryPlan(verification.plan, SIGNING_SECRET)
              .batchId,
            manifest_sha256: verification.plan.manifestHash,
            entry_count: 2,
            applied_count: 2,
            already_applied_count: 0,
          }),
          { status: 200 },
        );
      },
    ) as unknown as typeof fetch;

    const dryRun = verifyChatHistoryExport(bytes);
    expect(dryRun.verified).toBe(false);
    expect(fetchImpl).not.toHaveBeenCalled();

    const result = await applyChatHistoryPlan(verification.plan, {
      endpoint: "https://jobs.test/internal/chat-history/apply/",
      adminKey: "admin-secret",
      signingSecret: SIGNING_SECRET,
      fetchImpl,
    });
    expect(result).toMatchObject({
      status: "applied",
      entry_count: 2,
      applied_count: 2,
    });
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toBe(
      "https://jobs.test/internal/chat-history/apply",
    );
    expect(requests[0].init?.headers).toMatchObject({
      "secret-key": "admin-secret",
      "x-chat-history-plan-signature": signChatHistoryPlan(
        verification.plan,
        SIGNING_SECRET,
      ).signature,
    });
    const body = JSON.parse(String(requests[0].init?.body));
    expect(body.batch_id).toBe(result.batch_id);
    expect(body.entries).toHaveLength(2);
    expect(JSON.stringify(result)).not.toContain("admin-secret");
    expect(JSON.stringify(result)).not.toContain(SIGNING_SECRET);
  });

  it("refuses to sign blocked or oversized apply plans", () => {
    const plan = verifyChatHistoryExport(exportBytes()).plan;
    expect(() => signChatHistoryPlan({ ...plan, blocked: 1 }, SIGNING_SECRET)).toThrow(
      "all-stage",
    );
    expect(() => signChatHistoryPlan(plan, "short")).toThrow("32 characters");
  });
});
