import { describe, expect, it, vi } from "vitest";
import type { JobMessage, JobsEnv } from "../workers/jobs/env";
import {
  CONVERSATION_AUDIO_PREFIX_PATTERNS,
  processConversationAudioPurgeMessage,
} from "../workers/jobs/conversation-audio-purge";

function fakeBucket(initialKeys: string[] = []) {
  const objects = new Set(initialKeys);
  return {
    objects,
    binding: {
      list: vi.fn(async ({ prefix, limit }: { prefix?: string; limit?: number }) => ({
        objects: [...objects]
          .filter((key) => key.startsWith(prefix || ""))
          .slice(0, limit || 1_000)
          .map((key) => ({ key })),
        truncated: false,
      })),
      delete: vi.fn(async (keys: string | string[]) => {
        for (const key of Array.isArray(keys) ? keys : [keys])
          objects.delete(key);
      }),
    } as unknown as R2Bucket,
  };
}

function message(payload: Record<string, unknown>, uid = "purge-user") {
  return {
    body: {
      jobId: "conv-audio-purge-test",
      uid,
      kind: "conversation_audio_purge",
      payload,
    },
  } as unknown as Message<JobMessage>;
}

describe("conversation audio purge", () => {
  it("removes only the target conversation's audio across every prefix family", async () => {
    const assets = fakeBucket([
      "chunks/purge-user/conv-1/0001.opus",
      "chunks/purge-user/conv-1/0002.opus",
      "merged/purge-user/conv-1/audio.wav",
      "playback/purge-user/conv-1/audio.mp3",
      "sync-playback/purge-user/conv-1/audio.wav",
      // Same user, different conversation — must survive.
      "chunks/purge-user/conv-2/0001.opus",
      "playback/purge-user/conv-2/audio.mp3",
      // Different user with the same conversation id — must survive.
      "chunks/other-user/conv-1/0001.opus",
      // Job-scoped prefixes are not conversation-owned — must survive.
      "cf-sync/purge-user/job-1/0",
      "imports/purge-user/job-1.zip",
    ]);
    const recordings = fakeBucket([
      "purge-user/conv-1.wav",
      "purge-user/conv-2.wav",
      "other-user/conv-1.wav",
    ]);
    const env = {
      ASSETS: assets.binding,
      CONVERSATION_RECORDINGS: recordings.binding,
    } as unknown as JobsEnv;

    await processConversationAudioPurgeMessage(
      message({ conversationId: "conv-1" }),
      env,
    );

    expect([...assets.objects].sort()).toEqual([
      "cf-sync/purge-user/job-1/0",
      "chunks/other-user/conv-1/0001.opus",
      "chunks/purge-user/conv-2/0001.opus",
      "imports/purge-user/job-1.zip",
      "playback/purge-user/conv-2/audio.mp3",
    ]);
    expect([...recordings.objects].sort()).toEqual([
      "other-user/conv-1.wav",
      "purge-user/conv-2.wav",
    ]);

    // Idempotent: a retry of the same message is a no-op, not an error.
    await processConversationAudioPurgeMessage(
      message({ conversationId: "conv-1" }),
      env,
    );
    expect(assets.objects.size).toBe(5);
  });

  it("rejects a payload without a safe conversation id", async () => {
    const env = {
      ASSETS: fakeBucket().binding,
      CONVERSATION_RECORDINGS: fakeBucket().binding,
    } as unknown as JobsEnv;
    await expect(
      processConversationAudioPurgeMessage(message({}), env),
    ).rejects.toThrow("conversationId");
    await expect(
      processConversationAudioPurgeMessage(
        message({ conversationId: "../escape" }),
        env,
      ),
    ).rejects.toThrow("conversationId");
    expect(CONVERSATION_AUDIO_PREFIX_PATTERNS).toHaveLength(4);
  });
});
