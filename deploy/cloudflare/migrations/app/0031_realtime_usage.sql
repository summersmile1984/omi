CREATE TABLE IF NOT EXISTS cf_realtime_sessions (
  uid TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  expires_at TEXT,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, token_hash)
);

CREATE INDEX IF NOT EXISTS cf_realtime_sessions_uid_created_idx
  ON cf_realtime_sessions(uid, created_at DESC);

CREATE TABLE IF NOT EXISTS cf_realtime_usage (
  uid TEXT NOT NULL,
  usage_date TEXT NOT NULL,
  input_text_tokens INTEGER NOT NULL DEFAULT 0,
  input_audio_tokens INTEGER NOT NULL DEFAULT 0,
  input_cached_tokens INTEGER NOT NULL DEFAULT 0,
  output_text_tokens INTEGER NOT NULL DEFAULT 0,
  output_audio_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  cost_micros INTEGER NOT NULL DEFAULT 0,
  call_count INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, usage_date)
);

CREATE INDEX IF NOT EXISTS cf_realtime_usage_date_idx
  ON cf_realtime_usage(usage_date, uid);
