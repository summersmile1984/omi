CREATE TABLE IF NOT EXISTS cf_workstreams (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  goal_id TEXT,
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 256),
  objective TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 2048),
  status TEXT NOT NULL CHECK (status IN ('open', 'paused', 'completed', 'archived')),
  current_state_summary TEXT NOT NULL DEFAULT '',
  next_review_at INTEGER,
  last_meaningful_progress_at INTEGER,
  latest_event_sequence INTEGER NOT NULL DEFAULT 0 CHECK (latest_event_sequence >= 0),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_workstreams_uid_goal_idx
  ON cf_workstreams(uid, goal_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS cf_workstreams_uid_status_idx
  ON cf_workstreams(uid, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS cf_workstream_events (
  uid TEXT NOT NULL,
  event_id TEXT NOT NULL,
  workstream_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence >= 1),
  kind TEXT NOT NULL CHECK (kind IN (
    'user_note', 'conversation', 'message', 'screen_observation', 'task_change',
    'decision', 'agent_update', 'artifact_version', 'external_update', 'system'
  )),
  summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 2000),
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  sensitivity TEXT NOT NULL CHECK (sensitivity IN ('normal', 'sensitive', 'restricted')),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS cf_workstream_events_uid_workstream_sequence_idx
  ON cf_workstream_events(uid, workstream_id, sequence);

CREATE INDEX IF NOT EXISTS cf_workstream_events_uid_workstream_idx
  ON cf_workstream_events(uid, workstream_id, sequence DESC);

CREATE TABLE IF NOT EXISTS cf_workstream_artifacts (
  uid TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  workstream_id TEXT NOT NULL,
  logical_key TEXT NOT NULL CHECK (length(logical_key) BETWEEN 1 AND 256),
  version INTEGER NOT NULL CHECK (version >= 1),
  supersedes_artifact_id TEXT,
  kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
  uri TEXT NOT NULL CHECK (length(uri) BETWEEN 1 AND 2048),
  content_hash TEXT NOT NULL CHECK (length(content_hash) BETWEEN 16 AND 128),
  source_run_id TEXT,
  evidence_event_ids_json TEXT NOT NULL DEFAULT '[]',
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK (status IN ('draft', 'awaiting_review', 'approved', 'delivered', 'superseded')),
  created_at INTEGER NOT NULL,
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  PRIMARY KEY (uid, artifact_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS cf_workstream_artifacts_uid_head_idx
  ON cf_workstream_artifacts(uid, workstream_id, logical_key, version);

CREATE INDEX IF NOT EXISTS cf_workstream_artifacts_uid_workstream_idx
  ON cf_workstream_artifacts(uid, workstream_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cf_workstream_checkpoints (
  uid TEXT NOT NULL,
  checkpoint_id TEXT NOT NULL,
  workstream_id TEXT NOT NULL,
  runtime_id TEXT NOT NULL CHECK (length(runtime_id) BETWEEN 1 AND 256),
  last_event_sequence INTEGER NOT NULL CHECK (last_event_sequence >= 0),
  context_summary TEXT NOT NULL CHECK (length(context_summary) <= 4000),
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  updated_at INTEGER NOT NULL,
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  PRIMARY KEY (uid, checkpoint_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS cf_workstream_checkpoints_uid_runtime_idx
  ON cf_workstream_checkpoints(uid, workstream_id, runtime_id);

CREATE TABLE IF NOT EXISTS cf_workstream_mutations (
  uid TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, operation, idempotency_key)
);
