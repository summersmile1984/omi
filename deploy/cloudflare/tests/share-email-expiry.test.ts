import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { expireShareEmailDispatches } from "../workers/jobs/share-email";
import type { JobsEnv } from "../workers/jobs/env";

describe("share email scheduled recovery", () => {
  it("bounds prepared recovery, refunds only definite non-dispatch and retains unknown claims", async () => {
    const db = new DatabaseSync(":memory:");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const directory = fileURLToPath(
        new URL("../migrations/app/", import.meta.url)
      );
      for (const name of readdirSync(directory)
        .filter((n) => n.endsWith(".sql"))
        .sort())
        db.exec(readFileSync(directory + name, "utf8"));
      for (let i = 0; i < 102; i++) {
        const uid = "expiry-user-" + i,
          id = "expiry-conversation-" + i;
        db.prepare(
          "INSERT INTO cf_conversations(uid,id,created_at,visibility) VALUES (?,?,1,'shared')"
        ).run(uid, id);
        db.prepare(
          "INSERT INTO cf_shared_conversation_index VALUES (?,?,'shared',1)"
        ).run(id, uid);
        db.prepare(
          "INSERT INTO cf_share_email_quota VALUES (?,'20260906',1)"
        ).run(uid);
        db.prepare(
          "INSERT INTO cf_share_email_dispatches(id,uid,conversation_id,phase,quota_day,recipient_count,was_private,publish_revision,created_at,expires_at,payload_json) " +
            "VALUES (?,?,?,?,'20260906',1,1,0,1,2,'{}')"
        ).run(id, uid, id, i === 101 ? "dispatching" : "prepared");
        db.prepare(
          "INSERT INTO cf_share_email_recipients VALUES (?,?,'guest@example.invalid',?)"
        ).run(uid, id, id);
      }
      const env = {
        APP_DB: {
          prepare(sql: string) {
            return {
              bind(...args: unknown[]) {
                return {
                  execute: () => ({
                    meta: {
                      changes: Number(
                        db.prepare(sql).run(...(args as never[])).changes
                      ),
                    },
                  }),
                };
              },
            };
          },
          async batch(statements: Array<{ execute: () => unknown }>) {
            db.exec("BEGIN IMMEDIATE");
            try {
              const results = statements.map((s) => s.execute());
              db.exec("COMMIT");
              return results;
            } catch (error) {
              db.exec("ROLLBACK");
              throw error;
            }
          },
        },
      } as unknown as JobsEnv;
      await expireShareEmailDispatches(env, 100);
      expect(
        db
          .prepare(
            "SELECT phase,COUNT(*) AS count FROM cf_share_email_dispatches GROUP BY phase ORDER BY phase"
          )
          .all()
      ).toEqual([
        { phase: "ambiguous", count: 1 },
        { phase: "prepared", count: 1 },
        { phase: "rejected", count: 100 },
      ]);
      expect(
        db.prepare("SELECT SUM(used) AS count FROM cf_share_email_quota").get()
      ).toEqual({ count: 2 });
      expect(
        db
          .prepare("SELECT COUNT(*) AS count FROM cf_share_email_recipients")
          .get()
      ).toEqual({ count: 2 });
      expect(
        db
          .prepare("SELECT COUNT(*) AS count FROM cf_shared_conversation_index")
          .get()
      ).toEqual({ count: 2 });
      expect(
        db
          .prepare(
            "SELECT COUNT(*) AS count FROM cf_share_email_dispatches WHERE payload_json IS NOT NULL"
          )
          .get()
      ).toEqual({ count: 1 });
      expect(warn).toHaveBeenCalledTimes(1);
      expect(String(warn.mock.calls[0][0])).not.toContain("expiry-user");
    } finally {
      warn.mockRestore();
      db.close();
    }
  });
});
