-- Canonical privacy preparation: the account gate, complete lineage, item
-- revisions and control head are admitted in the same transaction as scrub.
CREATE TABLE cf_legal_holds (
  uid TEXT PRIMARY KEY NOT NULL,
  schema_version TEXT NOT NULL CHECK (schema_version = 'legal_hold.v1'),
  issuer TEXT NOT NULL CHECK (issuer IN ('admin', 'legal_hold_service')),
  active INTEGER NOT NULL CHECK (active IN (0, 1)),
  updated_at INTEGER NOT NULL
);
CREATE TABLE cf_destructive_operation_gates (
  uid TEXT PRIMARY KEY NOT NULL,
  kind TEXT NOT NULL CHECK (length(kind) > 0),
  token TEXT NOT NULL CHECK (length(token) = 64 AND token NOT GLOB '*[^0-9a-f]*'),
  state TEXT NOT NULL CHECK (state IN ('running', 'failed', 'completed')),
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  CHECK ((state = 'running' AND finished_at IS NULL) OR (state != 'running' AND finished_at IS NOT NULL))
);
CREATE TRIGGER cf_destructive_gate_acquire BEFORE INSERT ON cf_destructive_operation_gates
WHEN NEW.state = 'running'
BEGIN
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_legal_holds WHERE uid = NEW.uid AND active = 1)
    THEN RAISE(ABORT, 'legal_hold_active') END);
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_destructive_operation_gates WHERE uid = NEW.uid
    AND state = 'running' AND started_at > unixepoch() - 21600 AND (token != NEW.token OR kind != NEW.kind))
    THEN RAISE(ABORT, 'destructive_operation_in_progress') END);
END;
CREATE TRIGGER cf_destructive_gate_update BEFORE UPDATE ON cf_destructive_operation_gates
BEGIN
  SELECT (CASE WHEN OLD.uid != NEW.uid THEN RAISE(ABORT, 'destructive_operation_identity_changed') END);
  SELECT (CASE WHEN NEW.state = 'running' AND EXISTS (
    SELECT 1 FROM cf_legal_holds WHERE uid = NEW.uid AND active = 1)
    THEN RAISE(ABORT, 'legal_hold_active') END);
  SELECT (CASE WHEN OLD.state = 'running' AND OLD.started_at > unixepoch() - 21600
    AND (OLD.token != NEW.token OR OLD.kind != NEW.kind)
    THEN RAISE(ABORT, 'destructive_operation_in_progress') END);
END;
CREATE TRIGGER cf_legal_hold_insert BEFORE INSERT ON cf_legal_holds
WHEN NEW.active = 1 AND EXISTS (SELECT 1 FROM cf_destructive_operation_gates
  WHERE uid = NEW.uid AND state = 'running' AND started_at > unixepoch() - 21600)
BEGIN SELECT RAISE(ABORT, 'destructive_operation_in_progress'); END;
CREATE TRIGGER cf_legal_hold_update BEFORE UPDATE ON cf_legal_holds
WHEN NEW.active = 1 AND EXISTS (SELECT 1 FROM cf_destructive_operation_gates
  WHERE uid = NEW.uid AND state = 'running' AND started_at > unixepoch() - 21600)
BEGIN SELECT RAISE(ABORT, 'destructive_operation_in_progress'); END;

-- Whitespace matches Python str.strip(), used by the original lineage owner.
-- Empty canonical identity falls back to superseded_by; whitespace-only does not.
CREATE VIEW cf_memory_lineage_edges AS
SELECT uid, id, COALESCE(NULLIF(trim(
  CASE WHEN COALESCE(json_extract(canonical_metadata_json, '$.canonical_memory_id'), '') != ''
    THEN json_extract(canonical_metadata_json, '$.canonical_memory_id') ELSE COALESCE(superseded_by, '') END,
  char(9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)
), ''), id) AS parent_id FROM cf_memories;

-- Durable cleanup inventory contains no content. It survives provider failures;
-- only successful finalization removes it and its corresponding tombstones.
CREATE TABLE cf_memory_privacy_deletions (
  uid TEXT PRIMARY KEY NOT NULL,
  token TEXT NOT NULL,
  requested_ids_json TEXT NOT NULL CHECK (json_valid(requested_ids_json) AND json_type(requested_ids_json) = 'array'),
  targets_json TEXT NOT NULL CHECK (json_valid(targets_json) AND json_type(targets_json) = 'array'),
  created_at INTEGER NOT NULL,
  CHECK (json_array_length(targets_json) BETWEEN 1 AND 100)
);
CREATE TRIGGER cf_memory_privacy_deletion_immutable BEFORE UPDATE ON cf_memory_privacy_deletions
BEGIN SELECT RAISE(ABORT, 'memory_privacy_inventory_immutable'); END;

