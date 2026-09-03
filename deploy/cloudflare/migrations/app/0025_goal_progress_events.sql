CREATE TABLE IF NOT EXISTS cf_goal_progress_events (
  uid TEXT NOT NULL,
  event_id TEXT NOT NULL,
  goal_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence >= 1),
  kind TEXT NOT NULL CHECK (kind IN ('evidence', 'metric_update', 'milestone', 'status_change')),
  summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 1000),
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  metric_json TEXT,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS cf_goal_progress_events_uid_goal_sequence_idx
  ON cf_goal_progress_events(uid, goal_id, sequence);

CREATE INDEX IF NOT EXISTS cf_goal_progress_events_uid_goal_idx
  ON cf_goal_progress_events(uid, goal_id, sequence DESC);
