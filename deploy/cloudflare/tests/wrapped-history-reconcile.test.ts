import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  planWrappedHistory,
  renderWrappedHistorySql,
  renderWrappedHistoryVerifySql,
  verifyWrappedHistory,
} from "../scripts/wrapped-history-reconcile.mjs";

const sourceFingerprint = "a".repeat(64);
const exportSha256 = "b".repeat(64);

function result(): Record<string, unknown> {
  const event = {
    date: "March 15",
    title: "A memorable day",
    description: "A bounded description.",
    story: "A bounded story.",
    emoji: "🎉",
  };
  return {
    decision_style: {
      name: "Reflective Executor",
      description: "Decisive after reflection.",
    },
    top_phrases: [
      { phrase: "Let's do this", context: "Starting important work." },
    ],
    memorable_days: {
      most_fun_day: event,
      most_productive_day: event,
      most_stressful_day: event,
    },
    funniest_event: event,
    most_embarrassing_event: event,
    top_buddies: [
      {
        name: "Alex",
        relationship: "Friend",
        context: "Recurring collaborator.",
        emoji: "🤝",
      },
    ],
    obsessions: {
      show: "Not mentioned",
      movie: "Not mentioned",
      book: "Not mentioned",
      celebrity: "Not mentioned",
      food: "Not mentioned",
    },
    movie_recommendations: ["Inception"],
    struggle: { title: "A hard season", description: "Kept moving." },
    personal_win: { title: "Steady progress", description: "Made progress." },
  };
}

function manifest(
  rows: Array<Record<string, unknown>> = [
    {
      uid: "user-1",
      year: 2025,
      status: "done",
      source_fingerprint: sourceFingerprint,
      account_generation: 4,
      result: result(),
      created_at: 1_735_689_600,
      updated_at: 1_735_689_900,
    },
  ],
) {
  return {
    schema_version: 1,
    source: {
      kind: "firestore",
      collection: "users/{uid}/wrapped/{year}",
      export_sha256: exportSha256,
    },
    rows,
  };
}

