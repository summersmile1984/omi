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
    expect(
      normalizeRow("cf_goals", {
        uid: "u",
        id: "g",
        title: "Goal",
        desired_outcome: "Outcome",
        status: "focused",
        created_at: 1,
        updated_at: 2,
      }).source,
    ).toBe("imported");
    expect(() =>
      renderBackfillSql([{ table: "cf_unknown", row: { uid: "u", id: "x" } }]),
    ).toThrow("unsupported table");
    expect(() =>
      renderBackfillSql([
        { table: "cf_people", row: { uid: "u", id: "x", name: "A" } },
      ]),
    ).toThrow("missing created_at");
    expect(() =>
      renderBackfillSql([
        {
          table: "cf_action_items",
          row: {
            uid: "u",
            id: "x",
            description: "bad",
            completed: false,
            status: "completed",
            created_at: 1,
            updated_at: 1,
          },
        },
      ]),
    ).toThrow("completed action item");
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
    expect(
      renderBackfillSql([
        { table: "cf_user_calendar_onboarding", row: normalized },
      ]),
    ).toContain("cf_user_calendar_onboarding");
  });

  it("backfills legacy MCP key metadata without accepting raw key material", () => {
    const normalized = normalizeRow("cf_mcp_api_keys", {
      id: "legacy-key-1",
      user_id: "user-1",
      name: "Imported key",
      hashed_key: "a".repeat(64),
      created_at: "2026-08-28T10:00:00Z",
    });
    expect(normalized).toMatchObject({
      uid: "user-1",
      key_id: "legacy-key-1",
      name: "Imported key",
      key_hash: "a".repeat(64),
      key_prefix: "omi_mcp_legacy",
      app_id: "mcp-api",
      created_at: 1787911200,
    });
    expect(JSON.parse(String(normalized.scopes_json))).toHaveLength(9);
    expect(
      renderBackfillSql([{ table: "cf_mcp_api_keys", row: normalized }]),
    ).toContain("ON CONFLICT(key_id) DO UPDATE SET uid = excluded.uid");

    expect(() =>
      normalizeRow("cf_mcp_api_keys", {
        id: "legacy-key-2",
        user_id: "user-1",
        hashed_key: "b".repeat(64),
        created_at: 1,
        raw_key: `omi_mcp_${"f".repeat(32)}`,
      }),
    ).toThrow("must not contain raw secret");
    expect(() =>
      normalizeRow("cf_mcp_api_keys", {
        id: "legacy-key-3",
        user_id: "user-1",
        hashed_key: "not-a-hash",
        created_at: 1,
      }),
    ).toThrow("key_hash is invalid");
  });

  it("backfills raw X posts with bounded kinds and JSON metrics", () => {
    const normalized = normalizeRow("cf_x_posts", {
      uid: "user-1",
      id: "tweet-1",
      text: "Workers deployment notes",
      kind: "bookmark",
      lang: "en",
      metrics: { like_count: 3 },
      created_at: "2026-08-28T10:00:00Z",
      ingested_at: "2026-08-28T10:01:00Z",
      updated_at: "2026-08-28T10:01:00Z",
      memory_extraction_status: "completed",
    });
    expect(normalized).toMatchObject({
      uid: "user-1",
      id: "tweet-1",
      kind: "bookmark",
      created_at: 1787911200,
      memory_extraction_status: "completed",
    });
    expect(JSON.parse(String(normalized.metrics_json))).toEqual({
      like_count: 3,
    });
    expect(
      renderBackfillSql([{ table: "cf_x_posts", row: normalized }]),
    ).toContain("ON CONFLICT(uid, id) DO UPDATE SET text = excluded.text");
    expect(() =>
      normalizeRow("cf_x_posts", {
        ...normalized,
        kind: "retweet",
      }),
    ).toThrow("cf_x_posts.kind is invalid");
  });

  it("renders history rows with their three-column uid-scoped key", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_goal_progress_history",
        row: {
          uid: "u",
          goal_id: "g",
          date: "2026-08-28",
          value: 25,
          recorded_at: 1,
        },
      },
    ]);
    expect(sql).toContain(
      "ON CONFLICT(uid, goal_id, date) DO UPDATE SET value = excluded.value",
    );
    expect(() =>
      renderBackfillSql([
        {
          table: "cf_goal_progress_history",
          row: { uid: "u", goal_id: "g", date: "2026-08-28", value: 25 },
        },
      ]),
    ).toThrow("missing recorded_at");
  });

  it("renders goal progress events with the uid/event id key and JSON projections", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_goal_progress_events",
        row: {
          uid: "u",
          event_id: "gpe-1",
          goal_id: "g",
          sequence: 1,
          kind: "evidence",
          summary: "Captured evidence",
          evidence_refs_json: [
            { kind: "external", id: "source-1", scope: "canonical" },
          ],
          created_at: "2026-08-28T10:00:00Z",
        },
      },
    ]);
    expect(sql).toContain(
      "ON CONFLICT(uid, event_id) DO UPDATE SET goal_id = excluded.goal_id",
    );
    expect(sql).toContain("evidence_refs_json");
    expect(() =>
      renderBackfillSql([
        {
          table: "cf_goal_progress_events",
          row: { uid: "u", event_id: "gpe-1", goal_id: "g" },
        },
      ]),
    ).toThrow("missing sequence");
  });

  it("renders workstream projections and journal rows with domain keys", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_workstreams",
        row: {
          uid: "u",
          id: "ws-1",
          goal_id: "goal-1",
          title: "Ship the project",
          objective: "Publish the first release",
          status: "open",
          created_at: 1,
          updated_at: 2,
        },
      },
      {
        table: "cf_workstream_events",
        row: {
          uid: "u",
          event_id: "wse-1",
          workstream_id: "ws-1",
          sequence: 1,
          kind: "system",
          summary: "Started",
          created_at: 1,
        },
      },
    ]);
    expect(sql).toContain(
      "ON CONFLICT(uid, id) DO UPDATE SET goal_id = excluded.goal_id",
    );
    expect(sql).toContain(
      "ON CONFLICT(uid, event_id) DO UPDATE SET workstream_id = excluded.workstream_id",
    );
    expect(() =>
      renderBackfillSql([
        {
          table: "cf_workstreams",
          row: {
            uid: "u",
            id: "ws-1",
            title: "Incomplete",
            objective: "Missing status",
            created_at: 1,
          },
        },
      ]),
    ).toThrow("missing status");
  });

  it("renders R2 asset metadata with the integrity checksum", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_asset_objects",
        row: {
          uid: "u",
          object_key: "u/audio/clip.wav",
          content_type: "audio/wav",
          size: 6,
          etag: '"etag"',
          checksum_sha256: "a".repeat(64),
          created_at: 1,
          updated_at: 2,
        },
      },
    ]);
    expect(sql).toContain("checksum_sha256");
    expect(sql).toContain(
      "ON CONFLICT(uid, object_key) DO UPDATE SET content_type = excluded.content_type",
    );
  });

  it("renders conversation projections from Firestore-shaped nested fields", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_conversations",
        row: {
          uid: "u",
          id: "conversation-1",
          created_at: "2026-08-28T10:00:00Z",
          started_at: "2026-08-28T09:59:00Z",
          finished_at: "2026-08-28T10:01:00Z",
          structured: {
            title: "A conversation",
            overview: "Summary",
            action_items: [],
          },
          transcript_segments: [
            { id: "segment-1", text: "hello", is_user: true, start: 0, end: 1 },
          ],
          starred: true,
        },
      },
    ]);
    expect(sql).toContain("structured_json");
    expect(sql).toContain("transcript_segments_json");
    expect(sql).toContain(
      "ON CONFLICT(uid, id) DO UPDATE SET created_at = excluded.created_at",
    );
    expect(() =>
      renderBackfillSql([
        { table: "cf_conversations", row: { uid: "u", id: "conversation-1" } },
      ]),
    ).toThrow("missing created_at");
  });

  it("renders announcement content and per-user dismissal projections", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_announcements",
        row: {
          id: "release-1",
          type: "announcement",
          created_at: "2026-08-28T10:00:00Z",
          content: { title: "A release", body: "Details" },
          targeting: { trigger: "immediate", platforms: ["ios"] },
          display: { priority: 3, show_once: true },
        },
      },
      {
        table: "cf_announcement_dismissals",
        row: {
          uid: "u",
          announcement_id: "release-1",
          dismissed_at: 2,
          cta_clicked: true,
        },
      },
    ]);
    expect(sql).toContain("cf_announcements");
    expect(sql).toContain("content_json");
    expect(sql).toContain("ON CONFLICT(id) DO UPDATE SET type = excluded.type");
    expect(sql).toContain(
      "ON CONFLICT(uid, announcement_id) DO UPDATE SET dismissed_at = excluded.dismissed_at",
    );
  });

  it("renders public app catalog projections while rejecting private app fields", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_app_catalog",
        row: {
          id: "app-1",
          approved: true,
          is_popular: true,
          installs: 12,
          rating_avg: 4.5,
          rating_count: 3,
          updated_at: "2026-08-28T10:00:00Z",
          data: {
            id: "app-1",
            uid: "owner-1",
            name: "Example",
            capabilities: ["chat", "memories"],
            description: "Public app",
          },
        },
      },
    ]);
    expect(sql).toContain("cf_app_catalog");
    expect(sql).toContain("data_json");
    expect(sql).toContain("owner_uid");
    expect(sql).toContain("'owner-1'");
    expect(sql).toContain(
      "ON CONFLICT(id) DO UPDATE SET approved = excluded.approved",
    );
    expect(() =>
      renderBackfillSql([
        {
          table: "cf_app_catalog",
          row: {
            id: "app-1",
            updated_at: 1,
            data: { id: "app-1", email: "private@example.test" },
          },
        },
      ]),
    ).toThrow("private fields");
    expect(() =>
      renderBackfillSql([
        {
          table: "cf_app_catalog",
          row: {
            id: "app-1",
            owner_uid: "",
            updated_at: 1,
            data: { id: "app-1" },
          },
        },
      ]),
    ).toThrow("owner_uid is invalid");
  });

  it("renders uid-scoped enabled-app projections with an idempotent composite key", () => {
    const sql = renderBackfillSql([
      {
        table: "cf_user_enabled_apps",
        row: { uid: "u", app_id: "app-1", created_at: "2026-08-28T10:00:00Z" },
      },
    ]);
    expect(sql).toContain("cf_user_enabled_apps");
    expect(sql).toContain(
      "ON CONFLICT(uid, app_id) DO UPDATE SET created_at = excluded.created_at",
    );
    expect(() =>
      renderBackfillSql([
        { table: "cf_user_enabled_apps", row: { uid: "u", app_id: "app-1" } },
      ]),
    ).toThrow("missing created_at");
  });

  it("imports provider-owned app Payment Link mappings without putting provider ids in the public catalog", () => {
    const row = normalizeRow("cf_app_payment_links", {
      app_id: "paid-app",
      owner_uid: "creator-user",
      stripe_account_id: "acct_testCreator123",
      stripe_product_id: "prod_testPaidApp123",
      stripe_price_id: "price_testPaidApp123",
      stripe_payment_link_id: "plink_testPaidApp123",
      payment_link_url: "https://buy.stripe.com/test_paid_app",
      unit_amount: 900,
      created_at: "2026-08-29T00:00:00Z",
      updated_at: "2026-08-29T00:01:00Z",
    });
    expect(row).toMatchObject({
      app_id: "paid-app",
      owner_uid: "creator-user",
      currency: "usd",
      interval: "month",
      active: 1,
      unit_amount: 900,
    });
    expect(
      renderBackfillSql([{ table: "cf_app_payment_links", row }]),
    ).toContain(
      "ON CONFLICT(app_id) DO UPDATE SET owner_uid = excluded.owner_uid",
    );
    expect(() =>
      normalizeRow("cf_app_payment_links", {
        ...row,
        stripe_payment_link_id: "not-a-link",
      }),
    ).toThrow("stripe_payment_link_id is invalid");
  });
});
