CREATE TABLE IF NOT EXISTS cf_chat_shares (
  token TEXT PRIMARY KEY,
  sender_uid TEXT NOT NULL,
  sender_name TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_chat_shares_expiry_idx
  ON cf_chat_shares(expires_at);

CREATE TABLE IF NOT EXISTS cf_chat_share_messages (
  token TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  message_id TEXT NOT NULL,
  PRIMARY KEY (token, message_id),
  UNIQUE (token, ordinal),
  FOREIGN KEY (token) REFERENCES cf_chat_shares(token) ON DELETE CASCADE
);
