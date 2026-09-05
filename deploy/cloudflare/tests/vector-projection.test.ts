import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import { cleanupMemoryVectors } from "../workers/jobs/memory-vector-publication";
import {
  purgeAccountVectorProjections,
  processVectorProjection,
  reconcileVectorProjections,
  VECTOR_EMBEDDING_DIMENSIONS,
  VECTOR_EMBEDDING_MODEL,
  vectorNamespace,
} from "../workers/jobs/vector-projection";

type BoundStatement = {
  sql: string;
  args: unknown[];
  execute(): { success: true; results: unknown[]; meta: { changes: number } };
};

function sqliteValue(value: unknown) {
  return value as never;
}

class SqliteD1 {
  readonly database = new DatabaseSync(":memory:");
  now = 100;

  constructor(beforeMigration?: string) {
    this.database.exec("PRAGMA foreign_keys = ON");
    // The reconciler tests use explicit epoch seconds (100/200/300). D1
    // triggers must observe that same controlled clock, not today's date.
    this.database.function("unixepoch", () => this.now);
    const directory = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../migrations/app",
    );
    for (const filename of readdirSync(directory)
      .filter((value) => value.endsWith(".sql") && (!beforeMigration || value < beforeMigration))
      .sort()) {
      this.database.exec(readFileSync(path.join(directory, filename), "utf8"));
    }
  }

  prepare(sql: string) {
    const build = (args: unknown[] = []) => ({
      sql,
      args,
      bind: (...values: unknown[]) => build(values),
      first: async <T>() =>
        (this.database.prepare(sql).get(...args.map(sqliteValue)) as
          T | undefined) ?? null,
      all: async <T>() => ({
        success: true as const,
        results: this.database
          .prepare(sql)
          .all(...args.map(sqliteValue)) as T[],
        meta: { changes: 0 },
      }),
      run: async () => build(args).execute(),
      execute: () => {
        const statement = this.database.prepare(sql);
        if (/^SELECT\b/i.test(sql.trimStart())) {
          return {
            success: true as const,
            results: statement.all(...args.map(sqliteValue)),
            meta: { changes: 0 },
          };
        }
        const result = statement.run(...args.map(sqliteValue));
        return {
          success: true as const,
          results: [],
          meta: { changes: Number(result.changes) },
        };
      },
    });
    return build();
  }

  async batch(statements: BoundStatement[]) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((statement) => statement.execute());
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

class FakeVectorize {
  readonly upserts: Array<Array<Record<string, unknown>>> = [];
  readonly deletes: string[][] = [];
  readonly vectors = new Map<string, Record<string, unknown>>();
  mutationId = "";

  async upsert(vectors: Array<Record<string, unknown>>) {
    this.upserts.push(vectors);
    for (const vector of vectors) this.vectors.set(String(vector.id), vector);
    this.mutationId = crypto.randomUUID();
    return { mutationId: this.mutationId };
  }

  async deleteByIds(ids: string[]) {
    this.deletes.push(ids);
    for (const id of ids) this.vectors.delete(id);
    this.mutationId = crypto.randomUUID();
    return { mutationId: this.mutationId };
  }

  async getByIds(ids: string[]) {
    return ids.flatMap((id) => this.vectors.has(id) ? [this.vectors.get(id)!] : []);
  }

  async describe() {
    return { processedUpToMutation: this.mutationId };
  }
}

const databases: SqliteD1[] = [];

function environment(options: { failAi?: boolean; beforeMigration?: string } = {}) {
  const database = new SqliteD1(options.beforeMigration);
  databases.push(database);
  const memory = new FakeVectorize();
  const action = new FakeVectorize();
  const conversation = new FakeVectorize();
  const transcript = new FakeVectorize();
  const xPost = new FakeVectorize();
  const workstream = new FakeVectorize();
  const screenActivity = new FakeVectorize();
  const ai = {
    run: vi.fn(async (_model: string, input: Record<string, unknown>) => {
      if (options.failAi) throw new Error("simulated Workers AI failure");
      const texts = input.text as string[];
      return {
        data: texts.map(() =>
          Array.from({ length: VECTOR_EMBEDDING_DIMENSIONS }, () => 0.01),
        ),
      };
    }),
  };
  const env = {
    APP_DB: database as unknown as D1Database,
    AI: ai,
    MEMORY_VECTORS: memory,
    ACTION_ITEM_VECTORS: action,
    CONVERSATION_VECTORS: conversation,
    TRANSCRIPT_CHUNK_VECTORS: transcript,
    X_POST_VECTORS: xPost,
    WORKSTREAM_VECTORS: workstream,
    SCREEN_ACTIVITY_VECTORS: screenActivity,
    WORKERS_AI_VECTOR_MODEL: VECTOR_EMBEDDING_MODEL,
  } as unknown as JobsEnv;
  return {
    database,
    env,
    ai,
    memory,
    action,
    conversation,
    transcript,
    xPost,
    workstream,
    screenActivity,
  };
}

