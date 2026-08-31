import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import { planWrappedHistory } from "../scripts/wrapped-history-reconcile.mjs";
import type { JobsEnv } from "../workers/jobs/env";
import { registerWrappedHistoryImportRoutes } from "../workers/jobs/wrapped-history-import";

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql"))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...(args as never[])) as
          T | undefined) ?? null,
      all: async <T>() => ({
        results: this.database.prepare(sql).all(...(args as never[])) as T[],
      }),
      run: async () => ({
        meta: {
          changes: Number(
            this.database.prepare(sql).run(...(args as never[])).changes,
          ),
        },
      }),
    });
    return build();
  }

  async batch(statements: Array<ReturnType<SqliteD1["prepare"]>>) {
    this.database.exec("BEGIN");
    try {
      const results = [];
      for (const statement of statements) {
        const run = await statement.run();
        results.push({ success: true, meta: run.meta });
      }
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  close() {
    this.database.close();
  }
}

const exportSha256 = "b".repeat(64);
const sourceFingerprint = "a".repeat(64);

function result() {
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

function plan() {
  return planWrappedHistory({
    schema_version: 1,
    source: {
      kind: "firestore",
      collection: "users/{uid}/wrapped/{year}",
      export_sha256: exportSha256,
    },
    rows: [
      {
        uid: "wrapped-history-user",
        year: 2025,
        status: "done",
        source_fingerprint: sourceFingerprint,
        account_generation: 4,
        result: result(),
        created_at: 1_735_689_600,
        updated_at: 1_735_689_900,
      },
    ],
  });
}

function environment(enabled = true) {
  const database = new SqliteD1();
  database.database.exec(
    "INSERT INTO cf_account_cutover (uid, state, account_generation, ui_generation, api_generation, checkpoint_phase, destination_backend_bound, updated_at) VALUES ('wrapped-history-user', 'new', 4, 4, 4, 'completed', 1, 1000)",
  );
  const env = {
    APP_DB: database,
    ADMIN_KEY: "wrapped-history-admin",
    WRAPPED_HISTORY_IMPORT_STAGING_ENABLED: enabled ? "true" : "false",
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerWrappedHistoryImportRoutes(app);
  return { app, database, env };
}

function adminHeaders() {
  return {
    "secret-key": "wrapped-history-admin",
    "content-type": "application/json",
  };
}

describe("Cloudflare reviewed Wrapped history executor", () => {
  it("is fail-closed by default and requires the operator key", async () => {
    const { app, database, env } = environment(false);
    try {
      const disabled = await app.request(
        "/internal/wrapped-history/reviews",
        {
          method: "POST",
          headers: adminHeaders(),
          body: JSON.stringify(plan()),
        },
        env,
      );
      expect(disabled.status).toBe(503);
      env.WRAPPED_HISTORY_IMPORT_STAGING_ENABLED = "true";
      const unauthorized = await app.request(
        "/internal/wrapped-history/reviews",
        {
          method: "POST",
          body: JSON.stringify(plan()),
        },
        env,
      );
      expect(unauthorized.status).toBe(403);
      expect(
        database.database
          .prepare("SELECT COUNT(*) AS count FROM cf_wrapped_jobs")
          .get(),
      ).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });

  it("promotes only an approved plan and is idempotent without provider calls", async () => {
    const { app, database, env } = environment();
    try {
      const reviewed = await app.request(
        "/internal/wrapped-history/reviews",
        {
          method: "POST",
          headers: adminHeaders(),
          body: JSON.stringify(plan()),
        },
        env,
      );
      expect(reviewed.status).toBe(201);
      const reviewBody = (await reviewed.json()) as {
        review_id: string;
        entry_count: number;
      };
      expect(reviewBody.entry_count).toBe(1);
      const applied = await app.request(
        `/internal/wrapped-history/reviews/${reviewBody.review_id}/apply`,
        {
          method: "POST",
          headers: { "secret-key": "wrapped-history-admin" },
        },
        env,
      );
      expect(applied.status).toBe(200);
      expect(await applied.json()).toMatchObject({
        status: "applied",
        applied_count: 1,
        already_applied_count: 0,
      });
      expect(
        database.database
          .prepare(
            "SELECT status, result_json, account_generation FROM cf_wrapped_jobs",
          )
          .get(),
      ).toMatchObject({ status: "completed", account_generation: 4 });
      expect(
        database.database
          .prepare("SELECT status FROM cf_wrapped_history_applies")
          .get(),
      ).toMatchObject({ status: "applied" });
      const repeated = await app.request(
        `/internal/wrapped-history/reviews/${reviewBody.review_id}/apply`,
        {
          method: "POST",
          headers: { "secret-key": "wrapped-history-admin" },
        },
        env,
      );
      expect(repeated.status).toBe(200);
      expect(await repeated.json()).toMatchObject({
        status: "applied",
        applied_count: 0,
        already_applied_count: 1,
      });
      expect(
        database.database
          .prepare("SELECT COUNT(*) AS count FROM cf_wrapped_jobs")
          .get(),
      ).toMatchObject({ count: 1 });
    } finally {
      database.close();
    }
  });

  it("rechecks generation and deletion fences after review", async () => {
    const { app, database, env } = environment();
    try {
      const reviewed = await app.request(
        "/internal/wrapped-history/reviews",
        {
          method: "POST",
          headers: adminHeaders(),
          body: JSON.stringify(plan()),
        },
        env,
      );
      const reviewId = ((await reviewed.json()) as { review_id: string })
        .review_id;
      database.database.exec(
        "UPDATE cf_account_cutover SET account_generation = 5 WHERE uid = 'wrapped-history-user'",
      );
      const drift = await app.request(
        `/internal/wrapped-history/reviews/${reviewId}/apply`,
        {
          method: "POST",
          headers: { "secret-key": "wrapped-history-admin" },
        },
        env,
      );
      expect(drift.status).toBe(409);
      expect(
        database.database
          .prepare("SELECT COUNT(*) AS count FROM cf_wrapped_jobs")
          .get(),
      ).toMatchObject({ count: 0 });
    } finally {
      database.close();
    }
  });
});
