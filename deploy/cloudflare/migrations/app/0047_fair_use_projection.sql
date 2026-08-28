CREATE TABLE IF NOT EXISTS cf_fair_use_states (
  uid TEXT PRIMARY KEY,
  stage TEXT NOT NULL DEFAULT 'none' CHECK (stage IN ('none', 'warning', 'throttle', 'restrict')),
  last_case_ref TEXT NOT NULL DEFAULT '' CHECK (length(last_case_ref) <= 64),
  throttle_until INTEGER,
  restrict_until INTEGER,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cf_fair_use_usage_sources (
  uid TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN (
    'realtime', 'sync_fresh', 'sync_backfill', 'custom_stt'
  )),
  source_id TEXT NOT NULL CHECK (length(source_id) BETWEEN 1 AND 256),
  occurred_at INTEGER NOT NULL,
  speech_ms INTEGER NOT NULL DEFAULT 0 CHECK (speech_ms BETWEEN 0 AND 604800000),
  dg_ms INTEGER NOT NULL DEFAULT 0 CHECK (dg_ms BETWEEN 0 AND 604800000),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, source_kind, source_id)
);

CREATE INDEX IF NOT EXISTS cf_fair_use_usage_uid_occurred_idx
  ON cf_fair_use_usage_sources(uid, occurred_at);
