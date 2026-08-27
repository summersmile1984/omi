CREATE TABLE IF NOT EXISTS cf_user_transcription_preferences (
  uid TEXT PRIMARY KEY NOT NULL,
  single_language_mode INTEGER NOT NULL DEFAULT 0 CHECK (single_language_mode IN (0, 1)),
  vocabulary_json TEXT NOT NULL DEFAULT '[]',
  language TEXT NOT NULL DEFAULT '',
  uses_custom_stt INTEGER NOT NULL DEFAULT 0 CHECK (uses_custom_stt IN (0, 1)),
  custom_stt_since TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_transcription_preferences_updated_idx
  ON cf_user_transcription_preferences(updated_at);
