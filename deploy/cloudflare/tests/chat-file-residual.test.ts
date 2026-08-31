import { describe, expect, it, vi } from "vitest";
import {
  ACCOUNT_DELETION_D1_SURFACES,
  readAccountProductResidual,
} from "../workers/jobs/account-deletion-residual";

function bucket(objects: Record<string, boolean>) {
  return {
    list: vi.fn(async ({ prefix }: { prefix: string }) => ({
      objects: objects[prefix] ? [{ key: `${prefix}file-id`, size: 1 }] : [],
    })),
  } as unknown as R2Bucket;
}

describe("chat file account-deletion residual", () => {
  it("scans the dedicated CHAT_FILES bucket separately from shared ASSETS", async () => {
    const appDb = {
      prepare: vi.fn(() => ({ bind: vi.fn(() => ({})) })),
      batch: vi.fn(async () =>
        ACCOUNT_DELETION_D1_SURFACES.map(() => ({
          success: true,
          results: [{ count: 0 }],
          meta: {},
        })),
      ),
    } as unknown as D1Database;
    const result = await readAccountProductResidual(
      {
        APP_DB: appDb,
        ASSETS: bucket({}),
        CHAT_FILES: bucket({ "user-1/": true }),
        CONVERSATION_RECORDINGS: bucket({}),
        SPEECH_PROFILES: bucket({}),
      },
      "user-1",
    );
    expect(result.empty).toBe(false);
    expect(result.r2["chat-files:user-1/"]).toBe(1);
  });
});
