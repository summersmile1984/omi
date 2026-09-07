import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { afterEach, expect, it } from "vitest";
import {
  cleanupFramePixels,
  framePixelResidual,
} from "../workers/jobs/frame-request-storage";
import type { JobsEnv } from "../workers/jobs/env";

const databases: DatabaseSync[] = [];
afterEach(() => databases.splice(0).forEach((db) => db.close()));

// Static portability tripwire, not SQL behavioral coverage. The hosted
// 2026-09-06 frame-flow migration failed with incomplete input despite SQLite
// accepting it: https://github.com/cloudflare/workers-sdk/issues/4727. Native
// memory apply migration 0172 reproduced that same failure later that day.
// The tests below and test_frame_request_pixels.py execute publication/erasure.
it.each([
  "0168_frame_request_pixels.sql",
  "0172_memory_apply_intake.sql",
  "0173_memory_apply_edit.sql",
])(
  "keeps trigger CASE expressions parenthesized for remote D1 in %s",
  (file) => {
    const migration = readFileSync(
      new URL(`../migrations/app/${file}`, import.meta.url),
      "utf8"
    );
    expect(migration).not.toMatch(/(?:SELECT\s+|=\s*)CASE\b/i);
  }
);

function fixture() {
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  const directory = new URL("../migrations/app/", import.meta.url);
  for (const name of readdirSync(directory)
    .filter((n) => n.endsWith(".sql"))
    .sort())
    db.exec(readFileSync(new URL(name, directory), "utf8"));
  const prepare = (sql: string) => {
    const build = (args: Array<string | number | null> = []) => ({
      bind: (...values: Array<string | number | null>) => build(values),
      async first() {
        return db.prepare(sql).get(...args) ?? null;
      },
      async all() {
        return {
          success: true,
          results: db.prepare(sql).all(...args),
          meta: { changes: 0 },
        };
      },
      async run() {
        return {
          success: true,
          meta: { changes: Number(db.prepare(sql).run(...args).changes) },
        };
      },
    });
    return build();
  };
  const bucket = () => {
    const objects = new Set<string>(),
      handles = new Map<string, { aborted: boolean }>();
    let failures = 0;
    return {
      objects,
      handles,
      failDelete() {
        failures++;
      },
      resumeMultipartUpload(key: string, id: string) {
        return {
          async abort() {
            const h = handles.get(id);
            if (h) h.aborted = true;
          },
          async complete() {
            if (handles.get(id)?.aborted) throw Error("NoSuchUpload (10024)");
            objects.add(key);
          },
        };
      },
      async delete(key: string) {
        if (failures-- > 0) throw Error("unavailable");
        objects.delete(key);
      },
      async list({ prefix }: { prefix: string }) {
        return {
          objects: [...objects]
            .filter((k) => k.startsWith(prefix))
            .map((key) => ({ key })),
        };
      },
    };
  };
  const temporary = bucket(),
    permanent = bucket(),
    now = Math.floor(Date.now() / 1000);
  const env = {
    APP_DB: { prepare },
    FRAME_REQUESTS: permanent,
    FRAME_REQUESTS_TEMPORARY: temporary,
  } as unknown as JobsEnv;
  function request(
    uid: string,
    id: string,
    conversation: string | null = null
  ) {
    if (conversation)
      db.prepare(
        "INSERT INTO cf_conversations(uid,id,created_at) VALUES (?,?,?)"
      ).run(uid, conversation, now);
    db.prepare(
      "INSERT INTO cf_frame_requests(uid,request_id,device_id,account_generation,dedupe_key,dedupe_window,attempt_number,conversation_id,state,created_at,expires_at) VALUES (?,?,'desktop',0,?,1,0,?,'claimed',?,?)"
    ).run(uid, id, id.padEnd(64, "x"), conversation, now, now + 500);
  }
  let n = 0;
  function object(
    uid: string,
    requestId: string,
    tier: "temporary" | "permanent",
    publish = false
  ) {
    const id = String(++n).padStart(32, "0"),
      storage = tier + "-" + requestId,
      key = `frame-requests/${uid}/${id}.jpg`,
      upload = "upload-" + id;
    const target = tier === "temporary" ? temporary : permanent;
    db.prepare(
      "INSERT INTO cf_frame_objects(object_id,uid,request_id,storage_id,tier,account_generation,authority_snapshot,sha256,byte_count,created_at,write_expires_at) VALUES (?,?,?,?,?,0,'[null,null,1,0,0]',?,6,?,?)"
    ).run(id, uid, requestId, storage, tier, "a".repeat(64), now, now + 120);
    db.prepare("UPDATE cf_frame_objects SET upload_id=? WHERE object_id=?").run(
      upload,
      id
    );
    target.handles.set(upload, { aborted: false });
    target.objects.add(key);
    if (publish) {
      db.prepare(
        "UPDATE cf_frame_objects SET phase='ready' WHERE object_id=?"
      ).run(id);
      db.prepare(
        "UPDATE cf_frame_objects SET phase='live' WHERE object_id=?"
      ).run(id);
    }
    return { id, key, upload, target };
  }
  return { db, env, temporary, permanent, now, request, object };
}

