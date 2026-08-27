import { describe, expect, it } from "vitest";
import { normalizeRow, renderBackfillSql } from "../scripts/backfill-d1.mjs";

describe("D1 backfill SQL generator", () => {
  it("normalizes Firestore-shaped values into whitelisted transactional upserts", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_action_items",
        row: {
          uid: "user-1",
          id: "task-1",
          description: "ship it",
          completed: true,
          created_at: "2026-08-28T10:00:00Z",
          updated_at: "2026-08-28T10:01:00Z",
          provenance: [{ source: "legacy" }],
        },
      },
      {
        table: "cf_screen_activity",
        row: {
          uid: "user-1",
          id: "mac-1-9",
          timestamp: "2026-08-28T10:02:03.123Z",
          app_name: "O'Malley",
          ocr_text: "hello",
        },
      },
    ]);

    expect(sql).toContain("BEGIN TRANSACTION;");
    expect(sql).toContain("status, completed");
    expect(sql).toContain("'completed'");
    expect(sql).toContain("'2026-08-28 10:02:03.123'");
    expect(sql).toContain("'O''Malley'");
    expect(sql).toContain("COMMIT;");
  });

  it("supports typed aliases while rejecting unsupported tables and malformed identities", () => {
    expect(normalizeRow("cf_goals", {
      uid: "u",
      id: "g",
      title: "Goal",
      desired_outcome: "Outcome",
      status: "focused",
      created_at: 1,
      updated_at: 2,
    }).source).toBe("imported");
    expect(() => renderBackfillSql([{ table: "cf_unknown", row: { uid: "u", id: "x" } }])).toThrow(
      "unsupported table",
    );
    expect(() => renderBackfillSql([{ table: "cf_people", row: { uid: "u", id: "x", name: "A" } }])).toThrow(
      "missing created_at",
    );
    expect(() => renderBackfillSql([{ table: "cf_action_items", row: {
      uid: "u", id: "x", description: "bad", completed: false, status: "completed", created_at: 1, updated_at: 1,
    } }])).toThrow("completed action item");
  });
});
