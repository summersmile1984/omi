CREATE TABLE IF NOT EXISTS cf_action_items (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'cancelled', 'superseded')),
  completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
  goal_id TEXT,
  workstream_id TEXT,
  owner TEXT NOT NULL DEFAULT 'unknown',
  due_at INTEGER,
  due_confidence REAL,
  source TEXT NOT NULL DEFAULT 'legacy',
  provenance_json TEXT NOT NULL DEFAULT '[]',
  priority TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  indent_level INTEGER NOT NULL DEFAULT 0,
  recurrence_rule TEXT,
  recurrence_parent_id TEXT,
  superseded_by TEXT,
  conversation_id TEXT,
  is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
  exported INTEGER NOT NULL DEFAULT 0 CHECK (exported IN (0, 1)),
  export_date INTEGER,
  export_platform TEXT,
  apple_reminder_id TEXT,
  completed_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  idempotency_key TEXT,
  sync_requested INTEGER NOT NULL DEFAULT 0 CHECK (sync_requested IN (0, 1)),
  deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_action_items_uid_sort_idx
  ON cf_action_items(uid, deleted, completed, due_at, created_at);

CREATE INDEX IF NOT EXISTS cf_action_items_uid_idempotency_idx
  ON cf_action_items(uid, idempotency_key, deleted, completed);
