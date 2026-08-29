import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobsEnv } from "../workers/jobs/env";
import {
  purgeAccountVectorProjections,
  reconcileVectorProjections,
  VECTOR_EMBEDDING_DIMENSIONS,
  VECTOR_EMBEDDING_MODEL,
  vectorId,
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

  constructor() {
    this.database.exec("PRAGMA foreign_keys = ON");
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

  async upsert(vectors: Array<Record<string, unknown>>) {
    this.upserts.push(vectors);
    return { mutationId: crypto.randomUUID() };
  }

  async deleteByIds(ids: string[]) {
    this.deletes.push(ids);
    return { mutationId: crypto.randomUUID() };
  }
}

const databases: SqliteD1[] = [];

function environment(options: { failAi?: boolean } = {}) {
  const database = new SqliteD1();
  databases.push(database);
  const memory = new FakeVectorize();
  const action = new FakeVectorize();
  const conversation = new FakeVectorize();
  const transcript = new FakeVectorize();
  const xPost = new FakeVectorize();
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

afterEach(() => {
  for (const database of databases.splice(0)) database.close();
});

describe("Vectorize rebuildable D1 projection", () => {
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
    expect(state.memory.upserts[1][0].id).toBe(originalId);

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
    const expectedMemoryId = await vectorId(
      "memory",
      "vector-user",
      "memory-1",
      "000000",
    );

    await expect(
      purgeAccountVectorProjections(state.env, "vector-user"),
    ).resolves.toBe(5);

    expect(state.memory.deletes.flat()).toContain(expectedMemoryId);
    expect(
      state.database.database
        .prepare("SELECT COUNT(*) AS count FROM cf_vector_projection_state")
        .get(),
    ).toEqual({ count: 0 });
  });
});
