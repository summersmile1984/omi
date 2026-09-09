-- Finalization is admitted only after the artifact owner has removed every
-- external-write receipt and serving mapping for the selected memories.
CREATE TABLE cf_memory_privacy_finalize_guard (
  uid TEXT PRIMARY KEY NOT NULL,
  token TEXT NOT NULL
);
CREATE TRIGGER cf_memory_privacy_finalize_admission BEFORE INSERT ON cf_memory_privacy_finalize_guard
BEGIN
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
    OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
    THEN RAISE(ABORT, 'memory_apply_account_deleted') END);
  SELECT (CASE WHEN EXISTS (SELECT 1 FROM cf_legal_holds WHERE uid = NEW.uid AND active = 1)
    THEN RAISE(ABORT, 'legal_hold_active') END);
  SELECT (CASE WHEN NOT EXISTS (SELECT 1 FROM cf_destructive_operation_gates
    WHERE uid = NEW.uid AND token = NEW.token AND kind = 'explicit_memory_deletion' AND state = 'running')
    THEN RAISE(ABORT, 'memory_privacy_gate_changed') END);
  SELECT (CASE WHEN NOT EXISTS (SELECT 1 FROM cf_memory_privacy_deletions WHERE uid = NEW.uid AND token = NEW.token)
    THEN RAISE(ABORT, 'memory_privacy_inventory_changed') END);
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM cf_memory_privacy_deletions d, json_each(d.targets_json) t
    WHERE d.uid = NEW.uid AND NOT EXISTS (
      SELECT 1 FROM cf_memories m WHERE m.uid = d.uid AND m.id = json_extract(t.value, '$.id')
        AND m.item_revision = json_extract(t.value, '$.item_revision')
        AND m.privacy_receipt_id = json_extract(t.value, '$.receipt_id')
        AND m.content IS NULL AND m.status = 'tombstoned' AND m.source_state = 'tombstoned'
        AND m.deleted_at IS NOT NULL
    )
  ) THEN RAISE(ABORT, 'memory_privacy_tombstone_changed') END);
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM cf_memory_privacy_deletions d, json_each(d.targets_json) t
    WHERE d.uid = NEW.uid AND (
      EXISTS (SELECT 1 FROM cf_memory_vector_artifacts a WHERE a.uid = d.uid AND a.source_id = json_extract(t.value, '$.id'))
      OR EXISTS (SELECT 1 FROM cf_vector_projection_state s WHERE s.uid = d.uid AND s.projection_kind = 'memory'
        AND s.source_id = json_extract(t.value, '$.id'))
    )
  ) THEN RAISE(ABORT, 'memory_privacy_provider_pending') END);
END;

-- Pending cleanup outlives the 30-day receipt TTL. Its trusted inventory keeps
-- delayed creators from reopening a tombstone even if a receipt has expired.
CREATE TRIGGER cf_memory_privacy_pending_insert BEFORE INSERT ON cf_memories
WHEN EXISTS (SELECT 1 FROM cf_memory_privacy_deletions d, json_each(d.targets_json) t
  WHERE d.uid = NEW.uid AND json_extract(t.value, '$.id') = NEW.id)
BEGIN SELECT RAISE(ABORT, 'memory_privacy_cleanup_pending'); END;
CREATE TRIGGER cf_memory_privacy_pending_update BEFORE UPDATE ON cf_memories
WHEN EXISTS (SELECT 1 FROM cf_memory_privacy_deletions d, json_each(d.targets_json) t
  WHERE d.uid = OLD.uid AND json_extract(t.value, '$.id') = OLD.id)
  AND NOT EXISTS (SELECT 1 FROM cf_memory_privacy_apply_guard WHERE uid = OLD.uid)
BEGIN SELECT RAISE(ABORT, 'memory_privacy_cleanup_pending'); END;
CREATE TRIGGER cf_memory_privacy_pending_delete BEFORE DELETE ON cf_memories
WHEN EXISTS (SELECT 1 FROM cf_memory_privacy_deletions d, json_each(d.targets_json) t
  WHERE d.uid = OLD.uid AND json_extract(t.value, '$.id') = OLD.id)
  AND NOT EXISTS (SELECT 1 FROM cf_memory_privacy_finalize_guard WHERE uid = OLD.uid)
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = OLD.uid)
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = OLD.uid)
BEGIN SELECT RAISE(ABORT, 'memory_privacy_cleanup_pending'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_privacy_finalize_guard BEFORE INSERT ON cf_memory_privacy_finalize_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_memory_privacy_finalize_guard BEFORE UPDATE ON cf_memory_privacy_finalize_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

ALTER TABLE cf_memory_privacy_deletions ADD COLUMN last_attempt_at INTEGER NOT NULL DEFAULT 0;


ALTER TABLE cf_memory_privacy_deletions ADD COLUMN expand_lineages INTEGER NOT NULL DEFAULT 1 CHECK (expand_lineages IN (0, 1));
ALTER TABLE cf_memory_privacy_apply_guard ADD COLUMN expand_lineages INTEGER NOT NULL DEFAULT 1 CHECK (expand_lineages IN (0, 1));
DROP TRIGGER cf_memory_privacy_apply_admission;
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
  SELECT (CASE WHEN NEW.expand_lineages = 1 AND (EXISTS (
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
  )) THEN RAISE(ABORT, 'memory_privacy_lineage_changed') END);
END;

DROP TRIGGER cf_memory_privacy_deletion_immutable;
CREATE TRIGGER cf_memory_privacy_deletion_immutable BEFORE UPDATE ON cf_memory_privacy_deletions
WHEN OLD.uid IS NOT NEW.uid OR OLD.token IS NOT NEW.token
  OR OLD.requested_ids_json IS NOT NEW.requested_ids_json OR OLD.targets_json IS NOT NEW.targets_json
  OR OLD.created_at IS NOT NEW.created_at OR OLD.expand_lineages IS NOT NEW.expand_lineages
BEGIN SELECT RAISE(ABORT, 'memory_privacy_inventory_immutable'); END;

-- Delete-all/default requests survive between bounded batches and provider IO.
CREATE TABLE cf_memory_privacy_scopes (
  uid TEXT PRIMARY KEY NOT NULL,
  token TEXT NOT NULL CHECK (length(token) = 64 AND token NOT GLOB '*[^0-9a-f]*'),
  scope TEXT NOT NULL CHECK (scope IN ('all', 'default')),
  created_at INTEGER NOT NULL,
  last_attempt_at INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER cf_memory_privacy_scope_immutable BEFORE UPDATE ON cf_memory_privacy_scopes
WHEN OLD.uid IS NOT NEW.uid OR OLD.token IS NOT NEW.token OR OLD.scope IS NOT NEW.scope OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'memory_privacy_scope_immutable'); END;
CREATE TRIGGER IF NOT EXISTS adf_i_memory_privacy_scopes BEFORE INSERT ON cf_memory_privacy_scopes
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_memory_privacy_scopes BEFORE UPDATE ON cf_memory_privacy_scopes
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
