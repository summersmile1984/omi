import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { memoryPrivacyReceiptId } from "../workers/shared/memory-privacy-receipts";
import { cleanupExpiredMemoryPrivacyReceipts } from "../workers/jobs/memory-privacy";
import type { JobsEnv } from "../workers/jobs/env";

const examples = JSON.parse(
  readFileSync(
    new URL("fixtures/memory-privacy-receipts.json", import.meta.url),
    "utf8",
  ),
);

describe("canonical privacy receipts", () => {
  it("matches the original Python owner's shared Unicode fixtures", async () => {
    for (const example of examples)
      expect(
        await memoryPrivacyReceiptId(
          example.secret,
          example.uid,
          example.memory_id,
        ),
      ).toBe(example.receipt_id);
    expect(
      new Set(examples.map((row: { receipt_id: string }) => row.receipt_id))
        .size,
    ).toBe(examples.length);
  });

  it("refuses absent keys and ambiguous identities", async () => {
    for (const secret of [undefined, "", "x".repeat(31)])
      await expect(
        memoryPrivacyReceiptId(secret, "owner", "memory"),
      ).rejects.toThrow("secret is unavailable");
    await expect(
      memoryPrivacyReceiptId(examples[0].secret, "owner\nother", "memory"),
    ).rejects.toThrow("invalid memory privacy identity");
  });

  it("scheduled cleanup removes only expired receipts", async () => {
    const db = new DatabaseSync(":memory:");
    try {
      const directory = new URL("../migrations/app/", import.meta.url);
      for (const file of readdirSync(directory)
        .filter((name) => name.endsWith(".sql"))
        .sort())
        db.exec(readFileSync(new URL(file, directory), "utf8"));
      for (const [index, example] of examples.entries()) {
        db.prepare(
          "INSERT INTO cf_memories (uid,id,content,memory_tier,valid_at,created_at,updated_at,privacy_receipt_id,status,source_state,deleted_at) VALUES (?,?,NULL,'short_term',1,1,1,?,'tombstoned','tombstoned',1)",
        ).run(example.uid, example.memory_id, example.receipt_id);
        db.prepare(
          "INSERT INTO cf_memory_privacy_receipts VALUES (?,?,?,?)",
        ).run(example.uid, example.receipt_id, index, index + 2592000);
      }
      const env = {
        APP_DB: {
          prepare: (sql: string) => ({
            bind: (now: number) => ({
              run: async () => db.prepare(sql).run(now),
            }),
          }),
        },
      } as unknown as JobsEnv;
      await cleanupExpiredMemoryPrivacyReceipts(env, 2592001);
      expect(
        db.prepare("SELECT receipt_id FROM cf_memory_privacy_receipts").all(),
      ).toEqual([{ receipt_id: examples[2].receipt_id }]);
      expect(
        db.prepare("SELECT count(*) AS count FROM cf_memories").get(),
      ).toEqual({ count: 3 });
    } finally {
      db.close();
    }
  });
});
