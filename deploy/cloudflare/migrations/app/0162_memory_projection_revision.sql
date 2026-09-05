-- Canonical memory revisions, including projection work, advance in one D1
-- transaction. Wall-clock seconds remain presentation timestamps only.
-- Existing timestamp-based projections are invalidated monotonically.
UPDATE cf_memories
SET item_revision = MAX(
  item_revision, updated_at,
  COALESCE((SELECT MAX(source_version) FROM cf_vector_projection_state s
    WHERE s.uid = cf_memories.uid AND s.source_id = cf_memories.id
      AND s.projection_kind = 'memory'), 0),
  COALESCE((SELECT desired_version FROM cf_vector_projection_outbox o
    WHERE o.uid = cf_memories.uid AND o.source_id = cf_memories.id
      AND o.source_kind = 'memory'), 0)
) + 1
WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = cf_memories.uid)
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones d WHERE d.uid = cf_memories.uid);

CREATE VIEW cf_memory_projection_sources AS
SELECT m.uid, m.id, m.content, m.item_revision,
  CASE WHEN m.deleted_at IS NULL AND m.invalid_at IS NULL
    AND COALESCE(m.user_review, 1) != 0 AND m.is_locked = 0
    AND m.memory_tier IN ('short_term', 'long_term') AND m.status = 'active'
    AND m.processing_state = 'processed' AND m.source_state = 'active'
    AND (m.expires_at IS NULL OR m.expires_at > unixepoch())
    AND m.account_generation = COALESCE(
      (SELECT account_generation FROM cf_account_cutover c WHERE c.uid = m.uid), 0)
    AND json_type(m.sensitivity_labels_json) = 'array'
    AND NOT EXISTS (
      SELECT 1 FROM json_each(m.sensitivity_labels_json) label
      WHERE label.value IN ('credential', 'secret', 'financial', 'health', 'intimate',
        'minor', 'minors', 'workplace_confidential', 'identity_authentication')
    )
    THEN 'upsert' ELSE 'delete' END AS operation
FROM cf_memories m;

CREATE TRIGGER cf_memory_projection_created
AFTER INSERT ON cf_memories
BEGIN
  UPDATE cf_memories SET item_revision = MAX(
    NEW.item_revision,
    COALESCE((SELECT MAX(source_version) FROM cf_vector_projection_state s
      WHERE s.uid = NEW.uid AND s.source_id = NEW.id AND s.projection_kind = 'memory'), 0) + 1,
    COALESCE((SELECT desired_version FROM cf_vector_projection_outbox o
      WHERE o.uid = NEW.uid AND o.source_id = NEW.id AND o.source_kind = 'memory'), 0) + 1
  ) WHERE uid = NEW.uid AND id = NEW.id;
  INSERT INTO cf_vector_projection_outbox
  (uid, source_kind, source_id, desired_version, operation, attempts,
   next_attempt_at, last_error, created_at, updated_at)
SELECT uid, 'memory', id, item_revision, operation, 0, unixepoch(), NULL, unixepoch(), unixepoch()
FROM cf_memory_projection_sources
WHERE uid = NEW.uid AND id = NEW.id
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = cf_memory_projection_sources.uid)
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones d WHERE d.uid = cf_memory_projection_sources.uid)
ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET
  desired_version = excluded.desired_version, operation = excluded.operation,
  attempts = 0, next_attempt_at = excluded.next_attempt_at, last_error = NULL,
  updated_at = excluded.updated_at;
END;

CREATE TRIGGER cf_memory_projection_changed
AFTER UPDATE OF content, category, visibility, tags_json, headline, predicate,
  arguments_json, subject_entity_id, subject_attribution, object_entity_ids_json,
  qualifiers_json, capture_confidence, veracity, uncertainty_reasons_json,
  durability, reviewed, user_review, edited, is_read, is_dismissed, is_baseline,
  invalid_at, superseded_by, is_locked, memory_tier, status, processing_state,
  source_state, sensitivity_labels_json, user_asserted, captured_at, expires_at,
  account_generation, evidence_json, deleted_at, updated_at
ON cf_memories
BEGIN
  UPDATE cf_memories SET item_revision = OLD.item_revision + 1 WHERE uid = NEW.uid AND id = NEW.id;
  INSERT INTO cf_vector_projection_outbox
  (uid, source_kind, source_id, desired_version, operation, attempts,
   next_attempt_at, last_error, created_at, updated_at)
SELECT uid, 'memory', id, item_revision, operation, 0, unixepoch(), NULL, unixepoch(), unixepoch()
FROM cf_memory_projection_sources
WHERE uid = NEW.uid AND id = NEW.id
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = cf_memory_projection_sources.uid)
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones d WHERE d.uid = cf_memory_projection_sources.uid)
ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET
  desired_version = excluded.desired_version, operation = excluded.operation,
  attempts = 0, next_attempt_at = excluded.next_attempt_at, last_error = NULL,
  updated_at = excluded.updated_at;
END;

-- Account erasure has its own Vectorize purge and must not recreate work behind
-- its deletion fence. Other hard deletions (for example conversation merge)
-- leave a durable delete request even after the canonical row is gone.
CREATE TRIGGER cf_memory_projection_removed
AFTER DELETE ON cf_memories
WHEN NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = OLD.uid)
 AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = OLD.uid)
BEGIN
  INSERT INTO cf_vector_projection_outbox
    (uid, source_kind, source_id, desired_version, operation, attempts,
     next_attempt_at, last_error, created_at, updated_at)
  VALUES (OLD.uid, 'memory', OLD.id, OLD.item_revision + 1, 'delete', 0,
    unixepoch(), NULL, unixepoch(), unixepoch())
  ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET
    desired_version = excluded.desired_version, operation = 'delete',
    attempts = 0, next_attempt_at = excluded.next_attempt_at, last_error = NULL,
    updated_at = excluded.updated_at;
END;

INSERT INTO cf_vector_projection_outbox
  (uid, source_kind, source_id, desired_version, operation, attempts,
   next_attempt_at, last_error, created_at, updated_at)
SELECT uid, 'memory', id, item_revision, operation, 0, unixepoch(), NULL, unixepoch(), unixepoch()
FROM cf_memory_projection_sources
WHERE 1
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = cf_memory_projection_sources.uid)
  AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones d WHERE d.uid = cf_memory_projection_sources.uid)
ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET
  desired_version = excluded.desired_version, operation = excluded.operation,
  attempts = 0, next_attempt_at = excluded.next_attempt_at, last_error = NULL,
  updated_at = excluded.updated_at;
