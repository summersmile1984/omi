CREATE TABLE IF NOT EXISTS cf_conversations (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER,
  started_at INTEGER,
  finished_at INTEGER,
  source TEXT NOT NULL DEFAULT 'omi',
  language TEXT,
  status TEXT NOT NULL DEFAULT 'completed',
  visibility TEXT NOT NULL DEFAULT 'private',
  starred INTEGER NOT NULL DEFAULT 0 CHECK (starred IN (0, 1)),
  discarded INTEGER NOT NULL DEFAULT 0 CHECK (discarded IN (0, 1)),
  is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
  deferred INTEGER NOT NULL DEFAULT 0 CHECK (deferred IN (0, 1)),
  folder_id TEXT,
  client_device_id TEXT,
  client_platform TEXT,
  structured_json TEXT NOT NULL DEFAULT '{}',
  transcript_segments_json TEXT NOT NULL DEFAULT '[]',
  photos_json TEXT NOT NULL DEFAULT '[]',
  audio_files_json TEXT NOT NULL DEFAULT '[]',
  conversation_audio_json TEXT,
  apps_results_json TEXT NOT NULL DEFAULT '[]',
  suggested_apps_json TEXT NOT NULL DEFAULT '[]',
  geolocation_json TEXT,
  external_data_json TEXT,
  calendar_event_json TEXT,
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_conversations_uid_created_idx
  ON cf_conversations(uid, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS cf_conversations_uid_status_idx
  ON cf_conversations(uid, status, created_at DESC);

CREATE INDEX IF NOT EXISTS cf_conversations_uid_folder_idx
  ON cf_conversations(uid, folder_id, created_at DESC);
