CREATE TABLE IF NOT EXISTS cf_sync_playback_objects (
  uid TEXT NOT NULL,
  storage_key TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  audio_file_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('staging', 'stored', 'committed')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, storage_key)
);

CREATE INDEX IF NOT EXISTS cf_sync_playback_objects_state_idx
  ON cf_sync_playback_objects(state, updated_at);

CREATE INDEX IF NOT EXISTS cf_sync_playback_objects_conversation_idx
  ON cf_sync_playback_objects(uid, conversation_id);
