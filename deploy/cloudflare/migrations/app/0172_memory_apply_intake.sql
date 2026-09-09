-- Native intake commits upstream apply results into the existing memory rows.
-- These journal tables do not replace cf_memories or the archive-capability
-- projection cf_memory_control. Other writer families must still converge here
-- before a complete ledger head can be exposed by public snapshot routes.
ALTER TABLE cf_memories ADD COLUMN canonical_metadata_json TEXT NOT NULL DEFAULT '{}'
  CHECK (json_valid(canonical_metadata_json) AND json_type(canonical_metadata_json) = 'object');

CREATE TABLE cf_memory_apply_control (
  uid TEXT PRIMARY KEY NOT NULL,
  head_commit_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
  commit_sequence INTEGER NOT NULL CHECK (commit_sequence >= 0),
  control_json TEXT NOT NULL CHECK (json_valid(control_json) AND json_type(control_json) = 'object'),
  CHECK (json_extract(control_json, '$.uid') = uid),
  CHECK (json_extract(control_json, '$.head_commit_id') = head_commit_id),
  CHECK (json_extract(control_json, '$.account_generation') = account_generation),
  CHECK (json_extract(control_json, '$.source_generation') = source_generation),
  CHECK (json_extract(control_json, '$.commit_sequence') = commit_sequence)
);

CREATE TABLE cf_memory_operations (
  uid TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  logical_payload_digest TEXT NOT NULL,
  operation_json TEXT NOT NULL CHECK (json_valid(operation_json)),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, operation_id),
  CHECK (json_extract(operation_json, '$.uid') = uid),
  CHECK (json_extract(operation_json, '$.operation_id') = operation_id),
  CHECK (json_extract(operation_json, '$.logical_payload_digest') = logical_payload_digest),
  CHECK (json_extract(operation_json, '$.status') = 'committed')
);

CREATE TABLE cf_memory_commits (
  uid TEXT NOT NULL,
  commit_id TEXT NOT NULL,
  parent_commit_id TEXT NOT NULL,
  commit_sequence INTEGER NOT NULL CHECK (commit_sequence > 0),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
  operation_id TEXT NOT NULL,
  memory_ids_json TEXT NOT NULL CHECK (json_valid(memory_ids_json)),
  outbox_ids_json TEXT NOT NULL CHECK (json_valid(outbox_ids_json)),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, commit_id),
  UNIQUE (uid, account_generation, source_generation, commit_sequence)
);

CREATE TABLE cf_memory_outbox (
  uid TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('projection_sync', 'vector_sync', 'export_sync', 'delete_sync')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'delivered', 'retryable_failure')),
  memory_id TEXT,
  commit_id TEXT NOT NULL,
  commit_sequence INTEGER NOT NULL CHECK (commit_sequence > 0),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  event_json TEXT NOT NULL CHECK (json_valid(event_json)),
  available_at INTEGER NOT NULL,
  PRIMARY KEY (uid, event_id),
  CHECK (json_extract(event_json, '$.uid') = uid),
  CHECK (json_extract(event_json, '$.event_id') = event_id)
);
CREATE INDEX cf_memory_outbox_pending ON cf_memory_outbox(status, available_at, uid, event_id);

-- Exists only inside one D1 batch and is removed by its last statement. A
-- failed admission aborts the batch before any memory/journal/projection write.
CREATE TABLE cf_memory_apply_guard (
  uid TEXT PRIMARY KEY NOT NULL,
  expected_control_json TEXT,
  account_generation INTEGER NOT NULL,
  new_ids_json TEXT NOT NULL CHECK (json_valid(new_ids_json)),
  operation_ids_json TEXT NOT NULL CHECK (json_valid(operation_ids_json))
);
CREATE TRIGGER cf_memory_apply_admission BEFORE INSERT ON cf_memory_apply_guard
BEGIN
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
    OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
    THEN RAISE(ABORT, 'memory_apply_account_deleted') END);
  SELECT (CASE WHEN NEW.account_generation != COALESCE(
    (SELECT account_generation FROM cf_account_cutover WHERE uid = NEW.uid), 0)
    THEN RAISE(ABORT, 'memory_apply_generation_changed') END);
  SELECT (CASE WHEN (SELECT control_json FROM cf_memory_apply_control WHERE uid = NEW.uid)
    IS NOT NEW.expected_control_json THEN RAISE(ABORT, 'memory_apply_head_changed') END);
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_memories
    WHERE uid = NEW.uid AND id IN (SELECT value FROM json_each(NEW.new_ids_json)))
    THEN RAISE(ABORT, 'memory_apply_target_exists') END);
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_memory_operations
    WHERE uid = NEW.uid AND operation_id IN (SELECT value FROM json_each(NEW.operation_ids_json)))
    THEN RAISE(ABORT, 'memory_apply_operation_changed') END);
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_apply_control BEFORE INSERT ON cf_memory_apply_control
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_apply_control BEFORE UPDATE ON cf_memory_apply_control
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_operations BEFORE INSERT ON cf_memory_operations
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_operations BEFORE UPDATE ON cf_memory_operations
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_commits BEFORE INSERT ON cf_memory_commits
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_commits BEFORE UPDATE ON cf_memory_commits
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_outbox BEFORE INSERT ON cf_memory_outbox
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_outbox BEFORE UPDATE ON cf_memory_outbox
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_apply_guard BEFORE INSERT ON cf_memory_apply_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_apply_guard BEFORE UPDATE ON cf_memory_apply_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
