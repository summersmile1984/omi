/** Real migration SQL plus Queue/Core boundaries; no hosted Queue claim. */
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Message, MessageBatch } from "@cloudflare/workers-types";
import jobs from "../workers/jobs/index";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import { processMemoryConsolidationMessage, reconcileMemoryConsolidation } from "../workers/jobs/memory-consolidation";
import { verifyRequestAuthContext } from "../workers/shared/auth-context";

const cleanup: Array<() => void> = [];
afterEach(() => { for (const close of cleanup.splice(0)) close(); vi.restoreAllMocks(); });

function setup() {
  const db = new DatabaseSync(":memory:");
  cleanup.push(() => db.close());
  const now = 1788793200;
  vi.spyOn(Date, "now").mockReturnValue(now * 1000);
  db.function("unixepoch", () => now);
  const directory = fileURLToPath(new URL("../migrations/app/", import.meta.url));
  for (const file of readdirSync(directory).filter(f => f.endsWith(".sql")).sort()) {
    db.exec(readFileSync(path.join(directory, file), "utf8"));
  }
  const database = {
    prepare(sql: string) {
      const build = (args: unknown[] = []) => ({
        bind: (...values: unknown[]) => build(values),
        first: async () => db.prepare(sql).get(...args as never[]) ?? null,
        all: async () => ({ results: db.prepare(sql).all(...args as never[]), success: true }),
        run: async () => ({ meta: { changes: Number(db.prepare(sql).run(...args as never[]).changes) }, success: true }),
      });
      return build();
    },
  };
  const sent: Array<{ body: JobMessage; options?: unknown }> = [];
  const events: string[] = [];
  const env = {
    APP_DB: database,
    INTERNAL_ASSERTION_SECRET: "consolidation-dispatch-test-secret",
    JOBS: { send: vi.fn(async (body: JobMessage, options?: unknown) => { sent.push({body, options}); events.push("send"); }) },
    API_CORE: { fetch: vi.fn(async () => Response.json({ pending: false, retry_after_seconds: 0 })) },
  } as unknown as JobsEnv;
  const work = (uid = "owner", generation = 0, due = now) => db.prepare("INSERT INTO cf_memory_consolidation_dispatch(uid,account_generation,next_attempt_at) VALUES (?,?,?)").run(uid, generation, due);
  const message = (uid = "owner", generation = 0) => ({
    id: `delivery-${uid}`, timestamp: new Date(now * 1000), attempts: 3,
    body: { uid, jobId: `memory-consolidation:${generation}`, kind: "memory_consolidate", payload: {} },
    ack: vi.fn(() => events.push("ack")), retry: vi.fn(() => events.push("retry")),
  } as Message<JobMessage>);
  return { db, env, now, sent, events, work, message };
}

