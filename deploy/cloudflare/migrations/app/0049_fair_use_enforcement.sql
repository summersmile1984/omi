ALTER TABLE cf_fair_use_states
  ADD COLUMN violation_count_7d INTEGER NOT NULL DEFAULT 0 CHECK (violation_count_7d >= 0);

ALTER TABLE cf_fair_use_states
  ADD COLUMN violation_count_30d INTEGER NOT NULL DEFAULT 0 CHECK (violation_count_30d >= 0);

ALTER TABLE cf_fair_use_states
  ADD COLUMN last_violation_at INTEGER;

ALTER TABLE cf_fair_use_states
  ADD COLUMN last_classifier_score REAL NOT NULL DEFAULT 0.0
    CHECK (last_classifier_score BETWEEN 0.0 AND 1.0);

ALTER TABLE cf_fair_use_states
  ADD COLUMN last_classifier_type TEXT NOT NULL DEFAULT 'none' CHECK (last_classifier_type IN (
    'none', 'audiobook', 'podcast', 'prerecorded', 'tv_movie', 'commercial', 'unknown', 'free_exhausted'
  ));

ALTER TABLE cf_fair_use_states
  ADD COLUMN evaluation_lease_token TEXT;

ALTER TABLE cf_fair_use_states
  ADD COLUMN evaluation_lease_until INTEGER;

ALTER TABLE cf_fair_use_states
  ADD COLUMN next_evaluation_at INTEGER;

ALTER TABLE cf_fair_use_states
  ADD COLUMN cleared_by TEXT;

ALTER TABLE cf_fair_use_states
  ADD COLUMN cleared_at INTEGER;

CREATE INDEX IF NOT EXISTS cf_fair_use_states_evaluation_idx
  ON cf_fair_use_states(stage, next_evaluation_at, evaluation_lease_until);

CREATE INDEX IF NOT EXISTS cf_fair_use_usage_scan_idx
  ON cf_fair_use_usage_sources(source_kind, occurred_at, uid);

CREATE TABLE IF NOT EXISTS cf_fair_use_events (
  event_id TEXT PRIMARY KEY CHECK (length(event_id) BETWEEN 1 AND 64),
  uid TEXT NOT NULL,
  case_ref TEXT NOT NULL UNIQUE CHECK (length(case_ref) BETWEEN 4 AND 64),
  created_at INTEGER NOT NULL,
  session_id TEXT NOT NULL DEFAULT '' CHECK (length(session_id) <= 256),
  trigger TEXT NOT NULL CHECK (trigger IN ('daily', '3day', 'weekly')),
  daily_speech_ms INTEGER NOT NULL CHECK (daily_speech_ms >= 0),
  three_day_speech_ms INTEGER NOT NULL CHECK (three_day_speech_ms >= 0),
  weekly_speech_ms INTEGER NOT NULL CHECK (weekly_speech_ms >= 0),
  daily_threshold_ms INTEGER NOT NULL CHECK (daily_threshold_ms > 0),
  three_day_threshold_ms INTEGER NOT NULL CHECK (three_day_threshold_ms > 0),
  weekly_threshold_ms INTEGER NOT NULL CHECK (weekly_threshold_ms > 0),
  classifier_json TEXT,
  enforcement_action TEXT NOT NULL CHECK (enforcement_action IN ('none', 'warning', 'throttle', 'restrict')),
  previous_stage TEXT NOT NULL CHECK (previous_stage IN ('none', 'warning', 'throttle', 'restrict')),
  new_stage TEXT NOT NULL CHECK (new_stage IN ('none', 'warning', 'throttle', 'restrict')),
  resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
  resolved_at INTEGER,
  resolved_by TEXT,
  admin_notes TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS cf_fair_use_events_uid_created_idx
  ON cf_fair_use_events(uid, created_at DESC);

CREATE TABLE IF NOT EXISTS cf_fair_use_notification_outbox (
  notification_id TEXT PRIMARY KEY CHECK (length(notification_id) BETWEEN 1 AND 64),
  event_id TEXT NOT NULL UNIQUE,
  uid TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  data_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  not_before INTEGER NOT NULL,
  lease_until INTEGER,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (event_id) REFERENCES cf_fair_use_events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_fair_use_notification_outbox_delivery_idx
  ON cf_fair_use_notification_outbox(status, not_before, created_at);
