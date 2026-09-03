CREATE TABLE IF NOT EXISTS cf_announcements (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('changelog', 'feature', 'announcement')),
  created_at INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  app_version TEXT,
  firmware_version TEXT,
  device_models_json TEXT NOT NULL DEFAULT '[]',
  expires_at INTEGER,
  targeting_json TEXT,
  display_json TEXT,
  content_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS cf_announcements_active_type_created_idx
  ON cf_announcements (active, type, created_at DESC);

CREATE INDEX IF NOT EXISTS cf_announcements_active_versions_idx
  ON cf_announcements (active, app_version, firmware_version);

CREATE TABLE IF NOT EXISTS cf_announcement_dismissals (
  uid TEXT NOT NULL,
  announcement_id TEXT NOT NULL,
  dismissed_at INTEGER NOT NULL,
  cta_clicked INTEGER NOT NULL DEFAULT 0 CHECK (cta_clicked IN (0, 1)),
  PRIMARY KEY (uid, announcement_id),
  FOREIGN KEY (announcement_id) REFERENCES cf_announcements(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_announcement_dismissals_uid_idx
  ON cf_announcement_dismissals (uid, dismissed_at DESC);