describe("durable memory consolidation Queue dispatch", () => {
  it("rediscovers unsent D1 work and continues other accounts after one send fails", async () => {
    const t = setup(); t.work("a"); t.work("b");
    vi.mocked(t.env.JOBS.send).mockRejectedValueOnce(new Error("controlled queue outage"));
    await expect(reconcileMemoryConsolidation(t.env, t.now)).rejects.toThrow("queue unavailable");
    expect(t.sent.map(x => x.body.uid)).toEqual(["b"]);
    await reconcileMemoryConsolidation(t.env, t.now);
    expect(t.sent.map(x => x.body.uid)).toEqual(["b", "a", "b"]);
  });

  it("normal continuation sends before ack and never exhausts Queue retry attempts", async () => {
    const t = setup(); t.work();
    vi.mocked(t.env.API_CORE!.fetch).mockImplementation(async request => {
      const r = request as Request;
      const context = await verifyRequestAuthContext(r, "api-core", t.env.INTERNAL_ASSERTION_SECRET);
      expect(context?.uid).toBe("owner");
      expect(context?.authority).toBe("internal");
      expect(r.method).toBe("POST");
      expect(new URL(r.url).pathname).toBe("/internal/memory/consolidation");
      expect(await r.json()).toEqual({ account_generation: 0 });
      return Response.json({ pending: true, retry_after_seconds: 5 });
    });
    for (let i = 0; i < 6; i++) {
      const message = t.message();
      await processMemoryConsolidationMessage(message, t.env);
      expect(message.retry).not.toHaveBeenCalled();
      expect(t.events.slice(-2)).toEqual(["send", "ack"]);
      expect(t.sent.at(-1)?.options).toEqual({delaySeconds: 5});
    }
    expect(t.env.API_CORE!.fetch).toHaveBeenCalledTimes(6);
  });

  it("failed handoff leaves delivery unacknowledged for recovery", async () => {
    const t = setup(); t.work();
    vi.mocked(t.env.API_CORE!.fetch).mockResolvedValue(Response.json({pending: true, retry_after_seconds: 0}));
    vi.mocked(t.env.JOBS.send).mockRejectedValue(new Error("send failed"));
    const m = t.message();
    await jobs.queue({queue: "eddy-jobs-production", messages: [m]} as unknown as MessageBatch<JobMessage>, t.env);
    expect(m.ack).not.toHaveBeenCalled();
    expect(m.retry).toHaveBeenCalledOnce();
    expect(t.db.prepare("SELECT handled_sequence FROM cf_memory_consolidation_dispatch").get()).toEqual({handled_sequence: 0});
  });

  it("delays busy work without invoking Core and ignores obsolete or erased accounts", async () => {
    const t = setup(); t.work("busy", 0, t.now + 15); t.work("old"); t.work("deleted");
    t.db.exec(`INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('old',1,${t.now});`);
    t.db.prepare("INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES ('deleted',?,?)").run(t.now, t.now + 100);
    for (const uid of ["busy", "old", "deleted"]) {
      const m = t.message(uid);
      await processMemoryConsolidationMessage(m, t.env);
      expect(m.ack).toHaveBeenCalledOnce();
    }
    expect(t.env.API_CORE!.fetch).not.toHaveBeenCalled();
    expect(t.sent.map(x=>x.body.uid)).toEqual(["busy"]);
    expect(t.sent[0].options).toEqual({delaySeconds: 15});
    await reconcileMemoryConsolidation(t.env, t.now);
    expect(t.sent).toHaveLength(1);
  });

  it("runs independent account model waits concurrently within a Queue batch", async () => {
    const t = setup(); t.work("a"); t.work("b");
    const entered: string[] = [];
    const release: Array<(response: Response) => void> = [];
    cleanup.unshift(() => { for (const resolve of release) resolve(Response.json({pending:false,retry_after_seconds:0})); });
    vi.mocked(t.env.API_CORE!.fetch).mockImplementation(async request => {
      const context = await verifyRequestAuthContext(request as Request, "api-core", t.env.INTERNAL_ASSERTION_SECRET);
      entered.push(context!.uid);
      const response = new Promise<Response>(resolve => release.push(resolve));
      if (entered.length === 2) for (const resolve of release) resolve(Response.json({pending:false,retry_after_seconds:0}));
      return response;
    });
    const messages = [t.message("a"), t.message("b")];
    await jobs.queue({queue:"eddy-jobs-production",messages} as unknown as MessageBatch<JobMessage>, t.env);
    expect(entered.sort()).toEqual(["a", "b"]);
    for (const m of messages) expect(m.ack).toHaveBeenCalledOnce();
  });

  it("retains consolidation deliveries as replayable DLQ records", async () => {
    const t = setup(); t.work();
    const m = t.message();
    await jobs.queue({queue:"omi-cf-jobs-dlq-production",messages:[m]} as unknown as MessageBatch<JobMessage>, t.env);
    expect(m.ack).toHaveBeenCalledOnce();
    expect(t.db.prepare("SELECT kind,status,invalid_reason FROM cf_queue_dlq_messages").get()).toEqual({kind:"memory_consolidate",status:"captured",invalid_reason:null});
  });
});