function seedSources(database: SqliteD1) {
  database.database
    .prepare(
      `INSERT INTO cf_memories
       (uid, id, content, category, reviewed, user_review, memory_tier, valid_at, created_at, updated_at)
       VALUES ('vector-user', 'memory-1', 'Prefers green tea', 'system', 1, 1, 'long_term', 10, 10, 10)`,
    )
    .run();
  database.database
    .prepare(
      `INSERT INTO cf_action_items
       (uid, id, description, status, completed, created_at, updated_at)
       VALUES ('vector-user', 'action-1', 'Deploy staging', 'active', 0, 10, 10)`,
    )
    .run();
  database.database
    .prepare(
      `INSERT INTO cf_conversations
       (uid, id, created_at, updated_at, status, discarded, structured_json,
        transcript_segments_json, apps_results_json)
       VALUES ('vector-user', 'conversation-1', 10, 10, 'completed', 0, ?, ?, '[]')`,
    )
    .run(
      JSON.stringify({
        title: "Launch review",
        overview: "Cloudflare staging passed",
        category: "work",
      }),
      JSON.stringify([
        {
          text: "The exact launch window is tomorrow",
          speaker_id: 0,
          start: 0,
          end: 1,
        },
      ]),
    );
  database.database
    .prepare(
      `INSERT INTO cf_x_posts
       (uid, id, text, kind, created_at, updated_at)
       VALUES ('vector-user', 'post-1', 'Workers deployment notes', 'bookmark', 10, 10)`,
    )
    .run();
}

