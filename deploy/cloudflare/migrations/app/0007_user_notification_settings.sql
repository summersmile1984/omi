CREATE TABLE IF NOT EXISTS cf_user_notification_settings (
  uid TEXT PRIMARY KEY NOT NULL,
  notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK (notifications_enabled IN (0, 1)),
  notification_frequency INTEGER NOT NULL DEFAULT 0 CHECK (notification_frequency BETWEEN 0 AND 5),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_notification_settings_updated_idx
  ON cf_user_notification_settings(updated_at);
