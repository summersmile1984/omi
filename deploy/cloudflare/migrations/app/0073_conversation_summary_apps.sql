CREATE TABLE IF NOT EXISTS cf_conversation_summary_apps (
  app_id TEXT PRIMARY KEY NOT NULL CHECK (length(app_id) BETWEEN 1 AND 256),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_conversation_summary_apps_updated_idx
  ON cf_conversation_summary_apps(updated_at DESC, app_id);