it("aborts a registered upload before erasure and fences a late completion", async () => {
  const f = fixture();
  f.request("owner", "frame-1");
  const o = f.object("owner", "frame-1", "temporary");
  expect(await cleanupFramePixels(f.env, "owner", f.now + 121)).toBe(1);
  expect(o.target.handles.get(o.upload)?.aborted).toBe(true);
  await expect(
    o.target.resumeMultipartUpload(o.key, o.upload).complete()
  ).rejects.toThrow("NoSuchUpload");
  expect(o.target.objects.has(o.key)).toBe(false);
  expect((await framePixelResidual(f.env, "owner")).empty).toBe(true);
});

it("keeps the deletion receipt across R2 failure and retries physical erasure", async () => {
  const f = fixture();
  f.request("owner", "frame-1");
  const o = f.object("owner", "frame-1", "temporary");
  f.db
    .prepare("INSERT INTO cf_account_deletion_tombstones VALUES (?,?,?)")
    .run("owner", f.now, f.now + 1000);
  f.temporary.failDelete();
  expect(await cleanupFramePixels(f.env, "owner", f.now + 121)).toBe(0);
  expect(
    f.db.prepare("SELECT phase,attempts FROM cf_frame_objects").get()
  ).toMatchObject({ phase: "cleanup", attempts: 1 });
  expect((await framePixelResidual(f.env, "owner")).empty).toBe(false);
  expect(await cleanupFramePixels(f.env, "owner", f.now + 122)).toBe(0);
  expect(o.target.objects.has(o.key)).toBe(true);
  expect(
    f.db.prepare("SELECT attempts,next_attempt_at FROM cf_frame_objects").get()
  ).toMatchObject({ attempts: 1, next_attempt_at: f.now + 151 });
  expect(await cleanupFramePixels(f.env, "owner", f.now + 151)).toBe(1);
  expect(o.target.objects.has(o.key)).toBe(false);
  expect((await framePixelResidual(f.env, "owner")).empty).toBe(true);
});

it("expires temporary metadata while retaining permanent evidence until conversation deletion", async () => {
  const f = fixture();
  f.request("owner", "frame-1", "meeting");
  f.object("owner", "frame-1", "temporary", true);
  const permanent = f.object("owner", "frame-1", "permanent", true);
  f.request("owner", "frame-2");
  const temporary = f.object("owner", "frame-2", "temporary", true);
  await cleanupFramePixels(f.env, "owner", f.now + 600);
  expect(f.temporary.objects.size).toBe(0);
  expect(temporary.target.handles.get(temporary.upload)?.aborted).toBe(true);
  expect(f.permanent.objects.has(permanent.key)).toBe(true);
  expect(f.db.prepare("SELECT state FROM cf_frame_requests").get()?.state).toBe(
    "attached"
  );
  f.db.exec("DELETE FROM cf_conversations WHERE uid='owner' AND id='meeting'");
  expect((await framePixelResidual(f.env, "owner")).empty).toBe(false);
  await cleanupFramePixels(f.env, "owner", f.now + 601);
  expect((await framePixelResidual(f.env, "owner")).empty).toBe(true);
});

it("account deletion cleans both tiers without touching another account", async () => {
  const f = fixture();
  for (const uid of ["owner", "other"]) {
    f.request(uid, "frame-" + uid, "meeting");
    f.object(uid, "frame-" + uid, "temporary", true);
    f.object(uid, "frame-" + uid, "permanent", true);
  }
  f.db
    .prepare("INSERT INTO cf_account_deletion_tombstones VALUES (?,?,?)")
    .run("owner", f.now, f.now + 1000);
  await cleanupFramePixels(f.env, "owner", f.now);
  expect((await framePixelResidual(f.env, "owner")).empty).toBe(true);
  expect((await framePixelResidual(f.env, "other")).empty).toBe(false);
  expect([...f.permanent.objects].every((key) => key.includes("/other/"))).toBe(
    true
  );
});

it("legacy accounts with no frame journals can be audited before bucket enrollment", async () => {
  const f = fixture();
  delete f.env.FRAME_REQUESTS;
  delete f.env.FRAME_REQUESTS_TEMPORARY;
  expect((await framePixelResidual(f.env, "legacy")).empty).toBe(true);
  f.request("owner", "frame-1");
  f.object("owner", "frame-1", "temporary");
  await expect(framePixelResidual(f.env, "owner")).rejects.toThrow(
    "storage unavailable"
  );
});

it("reports failed pixel cleanup on the original frame envelope until retry succeeds", async () => {
  const f = fixture();
  f.request("owner", "frame-1");
  f.object("owner", "frame-1", "temporary", true);
  f.db.exec(
    "UPDATE cf_frame_requests SET state='failed',terminal_reason='device_failure',cleanup_state='pending'"
  );
  f.temporary.failDelete();
  await cleanupFramePixels(f.env, "owner", f.now);
  expect(
    f.db
      .prepare(
        "SELECT cleanup_state,cleanup_attempts,cleanup_next_attempt_at FROM cf_frame_requests"
      )
      .get()
  ).toMatchObject({
    cleanup_state: "failed",
    cleanup_attempts: 1,
    cleanup_next_attempt_at: f.now + 30,
  });
  await cleanupFramePixels(f.env, "owner", f.now + 30);
  expect(
    f.db.prepare("SELECT cleanup_state FROM cf_frame_requests").get()
      ?.cleanup_state
  ).toBe("deleted");
});
