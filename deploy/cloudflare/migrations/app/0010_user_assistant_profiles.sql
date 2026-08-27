CREATE TABLE IF NOT EXISTS cf_user_assistant_settings (
  uid TEXT PRIMARY KEY NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_assistant_settings_updated_idx
  ON cf_user_assistant_settings(updated_at);

CREATE TABLE IF NOT EXISTS cf_user_ai_profiles (
  uid TEXT PRIMARY KEY NOT NULL,
  profile_text TEXT,
  generated_at TEXT,
  data_sources_used INTEGER CHECK (data_sources_used IS NULL OR data_sources_used >= 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_ai_profiles_updated_idx
  ON cf_user_ai_profiles(updated_at);
