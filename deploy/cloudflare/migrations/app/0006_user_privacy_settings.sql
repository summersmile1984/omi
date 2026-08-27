CREATE TABLE IF NOT EXISTS cf_user_privacy_settings (
  uid TEXT PRIMARY KEY NOT NULL,
  store_recording_permission INTEGER NOT NULL DEFAULT 0 CHECK (store_recording_permission IN (0, 1)),
  private_cloud_sync_enabled INTEGER NOT NULL DEFAULT 1 CHECK (private_cloud_sync_enabled IN (0, 1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_privacy_settings_updated_idx
  ON cf_user_privacy_settings(updated_at);
