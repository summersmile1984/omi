import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it } from "vitest";
import { app } from "../workers/screen-frame-writer/index";
import {
  APPROVAL_PURPOSE,
  CONTENT_PURPOSE,
  objectKeys,
  sha256,
  type Approval,
} from "../workers/screen-frame-writer/approval";
import {
  cleanup,
  residual,
  writeApproved,
  type WriterEnv,
} from "../workers/screen-frame-writer/storage";
import { screenFrameStorageState } from "../workers/jobs/screen-frame-storage";
import {
  AUTH_CONTEXT_HEADER,
  AUTH_SIGNATURE_HEADER,
  createSignedAuthContext,
} from "../workers/shared/auth-context";

const databases: DatabaseSync[] = [];
afterEach(() => {
  for (const db of databases.splice(0)) db.close();
});
const secret = "screen-frame-test-key-".repeat(3);
const internalSecret = "screen-frame-internal-test-key-".repeat(2);

it("expires bounded attempt receipts without erasing live or other-owner attempts", async () => {
  const f = await fixture();
  f.db.exec(
    "INSERT INTO cf_conversations(uid,id,created_at) VALUES ('other','meeting',1); INSERT INTO cf_screen_frame_sets(uid,conversation_id) VALUES ('other','meeting')"
  );
  for (const uid of [f.p.uid, "other"]) {
    for (let i = 0; i < 130; i++) {
      f.db
        .prepare(
          "INSERT INTO cf_screen_frame_attempts(uid,conversation_id,attempt_id,fingerprint,epoch,expires_at) VALUES (?,'meeting',?, ?,0,1)"
        )
        .run(uid, `expired-${i}`, "a".repeat(64));
    }
  }
  const count = (uid: string) =>
    f.db
      .prepare("SELECT count(*) AS n FROM cf_screen_frame_attempts WHERE uid=?")
      .get(uid)?.n;
  await cleanup(f.env, f.p.uid);
  expect(count(f.p.uid)).toBe(3);
  expect(count("other")).toBe(130);
  await cleanup(f.env, f.p.uid);
  expect(count(f.p.uid)).toBe(1);
  expect(
    f.db
      .prepare("SELECT attempt_id FROM cf_screen_frame_attempts WHERE uid=?")
      .get(f.p.uid)?.attempt_id
  ).toBe(f.p.attempt_id);
});
const jpeg = new Uint8Array([255, 216, 1, 2, 255, 217]);
const thumbnail = new Uint8Array([255, 216, 3, 255, 217]);
function token(payload: unknown, purpose = APPROVAL_PURPOSE, key = secret) {
  const body = `${purpose}.${Buffer.from(JSON.stringify(payload)).toString(
    "base64url"
  )}`;
  return `${body}.${createHmac("sha256", key)
    .update(body)
    .digest("base64url")}`;
}
function d1() {
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  db.exec("PRAGMA foreign_keys = ON");
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
  return { db, binding: { prepare } as unknown as D1Database };
}
type Stage = "initiated" | "uploadPart" | "complete" | "completed" | "get";
function bucket() {
  const objects = new Map<string, Uint8Array>();
  const uploads = new Map<
    string,
    { key: string; active: boolean; bytes?: Uint8Array }
  >();
  const state = {
    objects,
    uploads,
    parts: 0,
    abortError: null as Error | null,
    hook: async (_stage: Stage, _key: string) => {},
  };
  const handle = (key: string, uploadId: string) => ({
    key,
    uploadId,
    async uploadPart(partNumber: number, value: Uint8Array) {
      await state.hook("uploadPart", key);
      const upload = uploads.get(uploadId);
      if (!upload?.active)
        throw new Error("uploadPart: upload missing (10024)");
      upload.bytes = new Uint8Array(value);
      state.parts++;
      return { partNumber, etag: "test-part" };
    },
    async complete() {
      await state.hook("complete", key);
      const upload = uploads.get(uploadId);
      if (!upload?.active || !upload.bytes)
        throw new Error("complete: upload missing (10024)");
      upload.active = false;
      objects.set(key, upload.bytes);
      await state.hook("completed", key);
      return { key };
    },
    async abort() {
      if (state.abortError) throw state.abortError;
      const upload = uploads.get(uploadId);
      if (upload) {
        upload.active = false;
        delete upload.bytes;
      }
    },
  });
  const binding = {
    async createMultipartUpload(key: string) {
      const id = crypto.randomUUID();
      uploads.set(id, { key, active: true });
      await state.hook("initiated", key);
      return handle(key, id);
    },
    resumeMultipartUpload: handle,
    async delete(keys: string | string[]) {
      for (const key of Array.isArray(keys) ? keys : [keys])
        objects.delete(key);
    },
    async list({ prefix }: { prefix: string }) {
      return {
        objects: [...objects.keys()]
          .filter((k) => k.startsWith(prefix))
          .map((key) => ({ key })),
        truncated: false,
      };
    },
    async get(key: string) {
      const data = objects.get(key);
      await state.hook("get", key);
      return data ? { body: new Blob([Buffer.from(data)]).stream() } : null;
    },
    async head(key: string) {
      return objects.has(key) ? { key } : null;
    },
  } as unknown as R2Bucket;
  return { state, binding };
}
async function fixture(uid = "screen-owner") {
  const database = d1(),
    r2 = bucket();
  const now = Math.floor(Date.now() / 1000);
  const p: Approval = {
    version: 1,
    iss: "omi-screen-frame-adjudicator",
    aud: "omi-screen-frame-writer",
    jti: crypto.randomUUID(),
    uid,
    conversation_id: "meeting",
    attempt_id: crypto.randomUUID(),
    epoch: 0,
    purpose: "meeting_note_v1",
    retention: "with_subject",
    decision: "approved_clean",
    model: "@cf/qwen/qwen3.8-27b",
    policy_version: "test-policy",
    prompt_version: "test-prompt",
    issued_at: now,
    expires_at: now + 600,
    canonical_sha256: await sha256(jpeg),
    thumbnail_sha256: await sha256(thumbnail),
    metadata: {
      captured_at: new Date().toISOString(),
      caption: "Project discussion",
      labels: ["design"],
      source_badge: "document",
      banner_suitability: 0.8,
      width: 2,
      height: 2,
      ground: { stops: ["#111111", "#222222"], is_neutral: true },
    },
  };
  database.db
    .prepare(
      "INSERT INTO cf_conversations(uid, id, created_at, visibility) VALUES (?, 'meeting', ?, 'public')"
    )
    .run(uid, now);
  database.db
    .prepare(
      "INSERT INTO cf_screen_frame_sets(uid, conversation_id) VALUES (?, 'meeting')"
    )
    .run(uid);
  database.db
    .prepare(
      "INSERT INTO cf_screen_frame_attempts(uid, conversation_id, attempt_id, fingerprint, epoch, expires_at) VALUES (?, 'meeting', ?, ?, 0, ?)"
    )
    .run(uid, p.attempt_id, "a".repeat(64), now + 86400);
  const env: WriterEnv = {
    APP_DB: database.binding,
    SCREEN_FRAMES: r2.binding,
    SCREEN_FRAME_SIGNING_SECRET: secret,
    INTERNAL_ASSERTION_SECRET: internalSecret,
  };
  const request = (approval = token(p), image = jpeg) =>
    app.request(
      "/internal/screen-frames/write",
      {
        method: "POST",
        body: JSON.stringify({
          approval,
          jpeg_base64: Buffer.from(image).toString("base64"),
          thumbnail_base64: Buffer.from(thumbnail).toString("base64"),
        }),
      },
      env
    );
  const publish = () =>
    database.db
      .prepare("UPDATE cf_screen_frame_sets SET frames_json = ? WHERE uid = ?")
      .run(JSON.stringify([{ id: p.jti, ...p.metadata }]), uid);
  const fence = () =>
    database.db
      .prepare(
        "INSERT INTO cf_account_deletion_tombstones(uid, completed_at, expires_at) VALUES (?, ?, ?)"
      )
      .run(uid, now, now + 86400);
  const content = (access = "owner", owner = uid) =>
    app.request(
      `/v1/screen-frame-content?token=${token(
        {
          uid: owner,
          frame_id: p.jti,
          variant: "content",
          access,
          expires_at: now + 3600,
        },
        CONTENT_PURPOSE
      )}`,
      {},
      env
    );
  return {
    ...database,
    r2: r2.state,
    env,
    p,
    request,
    publish,
    fence,
    content,
  };
}

