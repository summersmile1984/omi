import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import {
  openRecording,
  writeRecordingSegments,
  mergeRecordingSegments,
  recordingSessionEvent,
} from "../workers/realtime/recording-store";

const databases: DatabaseSync[] = [];
afterEach(() => databases.splice(0).forEach((database) => database.close()));
function fixture() {
  const database = new DatabaseSync(":memory:");
  databases.push(database);
  database.exec("PRAGMA foreign_keys = ON");
  const migrations = resolve(
    dirname(fileURLToPath(import.meta.url)),
    "../migrations/app",
  );
  for (const name of readdirSync(migrations)
    .filter((name) => name.endsWith(".sql"))
    .sort())
    database.exec(readFileSync(resolve(migrations, name), "utf8"));
  const statement = (sql: string, args: unknown[] = []): any => ({
    sql,
    args,
    bind: (...args: unknown[]) => statement(sql, args),
    first: async () => database.prepare(sql).get(...(args as never[])),
    run: async () => ({
      meta: {
        changes: Number(
          database.prepare(sql).run(...(args as never[])).changes,
        ),
      },
    }),
  });
  const d1 = {
    prepare: statement,
    batch: async (statements: any[]) => {
      database.exec("BEGIN IMMEDIATE");
      try {
        const results = statements.map(({ sql, args }) => {
          const query = database.prepare(sql);
          return query.columns().length
            ? { results: query.all(...args), meta: { changes: 0 } }
            : {
                results: [],
                meta: { changes: Number(query.run(...args).changes) },
              };
        });
        database.exec("COMMIT");
        return results;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    },
  } as unknown as D1Database;
  const input = {
    uid: "recording-user",
    sessionId: "11111111-1111-4111-8111-111111111111",
    source: "web",
    language: "en",
  };
  return { database, d1, input };
}
const segment = {
  text: "I prefer concise updates.",
  start: 0,
  end: 2,
  speaker: "SPEAKER_00",
  is_user: true,
};

describe("D1 recording identity and transcript ownership", () => {
  it("persists a new session, resumes its transcript and fences the old connection", async () => {
    const { database, d1, input } = fixture();
    const first = await openRecording(d1, input);
    await writeRecordingSegments(d1, first, [segment]);
    const resumed = await openRecording(d1, input);
    expect(resumed.conversation_id).toBe(input.sessionId);
    expect(JSON.parse(resumed.transcript_segments_json)).toEqual([segment]);
    expect(recordingSessionEvent(resumed)).toMatchObject({
      type: "conversation_session",
      status: "in_progress",
      lifecycle_phase: "in_progress",
      lifecycle_version: 1,
      conversation_id: input.sessionId,
      recording_session_id: input.sessionId,
      lifecycle_sequence: 2,
    });
    await expect(writeRecordingSegments(d1, first, [])).rejects.toThrow(
      "ownership",
    );
    expect(
      database.prepare("SELECT COUNT(*) AS count FROM cf_conversations").get(),
    ).toMatchObject({ count: 1 });
  });

  it("scopes an identical client recording ID to its authenticated user", async () => {
    const { database, d1, input } = fixture();
    const one = await openRecording(d1, input);
    const two = await openRecording(d1, { ...input, uid: "another-user" });
    await writeRecordingSegments(d1, one, [segment]);
    expect(JSON.parse(two.transcript_segments_json)).toEqual([]);
    expect(
      database.prepare("SELECT COUNT(*) AS count FROM cf_conversations").get(),
    ).toMatchObject({ count: 2 });
  });

  it.each(["completed", "deleted"])(
    "never reopens a %s conversation generation",
    async (terminal) => {
      const { database, d1, input } = fixture();
      const first = await openRecording(d1, input);
      if (terminal === "completed")
        database
          .prepare(
            "UPDATE cf_conversations SET status = 'completed' WHERE uid = ? AND id = ?",
          )
          .run(input.uid, first.conversation_id);
      else
        database
          .prepare("DELETE FROM cf_conversations WHERE uid = ? AND id = ?")
          .run(input.uid, first.conversation_id);
      const next = await openRecording(d1, input);
      expect(next.conversation_id).not.toBe(first.conversation_id);
      expect(next.recording_session_id).toBe(next.conversation_id);
      await expect(
        writeRecordingSegments(d1, first, [segment]),
      ).rejects.toThrow("ownership");
      expect(
        database
          .prepare(
            "SELECT conversation_id FROM cf_live_recording_sessions WHERE uid = ? AND recording_session_id = ?",
          )
          .get(input.uid, input.sessionId),
      ).toMatchObject({ conversation_id: first.conversation_id });
    },
  );

  it("refuses new capture and stale transcript writes after the account deletion fence", async () => {
    const { database, d1, input } = fixture();
    const binding = await openRecording(d1, input);
    database
      .prepare(
        "INSERT INTO cf_account_deletion_tombstones (uid, completed_at, expires_at) VALUES (?, 1, 9999999999)",
      )
      .run(input.uid);
    await expect(openRecording(d1, input)).rejects.toThrow(
      "account deletion fence",
    );
    await expect(
      writeRecordingSegments(d1, binding, [segment]),
    ).rejects.toThrow("account deletion fence");
    expect(
      database
        .prepare(
          "SELECT transcript_segments_json FROM cf_conversations WHERE uid = ?",
        )
        .get(input.uid),
    ).toMatchObject({ transcript_segments_json: "[]" });
  });

  it("adopts an existing in-progress conversation without replacing its content", async () => {
    const { database, d1, input } = fixture();
    database
      .prepare(
        "INSERT INTO cf_conversations (uid, id, created_at, status, transcript_segments_json) VALUES (?, ?, 1, 'in_progress', ?)",
      )
      .run(input.uid, input.sessionId, JSON.stringify([segment]));
    const binding = await openRecording(d1, input);
    expect(JSON.parse(binding.transcript_segments_json)).toEqual([segment]);
    expect(binding.conversation_id).toBe(input.sessionId);
  });

  it("deduplicates provider replay and refuses capacity overflow without dropping committed text", () => {
    expect(mergeRecordingSegments([segment], [segment])).toEqual([segment]);
    expect(
      mergeRecordingSegments(
        [segment],
        [{ ...segment, text: "Corrected fact" }],
      )[0].text,
    ).toBe("Corrected fact");
    expect(() =>
      mergeRecordingSegments(
        [segment],
        [{ ...segment, start: 3, end: 4, text: "a".repeat(500000) }],
      ),
    ).toThrow("capacity");
  });
});
