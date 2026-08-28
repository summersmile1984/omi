CREATE TABLE IF NOT EXISTS cf_task_shares (
  token TEXT PRIMARY KEY,
  sender_uid TEXT NOT NULL,
  sender_name TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_task_shares_expiry_idx
  ON cf_task_shares(expires_at);

CREATE TABLE IF NOT EXISTS cf_task_share_items (
  token TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  action_item_id TEXT NOT NULL,
  PRIMARY KEY (token, action_item_id),
  UNIQUE (token, ordinal),
  FOREIGN KEY (token) REFERENCES cf_task_shares(token) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cf_task_share_acceptances (
  token TEXT NOT NULL,
  recipient_uid TEXT NOT NULL,
  acceptance_nonce TEXT NOT NULL UNIQUE,
  accepted_at INTEGER NOT NULL,
  PRIMARY KEY (token, recipient_uid),
  FOREIGN KEY (token) REFERENCES cf_task_shares(token) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_task_share_acceptances_recipient_idx
  ON cf_task_share_acceptances(recipient_uid, accepted_at DESC);