describe("Wrapped historical result reconciliation planner", () => {
  it("plans a completed result with manifest and row checksums, then verifies it", () => {
    const plan = planWrappedHistory(manifest());
    expect(plan).toMatchObject({
      mode: "dry-run",
      schema_version: 1,
      total: 1,
      stage: 1,
      blocked: 0,
      source: { export_sha256: exportSha256 },
    });
    expect(plan.manifest_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(plan.entries[0]).toMatchObject({
      uid: "user-1",
      year: 2025,
      action: "stage",
      status: "planned",
      sourceFingerprint,
      accountGeneration: 4,
      resultSha256: expect.stringMatching(/^[0-9a-f]{64}$/),
      sourceRowSha256: expect.stringMatching(/^[0-9a-f]{64}$/),
    });
    const sql = renderWrappedHistorySql(plan, 1_800_000_000);
    expect(sql).toContain("cf_wrapped_jobs");
    expect(sql).toContain("state = 'new'");
    expect(sql).toContain("destination_backend_bound = 1");
    expect(sql).toContain(
      "NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents",
    );
    expect(sql).not.toMatch(/\b(?:BEGIN|COMMIT|SAVEPOINT)\b[^\n]*;/);
    expect(renderWrappedHistoryVerifySql(plan)).toContain("source_fingerprint");

    const verification = verifyWrappedHistory(plan, [
      {
        uid: "user-1",
        year: 2025,
        status: "completed",
        request_fingerprint: plan.entries[0].requestFingerprint,
        source_fingerprint: sourceFingerprint,
        account_generation: 4,
        result_json: plan.entries[0].resultJson,
      },
    ]);
    expect(verification).toMatchObject({
      status: "passed",
      checked: 1,
      missing: [],
      mismatched: [],
    });
  });

  it("blocks missing attestations, secrets, unsupported states, and deletion-fenced owners", () => {
    const rows = manifest([
      {
        uid: "user-1",
        year: 2025,
        status: "done",
        source_fingerprint: undefined,
        account_generation: 4,
        result: { ...result(), api_key: "must-not-land" },
        created_at: 1,
        updated_at: 2,
      },
      {
        uid: "user-2",
        year: 2025,
        status: "processing",
        source_fingerprint: sourceFingerprint,
        account_generation: 0,
        result: result(),
        created_at: 1,
        updated_at: 2,
      },
      {
        uid: "user-3",
        year: 2025,
        status: "done",
        source_fingerprint: sourceFingerprint,
        account_generation: 0,
        result: result(),
        created_at: 1,
        updated_at: 2,
      },
    ]);
    const plan = planWrappedHistory(rows, { fencedUids: ["user-3"] });
    expect(plan).toMatchObject({ total: 3, stage: 0, blocked: 3 });
    expect(plan.entries.map((entry) => entry.lastError)).toEqual([
      "sensitive_field:result.api_key,source_fingerprint_missing_or_invalid",
      "status_not_completed",
      "account_deletion_fence",
    ]);
    expect(renderWrappedHistorySql(plan)).not.toContain(
      "INSERT INTO cf_wrapped_jobs",
    );
    const verification = verifyWrappedHistory(plan, []);
    expect(verification).toMatchObject({
      status: "passed",
      checked: 0,
      blocked: 3,
    });
  });

  it("deduplicates identical rows but blocks conflicting history", () => {
    const first = manifest().rows[0];
    const same = { ...first };
    const changed = {
      ...first,
      result: {
        ...result(),
        personal_win: { title: "Different", description: "Different." },
      },
    };
    const deduped = planWrappedHistory(manifest([first, same]));
    expect(deduped).toMatchObject({ total: 1, stage: 1, blocked: 0 });
    const conflict = planWrappedHistory(manifest([first, changed]));
    expect(conflict).toMatchObject({ total: 1, stage: 0, blocked: 1 });
    expect(conflict.entries[0].lastError).toBe("conflicting_duplicate_row");
  });

  it("applies only when destination generation and deletion fence permit it", () => {
    const plan = planWrappedHistory(manifest());
    const database = new DatabaseSync(":memory:");
    try {
      database.exec("PRAGMA foreign_keys = ON");
      const migrations = path.resolve(
        path.dirname(fileURLToPath(import.meta.url)),
        "../migrations/app",
      );
      for (const filename of readdirSync(migrations)
        .filter((value) => value.endsWith(".sql"))
        .sort()) {
        database.exec(readFileSync(path.join(migrations, filename), "utf8"));
      }
      database
        .prepare(
          "INSERT INTO cf_account_cutover (uid, state, account_generation, ui_generation, api_generation, checkpoint_phase, destination_backend_bound, updated_at) VALUES (?, 'new', ?, ?, ?, 'completed', 1, ?)",
        )
        .run("user-1", 4, 4, 4, 1_800_000_000);
      database.exec(renderWrappedHistorySql(plan, 1_800_000_000));
      database.exec(renderWrappedHistorySql(plan, 1_800_000_000));
      expect(
        database
          .prepare(
            "SELECT status, source_fingerprint, account_generation FROM cf_wrapped_jobs WHERE uid = 'user-1' AND year = 2025",
          )
          .get(),
      ).toMatchObject({
        status: "completed",
        source_fingerprint: sourceFingerprint,
        account_generation: 4,
      });

      database
        .prepare(
          "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES (?, ?, 'pending', 'quiescing', ?, ?, ?)",
        )
        .run("user-2", "delete-1", 1, 1, 1);
      const fencedPlan = planWrappedHistory(
        manifest([{ ...manifest().rows[0], uid: "user-2" }]),
      );
      database.exec(renderWrappedHistorySql(fencedPlan, 1_800_000_000));
      expect(
        database
          .prepare(
            "SELECT count(*) AS count FROM cf_wrapped_jobs WHERE uid = 'user-2'",
          )
          .get(),
      ).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });
});
