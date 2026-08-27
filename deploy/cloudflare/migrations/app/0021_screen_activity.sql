CREATE TABLE IF NOT EXISTS cf_screen_activity (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  app_name TEXT NOT NULL DEFAULT '',
  window_title TEXT NOT NULL DEFAULT '',
  ocr_text TEXT NOT NULL DEFAULT '',
  device_name TEXT,
  client_device_id TEXT,
  PRIMARY KEY (uid, id),
  CHECK (length(timestamp) = 23),
  CHECK (length(app_name) <= 512),
  CHECK (length(window_title) <= 2048),
  CHECK (length(ocr_text) <= 1000),
  CHECK (device_name IS NULL OR length(device_name) <= 256),
  CHECK (client_device_id IS NULL OR length(client_device_id) <= 256)
);

CREATE INDEX IF NOT EXISTS cf_screen_activity_uid_timestamp_idx
  ON cf_screen_activity(uid, timestamp);

CREATE INDEX IF NOT EXISTS cf_screen_activity_uid_app_timestamp_idx
  ON cf_screen_activity(uid, app_name, timestamp);
