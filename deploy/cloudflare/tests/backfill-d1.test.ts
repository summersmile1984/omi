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

  it("backfills calendar onboarding flags without accepting token material", () => {
    const normalized = normalizeRow("cf_user_calendar_onboarding", {
      uid: "u",
      connected: true,
      onboarding_skipped: false,
      reauth_required: true,
      has_access_token: true,
      reauth_reason: "token_expired",
      created_at: 1,
      updated_at: 2,
      access_token: "must-not-land-in-d1-projection",
    });
    expect(normalized).toMatchObject({
      uid: "u",
      connected: 1,
      onboarding_skipped: 0,
      reauth_required: 1,
      has_access_token: 1,
      reauth_reason: "token_expired",
    });
    expect(normalized).not.toHaveProperty("access_token");
    expect(renderBackfillSql([{ table: "cf_user_calendar_onboarding", row: normalized }])).toContain(
      "cf_user_calendar_onboarding",
    );
  });

  it("renders history rows with their three-column uid-scoped key", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_goal_progress_history",
        row: { uid: "u", goal_id: "g", date: "2026-08-28", value: 25, recorded_at: 1 },
      },
    ]);
    expect(sql).toContain("ON CONFLICT(uid, goal_id, date) DO UPDATE SET value = excluded.value");
    expect(() => renderBackfillSql([
      { table: "cf_goal_progress_history", row: { uid: "u", goal_id: "g", date: "2026-08-28", value: 25 } },
    ])).toThrow("missing recorded_at");
  });
});
