CREATE TABLE IF NOT EXISTS cf_chat_messages (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  app_id TEXT,
  created_at INTEGER NOT NULL,
  message_json TEXT NOT NULL,
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_chat_messages_uid_app_created_idx
  ON cf_chat_messages(uid, app_id, created_at DESC, id DESC);
