CREATE TABLE IF NOT EXISTS cf_user_developer_webhooks (
  uid TEXT NOT NULL,
  webhook_type TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, webhook_type)
);

CREATE INDEX IF NOT EXISTS cf_user_developer_webhooks_uid_updated_idx
  ON cf_user_developer_webhooks(uid, updated_at);
