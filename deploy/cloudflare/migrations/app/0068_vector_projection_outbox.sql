CREATE TABLE IF NOT EXISTS cf_vector_projection_state (
  uid TEXT NOT NULL,
  projection_kind TEXT NOT NULL CHECK (projection_kind IN (
    'memory', 'action_item', 'conversation', 'transcript_chunk'
  )),
  source_id TEXT NOT NULL,
  sub_id TEXT NOT NULL DEFAULT '',
  vector_id TEXT NOT NULL CHECK (
    length(vector_id) = 64 AND vector_id NOT GLOB '*[^0-9a-f]*'
  ),
  source_version INTEGER NOT NULL,
  model TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, projection_kind, source_id, sub_id),
  UNIQUE (projection_kind, vector_id)
);

CREATE INDEX IF NOT EXISTS cf_vector_projection_state_uid_source_idx
  ON cf_vector_projection_state(uid, projection_kind, source_id);

CREATE TABLE IF NOT EXISTS cf_vector_projection_outbox (
  uid TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN (
    'memory', 'action_item', 'conversation'
  )),
  source_id TEXT NOT NULL,
  desired_version INTEGER NOT NULL,
  operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at INTEGER NOT NULL,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (uid, source_kind, source_id)
);

CREATE INDEX IF NOT EXISTS cf_vector_projection_outbox_due_idx
  ON cf_vector_projection_outbox(next_attempt_at, updated_at);

CREATE TRIGGER IF NOT EXISTS adf_i_vector_projection_state
BEFORE INSERT ON cf_vector_projection_state
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_vector_projection_state
BEFORE UPDATE ON cf_vector_projection_state
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_vector_projection_outbox
BEFORE INSERT ON cf_vector_projection_outbox
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_vector_projection_outbox
BEFORE UPDATE ON cf_vector_projection_outbox
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
