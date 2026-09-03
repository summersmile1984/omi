CREATE TABLE IF NOT EXISTS cf_x_posts (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  text TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('tweet', 'bookmark', 'like')),
  lang TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  ingested_at INTEGER,
  updated_at INTEGER NOT NULL,
  memory_extraction_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (memory_extraction_status IN ('pending', 'completed')),
  memory_extracted_at INTEGER,
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_x_posts_uid_created_idx
  ON cf_x_posts(uid, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS cf_x_posts_uid_kind_created_idx
  ON cf_x_posts(uid, kind, created_at DESC, id DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_x_posts
BEFORE INSERT ON cf_x_posts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_x_posts
BEFORE UPDATE ON cf_x_posts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

-- SQLite cannot widen a CHECK constraint in place. Rebuild both projection
-- tables while preserving the already migrated memory/action/conversation rows.
CREATE TABLE cf_vector_projection_state_next (
  uid TEXT NOT NULL,
  projection_kind TEXT NOT NULL CHECK (projection_kind IN (
    'memory', 'action_item', 'conversation', 'transcript_chunk', 'x_post'
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

INSERT INTO cf_vector_projection_state_next
SELECT uid, projection_kind, source_id, sub_id, vector_id,
       source_version, model, updated_at
FROM cf_vector_projection_state;

DROP TABLE cf_vector_projection_state;
ALTER TABLE cf_vector_projection_state_next RENAME TO cf_vector_projection_state;

CREATE INDEX cf_vector_projection_state_uid_source_idx
  ON cf_vector_projection_state(uid, projection_kind, source_id);

CREATE TABLE cf_vector_projection_outbox_next (
  uid TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN (
    'memory', 'action_item', 'conversation', 'x_post'
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

INSERT INTO cf_vector_projection_outbox_next
SELECT uid, source_kind, source_id, desired_version, operation, attempts,
       next_attempt_at, last_error, created_at, updated_at
FROM cf_vector_projection_outbox;

DROP TABLE cf_vector_projection_outbox;
ALTER TABLE cf_vector_projection_outbox_next RENAME TO cf_vector_projection_outbox;

CREATE INDEX cf_vector_projection_outbox_due_idx
  ON cf_vector_projection_outbox(next_attempt_at, updated_at);

CREATE TRIGGER adf_i_vector_projection_state
BEFORE INSERT ON cf_vector_projection_state
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER adf_u_vector_projection_state
BEFORE UPDATE ON cf_vector_projection_state
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER adf_i_vector_projection_outbox
BEFORE INSERT ON cf_vector_projection_outbox
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER adf_u_vector_projection_outbox
BEFORE UPDATE ON cf_vector_projection_outbox
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
