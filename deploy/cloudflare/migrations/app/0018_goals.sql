CREATE TABLE IF NOT EXISTS cf_goals (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  desired_outcome TEXT NOT NULL CHECK (length(desired_outcome) BETWEEN 1 AND 2000),
  why_it_matters TEXT,
  success_criteria_json TEXT NOT NULL DEFAULT '[]',
  horizon_at INTEGER,
  status TEXT NOT NULL CHECK (status IN ('background', 'focused', 'paused', 'achieved', 'abandoned')),
  focus_rank INTEGER,
  metric_json TEXT,
  source TEXT NOT NULL CHECK (source IN ('user', 'ai_suggested', 'imported')),
  relationship_disposition TEXT NOT NULL DEFAULT 'retain' CHECK (relationship_disposition IN ('retain', 'detach')),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  latest_progress_sequence INTEGER NOT NULL DEFAULT 0,
  ended_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_goals_uid_active_idx
  ON cf_goals(uid, is_active, status, focus_rank, created_at DESC);
