import type { JobMessage, JobsEnv } from "./env";

// Cascade conversation deletion removes the D1 projection synchronously in
// api-core and enqueues this job for the R2 audio owned by that single
// conversation. Deleting by exact conversation prefix keeps the purge
// idempotent and safely retryable; the per-uid recording-deletion flow remains
// the authority for whole-account audio removal.
export const CONVERSATION_AUDIO_PREFIX_PATTERNS = Object.freeze([
  "chunks/{uid}/{conversationId}/",
  "merged/{uid}/{conversationId}/",
  "playback/{uid}/{conversationId}/",
  "sync-playback/{uid}/{conversationId}/",
] as const);

const R2_DELETE_BATCH_SIZE = 500;
const MAX_ID_LENGTH = 256;

function conversationIdFrom(message: Message<JobMessage>): string {
  const raw = message.body.payload?.conversationId;
  if (
    typeof raw !== "string" ||
    !raw ||
    raw.length > MAX_ID_LENGTH ||
    raw.includes("/")
  ) {
    throw new Error("conversation audio purge requires a conversationId");
  }
  return raw;
}

function prefixFor(pattern: string, uid: string, conversationId: string) {
  return pattern
    .replace("{uid}", uid)
    .replace("{conversationId}", conversationId);
}

export async function processConversationAudioPurgeMessage(
  message: Message<JobMessage>,
  env: JobsEnv,
): Promise<void> {
  const uid = message.body.uid;
  if (typeof uid !== "string" || !uid || uid.length > MAX_ID_LENGTH) {
    throw new Error("conversation audio purge requires a uid");
  }
  const conversationId = conversationIdFrom(message);
  for (const pattern of CONVERSATION_AUDIO_PREFIX_PATTERNS) {
    const prefix = prefixFor(pattern, uid, conversationId);
    for (;;) {
      const listed = await env.ASSETS.list({
        prefix,
        limit: R2_DELETE_BATCH_SIZE,
      });
      const keys = listed.objects.map(({ key }) => key);
      if (!keys.length) break;
      await env.ASSETS.delete(keys);
      if (keys.length < R2_DELETE_BATCH_SIZE) break;
    }
  }
  await env.CONVERSATION_RECORDINGS.delete(`${uid}/${conversationId}.wav`);
}
