CREATE TABLE IF NOT EXISTS cf_goal_progress_history (
  uid TEXT NOT NULL,
  goal_id TEXT NOT NULL,
  date TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  value REAL NOT NULL,
  recorded_at INTEGER NOT NULL,
  PRIMARY KEY (uid, goal_id, date)
);

CREATE INDEX IF NOT EXISTS cf_goal_progress_history_uid_goal_idx
  ON cf_goal_progress_history(uid, goal_id, date DESC);
