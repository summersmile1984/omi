CREATE TABLE IF NOT EXISTS cf_user_enabled_apps (
  uid TEXT NOT NULL,
  app_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, app_id)
);

CREATE INDEX IF NOT EXISTS cf_user_enabled_apps_uid_created_idx
  ON cf_user_enabled_apps (uid, created_at ASC, app_id ASC);
