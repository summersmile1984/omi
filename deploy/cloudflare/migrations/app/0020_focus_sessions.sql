CREATE TABLE IF NOT EXISTS cf_focus_sessions (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('focused', 'distracted')),
  app_or_site TEXT NOT NULL,
  description TEXT NOT NULL,
  message TEXT,
  created_at INTEGER NOT NULL,
  duration_seconds INTEGER,
  PRIMARY KEY (uid, id),
  CHECK (length(app_or_site) BETWEEN 1 AND 500),
  CHECK (length(description) BETWEEN 1 AND 5000),
  CHECK (message IS NULL OR length(message) <= 5000),
  CHECK (duration_seconds IS NULL OR duration_seconds BETWEEN 0 AND 86400)
);

CREATE INDEX IF NOT EXISTS cf_focus_sessions_uid_created_idx
  ON cf_focus_sessions(uid, created_at DESC);
