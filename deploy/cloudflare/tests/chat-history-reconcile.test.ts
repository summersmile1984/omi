import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  planChatHistoryReconciliation,
  renderChatHistoryApplySql,
  renderChatHistoryVerifySql,
} from "../scripts/chat-history-reconcile.mjs";

const SOURCE_EXPORT = "a".repeat(64);
const SOURCE_FINGERPRINT = "b".repeat(64);
const CHECKSUM = "c".repeat(64);

function manifest(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collections: ["users/{uid}/chat_sessions", "users/{uid}/messages"],
      export_sha256: SOURCE_EXPORT,
      exported_at: "2026-09-01T00:00:00Z",
    },
    accounts: [
      {
        uid: "ba-user-1",
        account_generation: 2,
        source_fingerprint: SOURCE_FINGERPRINT,
      },
    ],
    sessions: [
      {
        uid: "ba-user-1",
        id: "session-1",
        title: "Imported chat",
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
        uid: "ba-user-1",
        id: "message-1",
        app_id: null,
        created_at: 1_700_000_000,
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

describe("chat history reconciliation planner", () => {
  it("plans a bounded session/message replay with guarded apply and verify SQL", () => {
    const plan = planChatHistoryReconciliation(manifest());
    expect(plan).toMatchObject({
      mode: "reviewed-plan",
      total: 2,
      stage: 2,
      blocked: 0,
      source: { export_sha256: SOURCE_EXPORT },
    });
    expect(plan.entries.every((entry) => /^[0-9a-f]{64}$/.test(entry.sourceRowSha256))).toBe(true);
    const apply = renderChatHistoryApplySql(plan, 1_700_000_010);
    expect(apply).toContain("cf_chat_history_import_ledger");
    expect(apply).toContain("destination_backend_bound = 1");
    expect(apply).toContain("history_account_generation");
    expect(apply).toContain("ON CONFLICT(uid, id) DO NOTHING");
    expect(apply).not.toContain("access_token");
    expect(renderChatHistoryVerifySql(plan)).toContain("zero-row result is required");
  });

  it("blocks attachment-bearing history until canonical files are independently verified", () => {
    const plan = planChatHistoryReconciliation(
      manifest({
        messages: [
          {
            uid: "ba-user-1",
            id: "message-1",
            created_at: 1_700_000_000,
            message_json: {
              id: "message-1",
              text: "with file",
              sender: "human",
              type: "text",
              chat_session_id: "session-1",
              files_id: ["legacy-file-1"],
            },
          },
        ],
      }),
    );
    expect(plan).toMatchObject({ stage: 0, blocked: 2 });
    expect(plan.entries.map((entry) => entry.lastError)).toContain("message_blocked");
    expect(renderChatHistoryApplySql(plan)).not.toContain("INSERT INTO cf_chat_sessions");
  });

  it("rejects secrets and fenced accounts before producing apply SQL", () => {
    expect(() =>
      planChatHistoryReconciliation(
        manifest({
          messages: [
            {
              uid: "ba-user-1",
              id: "message-1",
              created_at: 1_700_000_000,
              message_json: {
                id: "message-1",
                text: "bad",
                sender: "human",
                type: "text",
                chat_session_id: "session-1",
                access_token: "must-not-land",
              },
            },
          ],
        }),
      ),
    ).toThrow("sensitive field");

    const fenced = planChatHistoryReconciliation(manifest(), { fencedUids: ["ba-user-1"] });
    expect(fenced).toMatchObject({ stage: 0, blocked: 2 });
    expect(fenced.entries.every((entry) => entry.lastError?.includes("account_deletion_fence"))).toBe(true);
    expect(renderChatHistoryApplySql(fenced)).not.toContain("INSERT INTO cf_chat_messages");
  });

  it("detects source conflicts and preserves an existing destination row", () => {
    const conflicting = planChatHistoryReconciliation(
      manifest({
        sessions: [
          manifest().sessions[0],
          { ...manifest().sessions[0], title: "Changed" },
        ],
      }),
    );
    expect(conflicting.stage).toBe(0);
    expect(conflicting.blocked).toBe(2);

    const plan = planChatHistoryReconciliation(manifest());
    const database = new DatabaseSync(":memory:");
    database.exec(`
      CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);
      CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);
      CREATE TABLE cf_account_cutover (
        uid TEXT PRIMARY KEY, account_generation INTEGER NOT NULL,
        destination_backend_bound INTEGER NOT NULL, state TEXT NOT NULL,
        checkpoint_phase TEXT NOT NULL
      );
      CREATE TABLE cf_chat_sessions (
        uid TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
        preview TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        app_id TEXT, message_count INTEGER NOT NULL, starred INTEGER NOT NULL,
        PRIMARY KEY(uid, id)
      );
      CREATE TABLE cf_chat_messages (
        uid TEXT NOT NULL, id TEXT NOT NULL, app_id TEXT, created_at INTEGER NOT NULL,
        message_json TEXT NOT NULL,
        PRIMARY KEY(uid, id)
      );
    `);
    database.exec(readFileSync(new URL("../migrations/app/0128_chat_history_reconciliation.sql", import.meta.url), "utf8"));
    database.exec(renderChatHistoryApplySql(plan, 1_700_000_009));
    expect(database.prepare("SELECT COUNT(*) AS count FROM cf_chat_history_import_ledger").get()).toEqual({ count: 0 });
    database.prepare("INSERT INTO cf_account_cutover VALUES (?, ?, ?, ?, ?)").run("ba-user-1", 2, 1, "new", "completed");
    database.prepare("INSERT INTO cf_chat_sessions (uid,id,title,preview,created_at,updated_at,app_id,message_count,starred) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)").run("ba-user-1", "session-existing", "untouched", null, 1, 1, null, 0, 0);
    database.exec(renderChatHistoryApplySql(plan, 1_700_000_010));
    expect(database.prepare("SELECT COUNT(*) AS count FROM cf_chat_sessions WHERE id = 'session-1'").get()).toEqual({ count: 1 });
    expect(database.prepare("SELECT COUNT(*) AS count FROM cf_chat_messages WHERE id = 'message-1'").get()).toEqual({ count: 1 });
    expect(database.prepare("SELECT COUNT(*) AS count FROM cf_chat_history_import_ledger WHERE status = 'applied'").get()).toEqual({ count: 2 });
    const title = database.prepare("SELECT title FROM cf_chat_sessions WHERE id = 'session-existing'").get() as { title: string };
    expect(title.title).toBe("untouched");
    database.exec(renderChatHistoryApplySql(plan, 1_700_000_011));
    expect(database.prepare("SELECT COUNT(*) AS count FROM cf_chat_history_import_ledger").get()).toEqual({ count: 2 });
    database.close();
  });

  it("keeps the migration schema in the tracked app migration chain", () => {
    const migration = readFileSync(new URL("../migrations/app/0128_chat_history_reconciliation.sql", import.meta.url), "utf8");
    expect(migration).toContain("cf_chat_history_import_ledger");
    expect(migration).toContain("account deletion fence");
  });
});