-- Transient assertion lives only inside the atomic batch, never across IO.
CREATE TABLE cf_memory_privacy_apply_guard (
  uid TEXT PRIMARY KEY NOT NULL,
  token TEXT NOT NULL,
  expected_control_json TEXT,
  account_generation INTEGER NOT NULL,
  requested_ids_json TEXT NOT NULL CHECK (json_valid(requested_ids_json)),
  expected_items_json TEXT NOT NULL CHECK (json_valid(expected_items_json))
);
CREATE TRIGGER cf_memory_privacy_apply_admission BEFORE INSERT ON cf_memory_privacy_apply_guard
BEGIN
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
    OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
    THEN RAISE(ABORT, 'memory_apply_account_deleted') END);
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_legal_holds WHERE uid = NEW.uid AND active = 1)
    THEN RAISE(ABORT, 'legal_hold_active') END);
  SELECT (CASE WHEN NOT EXISTS (SELECT 1 FROM cf_destructive_operation_gates
    WHERE uid = NEW.uid AND token = NEW.token AND kind = 'explicit_memory_deletion' AND state = 'running')
    THEN RAISE(ABORT, 'memory_privacy_gate_changed') END);
  SELECT (CASE WHEN NEW.account_generation != COALESCE(
    (SELECT account_generation FROM cf_account_cutover WHERE uid = NEW.uid), 0)
    THEN RAISE(ABORT, 'memory_apply_generation_changed') END);
  SELECT (CASE WHEN (SELECT control_json FROM cf_memory_apply_control WHERE uid = NEW.uid)
    IS NOT NEW.expected_control_json THEN RAISE(ABORT, 'memory_apply_head_changed') END);
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_memory_privacy_deletions WHERE uid = NEW.uid)
    THEN RAISE(ABORT, 'memory_privacy_cleanup_pending') END);
  SELECT (CASE WHEN json_array_length(NEW.expected_items_json) NOT BETWEEN 1 AND 100
    OR json_array_length(NEW.requested_ids_json) = 0
    OR EXISTS (SELECT 1 FROM json_each(NEW.requested_ids_json) requested WHERE NOT EXISTS (
      SELECT 1 FROM cf_memories WHERE uid = NEW.uid AND id = requested.value))
    THEN RAISE(ABORT, 'memory_privacy_targets_changed') END);
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.expected_items_json) e WHERE NOT EXISTS (
      SELECT 1 FROM cf_memories m WHERE m.uid = NEW.uid AND m.id = json_extract(e.value, '$.id')
        AND m.account_generation = NEW.account_generation
        AND m.item_revision = json_extract(e.value, '$.item_revision')
        AND m.version = json_extract(e.value, '$.version')
        AND m.canonical_metadata_json IS json_extract(e.value, '$.canonical_metadata_json')
        AND m.capture_device_ids_json IS json_extract(e.value, '$.capture_device_ids_json')
        AND m.primary_capture_device IS json_extract(e.value, '$.primary_capture_device')
        AND m.status != 'tombstoned'
    )) THEN RAISE(ABORT, 'memory_privacy_targets_changed') END);
  -- Incoming aliases added by a legacy creator do not advance the canonical
  -- head, so rederive the entire connected component inside the transaction.
  SELECT (CASE WHEN EXISTS (
    WITH RECURSIVE lineage(id) AS (
      SELECT value FROM json_each(NEW.requested_ids_json)
      UNION
      SELECT CASE WHEN e.id = l.id THEN e.parent_id ELSE e.id END
      FROM cf_memory_lineage_edges e JOIN lineage l ON e.id = l.id OR e.parent_id = l.id
      WHERE e.uid = NEW.uid
    )
    SELECT id FROM cf_memories WHERE uid = NEW.uid AND id IN (SELECT id FROM lineage)
    EXCEPT SELECT json_extract(value, '$.id') FROM json_each(NEW.expected_items_json)
  ) OR EXISTS (
    WITH RECURSIVE lineage(id) AS (
      SELECT value FROM json_each(NEW.requested_ids_json)
      UNION
      SELECT CASE WHEN e.id = l.id THEN e.parent_id ELSE e.id END
      FROM cf_memory_lineage_edges e JOIN lineage l ON e.id = l.id OR e.parent_id = l.id
      WHERE e.uid = NEW.uid
    )
    SELECT json_extract(value, '$.id') FROM json_each(NEW.expected_items_json)
    EXCEPT SELECT id FROM cf_memories WHERE uid = NEW.uid AND id IN (SELECT id FROM lineage)
  ) THEN RAISE(ABORT, 'memory_privacy_lineage_changed') END);
END;

DROP TRIGGER cf_memories_locked_mutation;
CREATE TRIGGER cf_memories_locked_mutation
BEFORE UPDATE OF content, category, visibility, tags_json, headline, predicate,
  arguments_json, subject_entity_id, subject_attribution, object_entity_ids_json,
  qualifiers_json, capture_confidence, veracity, uncertainty_reasons_json,
  durability, reviewed, user_review, edited, is_read, is_dismissed, is_baseline,
  invalid_at, superseded_by ON cf_memories
WHEN OLD.is_locked != 0 AND NOT (
  NEW.content IS NULL AND NEW.status = 'tombstoned' AND NEW.source_state = 'tombstoned'
  AND NEW.deleted_at IS NOT NULL AND EXISTS (
    SELECT 1 FROM cf_memory_privacy_apply_guard g, json_each(g.expected_items_json) e
    WHERE g.uid = OLD.uid AND json_extract(e.value, '$.id') = OLD.id
      AND json_extract(e.value, '$.item_revision') = OLD.item_revision
  )
)
BEGIN SELECT RAISE(ABORT, 'memory_locked_for_mutation'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_legal_holds BEFORE INSERT ON cf_legal_holds
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_legal_holds BEFORE UPDATE ON cf_legal_holds
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_destructive_operation_gates BEFORE INSERT ON cf_destructive_operation_gates
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_destructive_operation_gates BEFORE UPDATE ON cf_destructive_operation_gates
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_privacy_deletions BEFORE INSERT ON cf_memory_privacy_deletions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_privacy_deletions BEFORE UPDATE ON cf_memory_privacy_deletions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_privacy_apply_guard BEFORE INSERT ON cf_memory_privacy_apply_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_privacy_apply_guard BEFORE UPDATE ON cf_memory_privacy_apply_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