function memoryWork(database: SqliteD1) {
  return database.database.prepare(
    "SELECT * FROM cf_vector_projection_outbox WHERE source_kind = 'memory'",
  ).get() as Parameters<typeof processVectorProjection>[1];
}

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("Vectorize rebuildable D1 projection", () => {
  it("keeps the newest vector bytes when two publishers finish in reverse order", async () => {
    const state = environment();
    seedSources(state.database);
    const row = () => state.database.database.prepare(
      "SELECT * FROM cf_vector_projection_outbox WHERE source_kind = 'memory'",
    ).get() as never;
    let resume!: () => void;
    let entered!: () => void;
    const arrived = new Promise<void>((resolve) => { entered = resolve; });
    const blocked = new Promise<void>((resolve) => { resume = resolve; });
    const upsert = state.memory.upsert.bind(state.memory);
    let first = true;
    state.ai.run.mockImplementation(async (_model, input) => ({
      data: (input.text as string[]).map((text) =>
        Array(VECTOR_EMBEDDING_DIMENSIONS).fill(text.includes("green") ? 0.1 : 0.9)),
    }));
    state.memory.upsert = async (vectors) => {
      if (first) { first = false; entered(); await blocked; }
      return upsert(vectors);
    };
    const old = processVectorProjection(state.env, row());
    await arrived;
    state.database.database.prepare(
      "UPDATE cf_memories SET content = 'Prefers oolong tea' WHERE id = 'memory-1'",
    ).run();
    expect(await processVectorProjection(state.env, row())).toBe(true);
    resume();
    await old;
    const published = state.database.database.prepare(
      "SELECT vector_id, source_version FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
    ).get()!;
    const memory = state.database.database.prepare(
      "SELECT item_revision FROM cf_memories WHERE id = 'memory-1'",
    ).get()!;
    expect(published.source_version).toBe(memory.item_revision);
    expect(state.memory.vectors.get(String(published.vector_id))?.values).toEqual(
      Array(VECTOR_EMBEDDING_DIMENSIONS).fill(0.9),
    );
  });

  it("does not let a stale deletion retract a newer publication", async () => {
    const state = environment();
    seedSources(state.database);
    await processVectorProjection(state.env, memoryWork(state.database));
    state.database.database.prepare(
      "UPDATE cf_memories SET deleted_at = 20 WHERE id = 'memory-1'",
    ).run();
    const deletion = memoryWork(state.database);
    state.database.database.prepare(
      "UPDATE cf_memories SET deleted_at = NULL, content = 'New preference' WHERE id = 'memory-1'",
    ).run();
    await processVectorProjection(state.env, memoryWork(state.database));
    const current = state.database.database.prepare(
      "SELECT vector_id FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
    ).get()!;
    await processVectorProjection(state.env, deletion);
    await cleanupMemoryVectors(state.env);
    await cleanupMemoryVectors(state.env);
    expect(state.database.database.prepare(
      "SELECT vector_id FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
    ).get()).toEqual(current);
    expect(state.memory.vectors.has(String(current.vector_id))).toBe(true);
  });

  it("keeps account deletion pending until a late writer and its vectors are drained", async () => {
    const state = environment();
    seedSources(state.database);
    let resume!: () => void;
    let entered!: () => void;
    const arrived = new Promise<void>((resolve) => { entered = resolve; });
    const blocked = new Promise<void>((resolve) => { resume = resolve; });
    const upsert = state.memory.upsert.bind(state.memory);
    state.memory.upsert = async (vectors) => {
      entered();
      await blocked;
      return upsert(vectors);
    };
    const pending = processVectorProjection(state.env, memoryWork(state.database));
    await arrived;
    state.database.database.prepare(
      "INSERT INTO cf_account_deletion_tombstones VALUES ('vector-user', 100, 10000)",
    ).run();
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(state.memory.deletes).toHaveLength(0);
    resume();
    await pending;
    expect(state.database.database.prepare(
      "SELECT COUNT(*) AS count FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
    ).get()).toEqual({ count: 0 });
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(0);
    expect(state.memory.vectors.size).toBe(0);
  });

  it("waits for applied external deletion instead of accepting its mutation receipt as completion", async () => {
    const state = environment();
    seedSources(state.database);
    await processVectorProjection(state.env, memoryWork(state.database));
    const applyDelete = state.memory.deleteByIds.bind(state.memory);
    const accepted: string[][] = [];
    state.memory.deleteByIds = async (ids) => {
      accepted.push(ids);
      return { mutationId: "pending-deletion" };
    };
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(state.memory.vectors.size).toBe(1);
    await applyDelete(accepted[0]);
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(0);
    expect(state.memory.vectors.size).toBe(0);
  });

  it("owns an accepted write even when the provider response is lost", async () => {
    const state = environment();
    seedSources(state.database);
    const upsert = state.memory.upsert.bind(state.memory);
    state.memory.upsert = async (vectors) => {
      await upsert(vectors);
      throw new Error("response lost after acceptance");
    };
    expect(await processVectorProjection(state.env, memoryWork(state.database))).toBe(false);
    expect(state.memory.vectors.size).toBe(1);
    expect(await cleanupMemoryVectors(state.env, "vector-user")).toBe(1);
    expect(state.memory.deletes).toHaveLength(0);
    state.database.now = 1001;
    expect(await cleanupMemoryVectors(state.env, "vector-user")).toBe(1);
    expect(await cleanupMemoryVectors(state.env, "vector-user")).toBe(0);
    expect(state.memory.vectors.size).toBe(0);
  });

  it("retains an abandoned claim until its writer bound and an observed delete barrier", async () => {
    const state = environment();
    state.database.database.prepare(
      `INSERT INTO cf_memory_vector_artifacts
       (vector_id, uid, source_id, attempt_id, sub_id, source_version, model, writer_until)
       VALUES ('unknown-vector', 'vector-user', 'memory-1', 'lost-attempt', '0', 1, ?, 1000)`,
    ).run(VECTOR_EMBEDDING_MODEL);
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(state.memory.deletes).toHaveLength(0);
    state.database.now = 1001;
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(state.memory.deletes).toEqual([["unknown-vector"]]);
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(0);
  });

  it("allows fenced cleanup progress but denies extending or moving a writer", async () => {
    const state = environment();
    state.database.database.prepare(
      `INSERT INTO cf_memory_vector_artifacts
       (vector_id, uid, source_id, attempt_id, sub_id, source_version, model, writer_until)
       VALUES ('owned-vector', 'vector-user', 'memory-1', 'attempt', '0', 1, ?, 1000)`,
    ).run(VECTOR_EMBEDDING_MODEL);
    state.database.database.prepare(
      "INSERT INTO cf_account_deletion_tombstones VALUES ('vector-user', 100, 10000)",
    ).run();
    for (const mutation of ["uid = 'another-user'", "writer_until = 1001", "source_version = 2"]) {
      expect(() => state.database.database.prepare(
        `UPDATE cf_memory_vector_artifacts SET ${mutation}`,
      ).run()).toThrow("account_deletion_in_progress");
    }
    state.database.database.prepare(
      "UPDATE cf_memory_vector_artifacts SET writer_done = 1, retired = 1",
    ).run();
    expect(() => state.database.database.prepare(
      "UPDATE cf_memory_vector_artifacts SET retired = 0",
    ).run()).toThrow("account_deletion_in_progress");
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(0);
  });

  it("adopts existing projection IDs during upgrade and preserves their old-writer drain window", async () => {
    const state = environment({ beforeMigration: "0163_memory_vector_publication.sql" });
    seedSources(state.database);
    const legacyId = "a".repeat(64);
    state.database.database.prepare(
      `INSERT INTO cf_vector_projection_state
       (uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at)
       SELECT uid, 'memory', id, '000000', ?, item_revision, ?, 100
       FROM cf_memories WHERE id = 'memory-1'`,
    ).run(legacyId, VECTOR_EMBEDDING_MODEL);
    await state.memory.upsert([{ id: legacyId, values: [0.1] }]);
    state.database.database.exec(readFileSync(new URL(
      "../migrations/app/0163_memory_vector_publication.sql", import.meta.url,
    ), "utf8"));
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(state.memory.deletes).toHaveLength(0);
    state.database.now = 1001;
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(1);
    expect(state.memory.deletes).toEqual([[legacyId]]);
    expect(await purgeAccountVectorProjections(state.env, "vector-user")).toBe(0);
  });

  it("retracts a blank legacy memory without leaving an impossible embedding retry", async () => {
    const state = environment();
    seedSources(state.database);
    await processVectorProjection(state.env, memoryWork(state.database));
    state.database.database.prepare(
      "UPDATE cf_memories SET content = ' ' WHERE id = 'memory-1'",
    ).run();
    expect(await processVectorProjection(state.env, memoryWork(state.database))).toBe(true);
    expect(memoryWork(state.database)).toBeUndefined();
    expect(await cleanupMemoryVectors(state.env, "vector-user")).toBe(1);
    expect(await cleanupMemoryVectors(state.env, "vector-user")).toBe(0);
  });

  it("seeds missing rows, embeds them, and records only candidate mappings", async () => {
    const state = environment();
    seedSources(state.database);

    await expect(reconcileVectorProjections(state.env, 100)).resolves.toBe(4);

    expect(state.ai.run).toHaveBeenCalledTimes(4);
    expect(state.memory.upserts).toHaveLength(1);
    expect(state.action.upserts).toHaveLength(1);
    expect(state.conversation.upserts).toHaveLength(1);
    expect(state.transcript.upserts).toHaveLength(1);
    expect(state.xPost.upserts).toHaveLength(1);
    const namespace = await vectorNamespace("vector-user");
    expect(namespace).toHaveLength(64);
    expect(state.memory.upserts[0][0]).toMatchObject({ namespace });
    expect(state.conversation.upserts[0][0]).toMatchObject({
      namespace,
      metadata: { created_at: 10 },
    });
    expect(
      state.database.database
        .prepare(
          "SELECT projection_kind FROM cf_vector_projection_state ORDER BY projection_kind",
        )
        .all(),
    ).toEqual([
      { projection_kind: "action_item" },
      { projection_kind: "conversation" },
      { projection_kind: "memory" },
      { projection_kind: "transcript_chunk" },
      { projection_kind: "x_post" },
    ]);
    expect(
      state.database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_vector_projection_outbox")
        .get(),
    ).toEqual({ count: 0 });
  });

  it("projects open workstreams and screen activity as reembedded candidates", async () => {
    const state = environment();
    state.database.database
      .prepare(
        `INSERT INTO cf_workstreams
         (uid, id, goal_id, title, objective, status, current_state_summary,
          last_meaningful_progress_at, latest_event_sequence, account_generation, created_at, updated_at)
         VALUES ('vector-user', 'ws-1', NULL, 'Ship search', 'Ship semantic search', 'open',
                 'Reembedding underway', 10, 1, 1, 10, 10)`,
      )
      .run();
    state.database.database
      .prepare(
        `INSERT INTO cf_screen_activity
         (uid, id, timestamp, app_name, window_title, ocr_text, updated_at)
         VALUES ('vector-user', 'shot-1', '2026-01-02 03:04:05.000', 'Xcode', 'Omi.xcodeproj',
                 'building the desktop app', NULL)`,
      )
      .run();

    await expect(reconcileVectorProjections(state.env, 100)).resolves.toBe(2);
    expect(state.workstream.upserts).toHaveLength(1);
    expect(state.screenActivity.upserts).toHaveLength(1);
    const namespace = await vectorNamespace("vector-user");
    expect(state.workstream.upserts[0][0]).toMatchObject({
      namespace,
      metadata: { created_at: 10 },
    });
    // Legacy timestamp range filters map onto created_at metadata parsed from
    // the naive capture timestamp.
    expect(state.screenActivity.upserts[0][0]).toMatchObject({
      namespace,
      metadata: {
        created_at: Math.floor(Date.parse("2026-01-02T03:04:05.000Z") / 1000),
      },
    });

    // Closing the workstream and blanking the OCR text retracts both.
    state.database.database
      .prepare("UPDATE cf_workstreams SET status = 'archived', updated_at = 20")
      .run();
    state.database.database
      .prepare("UPDATE cf_screen_activity SET ocr_text = '', updated_at = 20")
      .run();
    await reconcileVectorProjections(state.env, 200);
    await reconcileVectorProjections(state.env, 300);
    expect(state.workstream.deletes.flat()).toHaveLength(1);
    expect(state.screenActivity.deletes.flat()).toHaveLength(1);
    expect(
      state.database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_vector_projection_state WHERE projection_kind IN ('workstream', 'screen_activity')",
        )
        .get(),
    ).toEqual({ count: 0 });
  });

  it("reprojects changed content and deletes derived vectors after source deletion", async () => {
    const state = environment();
    seedSources(state.database);
    await reconcileVectorProjections(state.env, 100);
    const originalId = String(state.memory.upserts[0][0].id);

    state.database.database
      .prepare(
        "UPDATE cf_memories SET content = 'Prefers oolong tea', updated_at = 20 WHERE uid = 'vector-user'",
      )
      .run();
    await reconcileVectorProjections(state.env, 200);
    expect(state.memory.upserts).toHaveLength(2);
    // Vectorize replaces bytes for an existing ID (official client API).
    // Every publication must therefore use an immutable attempt-specific ID.
    expect(state.memory.upserts[1][0].id).not.toBe(originalId);

    state.database.database
      .prepare(
        "UPDATE cf_memories SET deleted_at = 30, updated_at = 30 WHERE uid = 'vector-user'",
      )
      .run();
    await reconcileVectorProjections(state.env, 300);
    expect(state.memory.deletes.flat()).toContain(originalId);
    expect(
      state.database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
        )
        .get(),
    ).toEqual({ count: 0 });
  });

  it("preserves a newer same-second revision while an embedding is in flight", async () => {
    const state = environment();
    seedSources(state.database);
    let changed = false;
    state.ai.run.mockImplementation(async (_model, input) => {
      const texts = input.text as string[];
      if (!changed && texts.includes("Prefers green tea")) {
        changed = true;
        state.database.database
          .prepare(
            "UPDATE cf_memories SET content = 'Prefers oolong tea' WHERE id = 'memory-1'",
          )
          .run();
      }
      return {
        data: texts.map(() => Array(VECTOR_EMBEDDING_DIMENSIONS).fill(0.01)),
      };
    });
    await reconcileVectorProjections(state.env, 100);
    const current = state.database.database
      .prepare(
        "SELECT item_revision, updated_at FROM cf_memories WHERE id = 'memory-1'",
      )
      .get()!;
    expect(current.updated_at).toBe(10);
    expect(
      state.database.database
        .prepare(
          "SELECT desired_version FROM cf_vector_projection_outbox WHERE source_kind = 'memory'",
        )
        .get(),
    ).toEqual({ desired_version: current.item_revision });
    await reconcileVectorProjections(state.env, 200);
    expect(
      state.ai.run.mock.calls.some(([, input]) =>
        (input.text as string[]).includes("Prefers oolong tea"),
      ),
    ).toBe(true);
    expect(
      state.database.database
        .prepare(
          "SELECT DISTINCT source_version FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
        )
        .all(),
    ).toEqual([{ source_version: current.item_revision }]);
    expect(
      state.database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_vector_projection_outbox WHERE source_kind = 'memory'",
        )
        .get(),
    ).toEqual({ count: 0 });
  });

  it("indexes an eligible unreviewed memory and removes it when its canonical source becomes restricted", async () => {
    const state = environment();
    seedSources(state.database);
    state.database.database
      .prepare(
        "UPDATE cf_memories SET reviewed = 0, user_review = NULL WHERE id = 'memory-1'",
      )
      .run();
    await reconcileVectorProjections(state.env, 100);
    expect(state.memory.upserts).toHaveLength(1);
    state.database.database
      .prepare(
        `UPDATE cf_memories SET sensitivity_labels_json = '["credential"]' WHERE id = 'memory-1'`,
      )
      .run();
    await reconcileVectorProjections(state.env, 200);
    expect(state.memory.deletes.flat()).toContain(
      state.memory.upserts[0][0].id,
    );
    expect(
      state.database.database
        .prepare(
          "SELECT COUNT(*) AS count FROM cf_vector_projection_outbox WHERE source_kind = 'memory'",
        )
        .get(),
    ).toEqual({ count: 0 });
  });

  it("reprojects unchanged source rows when the embedding model changes", async () => {
    const state = environment();
    seedSources(state.database);
    await reconcileVectorProjections(state.env, 100);
    state.memory.upserts.length = 0;
    state.database.database
      .prepare(
        "UPDATE cf_vector_projection_state SET model = 'retired-model' WHERE projection_kind = 'memory'",
      )
      .run();

    await reconcileVectorProjections(state.env, 200);

    expect(state.memory.upserts).toHaveLength(1);
    expect(
      state.database.database
        .prepare(
          "SELECT model FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
        )
        .get(),
    ).toEqual({ model: VECTOR_EMBEDDING_MODEL });
  });

  it("keeps a durable retry row when Workers AI is unavailable", async () => {
    const state = environment({ failAi: true });
    seedSources(state.database);

    await expect(reconcileVectorProjections(state.env, 100)).resolves.toBe(0);

    expect(
      state.database.database
        .prepare(
          "SELECT attempts, last_error FROM cf_vector_projection_outbox ORDER BY source_kind LIMIT 1",
        )
        .get(),
    ).toEqual({ attempts: 1, last_error: "vector projection unavailable" });
    expect(
      state.database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_vector_projection_state")
        .get(),
    ).toEqual({ count: 0 });
  });

  it("purges every recorded vector before account D1 cleanup", async () => {
    const state = environment();
    seedSources(state.database);
    await reconcileVectorProjections(state.env, 100);
    const expectedMemoryId = state.memory.upserts[0][0].id;

    await expect(
      purgeAccountVectorProjections(state.env, "vector-user"),
    ).resolves.toBe(1);
    // Memory deletion is asynchronous and keeps its journal until observed.
    // Only then may the account owner drain the other four projection kinds.
    await expect(
      purgeAccountVectorProjections(state.env, "vector-user"),
    ).resolves.toBe(4);
    await expect(
      purgeAccountVectorProjections(state.env, "vector-user"),
    ).resolves.toBe(0);

    expect(state.memory.deletes.flat()).toContain(expectedMemoryId);
    expect(
      state.database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_vector_projection_state")
        .get(),
    ).toEqual({ count: 0 });
  });
});
