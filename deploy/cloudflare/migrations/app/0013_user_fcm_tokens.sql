CREATE TABLE IF NOT EXISTS cf_user_fcm_tokens (
  uid TEXT NOT NULL,
  device_key TEXT NOT NULL,
  token TEXT NOT NULL,
  time_zone TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, device_key)
);

CREATE INDEX IF NOT EXISTS cf_user_fcm_tokens_uid_updated_idx
  ON cf_user_fcm_tokens(uid, updated_at);
