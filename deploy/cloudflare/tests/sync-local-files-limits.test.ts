import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import { registerSyncRoutes } from "../workers/jobs/sync-local-files";
import type { JobsEnv } from "../workers/jobs/env";

const authContext = async () => ({ uid: "user-1" });

// A claim-count-gate probe does not need a working D1 database -- only that
// the request gets past validateClaims without erroring on a missing
// binding. first() resolving to null fails conversationMatchesCapture's own
// check (403), which is how "the claim count was fine" is distinguished from
// "validateClaims rejected the batch" (400) below.
const stubAppDb = {
  prepare: () => ({
    bind: () => ({ first: async () => null }),
  }),
} as unknown as JobsEnv["APP_DB"];

function app() {
  const instance = new Hono<{ Bindings: JobsEnv }>();
  registerSyncRoutes(instance, authContext);
  return instance;
}

function syncFile(index: number) {
  return { name: `chunk_${index}_${Date.now()}.bin`, sha256: "a".repeat(64) };
}

function request(files: unknown[]) {
  return new Request("https://jobs.internal/v2/sync-capture-manifest", {
    method: "POST",
    headers: {
      "x-app-platform": "ios",
      "x-device-id-hash": "deadbeef",
      "content-type": "application/json",
    },
    body: JSON.stringify({ conversation_id: "conversation-1", files }),
  });
}

describe("sync-local-files SYNC_MAX_FILES override", () => {
  it("accepts a batch within the default ceiling with no env override", async () => {
    const files = Array.from({ length: 3 }, (_, i) => syncFile(i));
    const response = await app().fetch(
      request(files),
      { APP_DB: stubAppDb } as JobsEnv,
    );
    // No R2/D1 bindings are wired in this env, so a valid claim count still
    // fails downstream -- the point here is that it gets PAST the claim-count
    // gate (not a 400 from validateClaims), proving the default (20) admits 3.
    expect(response.status).not.toBe(400);
  });

  it("rejects a batch the default ceiling would admit once SYNC_MAX_FILES lowers it", async () => {
    const files = Array.from({ length: 3 }, (_, i) => syncFile(i));
    const response = await app().fetch(
      request(files),
      { APP_DB: stubAppDb, SYNC_MAX_FILES: "2" } as JobsEnv,
    );
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({
      error: "invalid capture manifest request",
    });
  });
});