describe("isolated screenshot writer", () => {
  it("rejects a deployment that reuses the common assertion credential for image approval", async () => {
    const f = await fixture();
    expect((await app.request("/ready", {}, f.env)).status).toBe(200);
    f.env.SCREEN_FRAME_SIGNING_SECRET = internalSecret;
    expect((await app.request("/ready", {}, f.env)).status).toBe(503);
    expect(
      (await f.request(token(f.p, APPROVAL_PURPOSE, internalSecret))).status
    ).toBe(403);
    expect(f.r2.parts).toBe(0);
  });
  it("writes only the exact approved bytes once, preserves legacy enabled defaults and publishes through D1", async () => {
    const f = await fixture();
    expect((await f.request()).status).toBe(201);
    expect((await f.content()).status).toBe(404);
    f.publish();
    expect(new Uint8Array(await (await f.content()).arrayBuffer())).toEqual(
      jpeg
    );
    expect((await f.content("shared")).status).toBe(200);
    expect(f.r2.objects.get(objectKeys(f.p.uid, f.p.jti)[1])).toEqual(
      thumbnail
    );
    expect((await f.request()).status).toBe(503);
    expect(f.r2.parts).toBe(2);
  });
  it.each([
    "wrong-key",
    "expired",
    "rejected",
    "wrong-purpose",
    "wrong-model",
    "retired-model",
    "wrong-audience",
    "wrong-owner",
    "wrong-digest",
  ])("rejects %s before any image storage", async (kind) => {
    const f = await fixture();
    const p = { ...f.p };
    if (kind === "expired") {
      p.issued_at -= 601;
      p.expires_at -= 601;
    }
    if (kind === "rejected") Object.assign(p, { decision: "rejected" });
    if (kind === "wrong-model") Object.assign(p, { model: "other-model" });
    if (kind === "retired-model") Object.assign(p, { model: "gemini-2.5-flash-lite" });
    if (kind === "wrong-audience") Object.assign(p, { aud: "api-core" });
    if (kind === "wrong-owner") p.uid = "another-owner";
    const signed = token(
      p,
      kind === "wrong-purpose" ? CONTENT_PURPOSE : APPROVAL_PURPOSE,
      kind === "wrong-key" ? "other-key" : secret
    );
    expect(
      (await f.request(signed, kind === "wrong-digest" ? thumbnail : jpeg))
        .status
    ).toBeGreaterThanOrEqual(400);
    expect(f.r2.parts).toBe(0);
    expect(f.r2.uploads.size).toBe(0);
  });
  it.each(["initiated", "uploadPart", "complete", "completed"] as const)(
    "account erasure during %s prevents late publication and leaves no image bytes",
    async (stage) => {
      const f = await fixture();
      let triggered = false;
      f.r2.hook = async (event) => {
        if (event === stage && !triggered) {
          triggered = true;
          f.fence();
          await cleanup(f.env, f.p.uid);
        }
      };
      expect((await f.request()).status).toBe(503);
      expect(triggered).toBe(true);
      expect(await residual(f.env, f.p.uid)).toEqual({
        uid: f.p.uid,
        empty: true,
        writes: 0,
        objects_present: false,
      });
      expect(
        [...f.r2.uploads.values()].every((u) => !u.active && !u.bytes)
      ).toBe(true);
      expect((await f.content()).status).toBe(404);
    }
  );
  it.each(["setting", "epoch", "subject"])(
    "cancels pending images when the %s changes",
    async (change) => {
      const f = await fixture();
      let triggered = false;
      f.r2.hook = async (event) => {
        if (event !== "complete" || triggered) return;
        triggered = true;
        if (change === "setting")
          f.db
            .prepare(
              "INSERT INTO cf_screen_frame_settings(uid,enabled) VALUES (?,0)"
            )
            .run(f.p.uid);
        if (change === "epoch")
          f.db
            .prepare(
              "UPDATE cf_screen_frame_sets SET epoch = epoch + 1 WHERE uid = ?"
            )
            .run(f.p.uid);
        if (change === "subject")
          f.db
            .prepare("DELETE FROM cf_conversations WHERE uid = ?")
            .run(f.p.uid);
      };
      expect((await f.request()).status).toBe(503);
      expect(f.r2.objects.size).toBe(0);
      expect(
        f.db.prepare("SELECT phase FROM cf_screen_frame_writes").get()
      ).toEqual({ phase: "deleted" });
      expect((await f.request()).status).toBe(503);
    }
  );
  it("retains the cleanup journal on provider errors and retries without clearing another account", async () => {
    const f = await fixture();
    await f.request();
    f.publish();
    f.fence();
    f.r2.objects.set("another-owner/retained.jpg", jpeg);
    f.r2.abortError = new Error("abortMultipartUpload: InternalError (10001)");
    await expect(cleanup(f.env, f.p.uid)).rejects.toThrow("10001");
    expect(
      f.db.prepare("SELECT phase FROM cf_screen_frame_writes").get()
    ).toEqual({ phase: "cleanup" });
    expect((await residual(f.env, f.p.uid)).empty).toBe(false);
    f.r2.abortError = new Error("abortMultipartUpload: NoSuchUpload (10024)");
    await cleanup(f.env, f.p.uid);
    expect((await residual(f.env, f.p.uid)).empty).toBe(true);
    expect(f.r2.objects.has("another-owner/retained.jpg")).toBe(true);
  });
  it("revokes already-issued URLs on sharing or account setting changes, including during R2 reads", async () => {
    const f = await fixture();
    await f.request();
    f.publish();
    expect((await f.content("owner", "another-owner")).status).toBe(404);
    f.db.prepare("UPDATE cf_screen_frame_sets SET sharing_enabled = 0").run();
    expect((await f.content("shared")).status).toBe(404);
    expect((await f.content()).status).toBe(200);
    f.r2.hook = async (event) => {
      if (event === "get")
        f.db
          .prepare(
            "INSERT INTO cf_screen_frame_settings(uid,enabled) VALUES (?,0)"
          )
          .run(f.p.uid);
    };
    expect((await f.content()).status).toBe(404);
  });
  it("requires a method-, owner- and audience-bound internal assertion for erasure", async () => {
    const f = await fixture();
    const path = `/internal/users/${f.p.uid}/screen-frames/cleanup`;
    expect((await app.request(path, { method: "POST" }, f.env)).status).toBe(
      401
    );
    const signed = await createSignedAuthContext(
      { uid: f.p.uid, authority: "internal", requestId: "screen-test" },
      "screen-frame-writer",
      "POST",
      path,
      internalSecret
    );
    if (!signed) throw new Error("test signing unavailable");
    const headers = {
      [AUTH_CONTEXT_HEADER]: signed.encoded,
      [AUTH_SIGNATURE_HEADER]: signed.signature,
    };
    expect(
      (await app.request(path, { method: "POST", headers }, f.env)).status
    ).toBe(200);
    expect(
      (await app.request(path, { method: "GET", headers }, f.env)).status
    ).toBe(401);
  });
  it("refuses forged D1 publication without a completed write", async () => {
    const f = await fixture();
    expect(() => f.publish()).toThrow("not approved and written");
    await writeApproved(f.env, f.p, jpeg, thumbnail);
    f.db.prepare("UPDATE cf_screen_frame_sets SET epoch = epoch + 1").run();
    expect(() => f.publish()).toThrow("not approved and written");
  });
  it("serves erasure through the Jobs assertion and rejects missing or mismatched writer evidence", async () => {
    const f = await fixture("legacy:用户");
    await f.request();
    f.publish();
    f.fence();
    const env = {
      INTERNAL_ASSERTION_SECRET: internalSecret,
      SCREEN_FRAME_WRITER: {
        fetch: (request: Request) => app.fetch(request, f.env),
      } as unknown as Fetcher,
    };
    expect(await screenFrameStorageState(env, f.p.uid, "cleanup")).toEqual({
      empty: true,
      writes: 0,
      objects_present: false,
    });
    await expect(
      screenFrameStorageState(
        { INTERNAL_ASSERTION_SECRET: internalSecret },
        f.p.uid,
        "residual"
      )
    ).rejects.toThrow("unavailable");
    env.SCREEN_FRAME_WRITER = {
      fetch: async () =>
        Response.json({
          uid: "other-user",
          empty: true,
          writes: 0,
          objects_present: false,
        }),
    } as unknown as Fetcher;
    await expect(
      screenFrameStorageState(env, f.p.uid, "residual")
    ).rejects.toThrow("invalid screen frame residual");
  });
});
