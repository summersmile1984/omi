-- Bounded capture-gap reads use the user's recording start-time range.
CREATE INDEX IF NOT EXISTS cf_conversations_uid_started_idx
  ON cf_conversations(uid, started_at DESC, id DESC);
