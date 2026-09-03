CREATE TABLE IF NOT EXISTS cf_user_notification_preferences (
  uid TEXT PRIMARY KEY NOT NULL,
  daily_summary_enabled INTEGER NOT NULL DEFAULT 1 CHECK (daily_summary_enabled IN (0, 1)),
  daily_summary_hour_local INTEGER NOT NULL DEFAULT 22 CHECK (daily_summary_hour_local BETWEEN 0 AND 23),
  mentor_notification_frequency INTEGER NOT NULL DEFAULT 0 CHECK (mentor_notification_frequency BETWEEN 0 AND 5),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_notification_preferences_updated_idx
  ON cf_user_notification_preferences(updated_at);
