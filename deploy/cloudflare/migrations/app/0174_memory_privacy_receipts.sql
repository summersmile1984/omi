-- Preserve the upstream content=None privacy tombstone while retaining the
-- nonempty active-memory contract. Copy rows without firing lifecycle/outbox
-- triggers; restore every existing index, view and trigger unchanged. There
-- are no foreign keys referencing cf_memories in the preceding schema.

DROP TRIGGER adf_i_memories;

DROP TRIGGER adf_u_memories;

DROP TRIGGER cf_memories_lifecycle_defaults;

DROP TRIGGER cf_memories_locked_mutation;

DROP VIEW cf_memory_projection_sources;

DROP TRIGGER cf_memory_projection_created;

DROP TRIGGER cf_memory_projection_changed;

DROP TRIGGER cf_memory_projection_removed;

DROP TRIGGER cf_memory_vector_mapping_revision;

DROP TRIGGER cf_memory_vector_mapping_delete;

DROP TRIGGER cf_memory_apply_admission;

DROP TRIGGER cf_memory_apply_edit_admission;

CREATE TABLE cf_memories_privacy (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  content TEXT CHECK (
    (content IS NOT NULL AND length(content) BETWEEN 1 AND 50000) OR
    (content IS NULL AND status = 'tombstoned' AND source_state = 'tombstoned' AND deleted_at IS NOT NULL)
  ),
  category TEXT NOT NULL DEFAULT 'interesting' CHECK (category IN (
    'interesting', 'system', 'manual', 'workflow'
  )),
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('public', 'private')),
  tags_json TEXT NOT NULL DEFAULT '[]',
  headline TEXT,
  predicate TEXT,
  arguments_json TEXT NOT NULL DEFAULT '{}',
  subject_entity_id TEXT,
  subject_attribution TEXT NOT NULL DEFAULT 'unknown' CHECK (subject_attribution IN (
    'user', 'third_party', 'unknown', 'legacy_assumed'
  )),
  object_entity_ids_json TEXT NOT NULL DEFAULT '[]',
  qualifiers_json TEXT NOT NULL DEFAULT '{}',
  capture_confidence REAL,
  veracity REAL,
  uncertainty_reasons_json TEXT NOT NULL DEFAULT '[]',
  durability TEXT,
  conversation_id TEXT,
  reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
  user_review INTEGER CHECK (user_review IS NULL OR user_review IN (0, 1)),
  manually_added INTEGER NOT NULL DEFAULT 0 CHECK (manually_added IN (0, 1)),
  edited INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
  scoring TEXT,
  app_id TEXT,
  data_protection_level TEXT,
  is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
  is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
  is_dismissed INTEGER NOT NULL DEFAULT 0 CHECK (is_dismissed IN (0, 1)),
  kg_extracted INTEGER NOT NULL DEFAULT 0 CHECK (kg_extracted IN (0, 1)),
  is_baseline INTEGER NOT NULL DEFAULT 0 CHECK (is_baseline IN (0, 1)),
  memory_tier TEXT NOT NULL CHECK (memory_tier IN ('short_term', 'long_term', 'archive')),
  valid_at INTEGER NOT NULL,
  invalid_at INTEGER,
  superseded_by TEXT,
  primary_capture_device TEXT,
  capture_device_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted_at INTEGER, version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0), status TEXT NOT NULL DEFAULT 'active'
  CHECK (status IN ('active', 'superseded', 'hidden', 'tombstoned')), processing_state TEXT NOT NULL DEFAULT 'processed'
  CHECK (processing_state IN ('pending', 'processed', 'blocked')), source_state TEXT NOT NULL DEFAULT 'active'
  CHECK (source_state IN ('active', 'tombstoned', 'purged')), sensitivity_labels_json TEXT NOT NULL DEFAULT '[]'
  CHECK (length(sensitivity_labels_json) <= 4096 AND json_valid(sensitivity_labels_json)), user_asserted INTEGER NOT NULL DEFAULT 0
  CHECK (user_asserted IN (0, 1)), captured_at INTEGER NOT NULL DEFAULT 0
  CHECK (captured_at >= 0), expires_at INTEGER
  CHECK (expires_at IS NULL OR expires_at >= 0), item_revision INTEGER NOT NULL DEFAULT 1
  CHECK (item_revision > 0), account_generation INTEGER NOT NULL DEFAULT 0
  CHECK (account_generation >= 0), evidence_json TEXT NOT NULL DEFAULT '[]'
  CHECK (length(evidence_json) <= 65536 AND json_valid(evidence_json)), data_protection_source_revision TEXT, canonical_metadata_json TEXT NOT NULL DEFAULT '{}'
  CHECK (json_valid(canonical_metadata_json) AND json_type(canonical_metadata_json) = 'object'),
  PRIMARY KEY (uid, id)
);

INSERT INTO cf_memories_privacy(rowid, uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, veracity, uncertainty_reasons_json, durability, conversation_id, reviewed, user_review, manually_added, edited, scoring, app_id, data_protection_level, is_locked, is_read, is_dismissed, kg_extracted, is_baseline, memory_tier, valid_at, invalid_at, superseded_by, primary_capture_device, capture_device_ids_json, created_at, updated_at, deleted_at, version, status, processing_state, source_state, sensitivity_labels_json, user_asserted, captured_at, expires_at, item_revision, account_generation, evidence_json, data_protection_source_revision, canonical_metadata_json)
SELECT rowid, uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, veracity, uncertainty_reasons_json, durability, conversation_id, reviewed, user_review, manually_added, edited, scoring, app_id, data_protection_level, is_locked, is_read, is_dismissed, kg_extracted, is_baseline, memory_tier, valid_at, invalid_at, superseded_by, primary_capture_device, capture_device_ids_json, created_at, updated_at, deleted_at, version, status, processing_state, source_state, sensitivity_labels_json, user_asserted, captured_at, expires_at, item_revision, account_generation, evidence_json, data_protection_source_revision, canonical_metadata_json FROM cf_memories;

DROP TABLE cf_memories;

ALTER TABLE cf_memories_privacy RENAME TO cf_memories;

CREATE INDEX cf_memories_uid_active_updated_idx
  ON cf_memories(uid, deleted_at, invalid_at, updated_at DESC, id DESC);

CREATE INDEX cf_memories_uid_category_updated_idx
  ON cf_memories(uid, category, updated_at DESC, id DESC);

CREATE TRIGGER adf_i_memories
BEFORE INSERT ON cf_memories
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER adf_u_memories
BEFORE UPDATE ON cf_memories
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE INDEX cf_memories_short_term_lifecycle_idx
  ON cf_memories(
    uid, account_generation, memory_tier, status, processing_state,
    deleted_at, invalid_at, captured_at, expires_at, id
  );

CREATE TRIGGER cf_memories_lifecycle_defaults
AFTER INSERT ON cf_memories
WHEN NEW.captured_at = 0 OR (NEW.expires_at IS NULL AND NEW.memory_tier = 'short_term')
BEGIN
  UPDATE cf_memories
  SET captured_at = CASE
        WHEN NEW.captured_at = 0 THEN NEW.valid_at
        ELSE NEW.captured_at
      END,
      expires_at = CASE
        WHEN NEW.expires_at IS NULL AND NEW.memory_tier = 'short_term'
          THEN (CASE WHEN NEW.captured_at = 0 THEN NEW.valid_at ELSE NEW.captured_at END) + 172800
        ELSE NEW.expires_at
      END,
      account_generation = CASE
        WHEN NEW.account_generation = 0 THEN COALESCE(
          (SELECT account_generation FROM cf_account_cutover
           WHERE cf_account_cutover.uid = NEW.uid),
          0
        )
        ELSE NEW.account_generation
      END
  WHERE uid = NEW.uid AND id = NEW.id;
END;

CREATE INDEX cf_memories_data_protection_idx
  ON cf_memories(uid, data_protection_level, data_protection_source_revision, id);

CREATE TRIGGER cf_memories_locked_mutation
BEFORE UPDATE OF content, category, visibility, tags_json, headline, predicate,
  arguments_json, subject_entity_id, subject_attribution, object_entity_ids_json,
  qualifiers_json, capture_confidence, veracity, uncertainty_reasons_json,
  durability, reviewed, user_review, edited, is_read, is_dismissed, is_baseline,
  invalid_at, superseded_by
ON cf_memories
WHEN OLD.is_locked != 0
BEGIN SELECT RAISE(ABORT, 'memory_locked_for_mutation'); END;

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

CREATE TRIGGER cf_memory_vector_mapping_revision
AFTER UPDATE OF item_revision ON cf_memories
BEGIN
  DELETE FROM cf_vector_projection_state
  WHERE uid = NEW.uid AND source_id = NEW.id AND projection_kind = 'memory'
    AND source_version != NEW.item_revision;
END;

CREATE TRIGGER cf_memory_vector_mapping_delete
AFTER DELETE ON cf_memories
BEGIN
  DELETE FROM cf_vector_projection_state
  WHERE uid = OLD.uid AND source_id = OLD.id AND projection_kind = 'memory';
END;

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

CREATE TRIGGER cf_memory_apply_edit_admission BEFORE INSERT ON cf_memory_apply_guard
WHEN json_array_length(NEW.expected_items_json) > 0
BEGIN
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM cf_memories m JOIN json_each(NEW.expected_items_json) e
      ON m.id = json_extract(e.value, '$.id')
    WHERE m.uid = NEW.uid AND m.is_locked != 0
  ) THEN RAISE(ABORT, 'memory_locked_for_mutation') END);
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.expected_items_json) e
    WHERE NOT EXISTS (
      SELECT 1 FROM cf_memories m
      WHERE m.uid = NEW.uid AND m.id = json_extract(e.value, '$.id')
        AND m.deleted_at IS NULL AND m.invalid_at IS NULL AND m.source_state = 'active'
        AND m.status = 'active' AND m.superseded_by IS NULL
        AND m.account_generation = NEW.account_generation
        AND m.item_revision = json_extract(e.value, '$.item_revision')
        AND m.version = json_extract(e.value, '$.version')
        AND m.canonical_metadata_json IS json_extract(e.value, '$.canonical_metadata_json')
        AND m.capture_device_ids_json IS json_extract(e.value, '$.capture_device_ids_json')
        AND m.primary_capture_device IS json_extract(e.value, '$.primary_capture_device')
    )
  ) THEN RAISE(ABORT, 'memory_apply_target_changed') END);
END;

-- Retain only a server-keyed identity after canonical privacy finalization.
-- Current creators always supply the key; existing rows need no bulk backfill.
ALTER TABLE cf_memories ADD COLUMN privacy_receipt_id TEXT
  CHECK (privacy_receipt_id IS NULL OR (
    length(privacy_receipt_id) = 72 AND substr(privacy_receipt_id, 1, 8) = 'receipt_'
    AND substr(privacy_receipt_id, 9) NOT GLOB '*[^0-9a-f]*'
  ));

CREATE TABLE cf_memory_privacy_receipts (
  uid TEXT NOT NULL,
  receipt_id TEXT NOT NULL CHECK (
    length(receipt_id) = 72 AND substr(receipt_id, 1, 8) = 'receipt_'
    AND substr(receipt_id, 9) NOT GLOB '*[^0-9a-f]*'
  ),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL CHECK (expires_at = created_at + 2592000),
  PRIMARY KEY (uid, receipt_id)
);
CREATE INDEX cf_memory_privacy_receipts_expiry ON cf_memory_privacy_receipts(uid, expires_at);

CREATE TRIGGER IF NOT EXISTS adf_i_memory_privacy_receipts
BEFORE INSERT ON cf_memory_privacy_receipts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memory_privacy_receipts
BEFORE UPDATE ON cf_memory_privacy_receipts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

-- Seal only an already-scrubbed tombstone. Its identity remains available for
-- provider-cleanup retries until physical finalization removes the memory row.
CREATE TRIGGER cf_memory_privacy_receipt_admission BEFORE INSERT ON cf_memory_privacy_receipts
BEGIN
  SELECT (CASE WHEN NOT EXISTS (
    SELECT 1 FROM cf_memories m WHERE m.uid = NEW.uid AND m.privacy_receipt_id = NEW.receipt_id
      AND m.status = 'tombstoned' AND m.source_state = 'tombstoned'
      AND m.deleted_at IS NOT NULL AND m.content IS NULL
  ) THEN RAISE(ABORT, 'memory_privacy_requires_tombstone') END);
END;

CREATE TRIGGER cf_memory_privacy_receipt_immutable BEFORE UPDATE ON cf_memory_privacy_receipts
BEGIN
  SELECT RAISE(ABORT, 'memory_privacy_receipt_immutable');
END;

-- Old/in-flight writers lacking the key cannot reinsert an erased identity.
-- Accounts with no live receipt retain the existing legacy-row import contract.
CREATE TRIGGER cf_memory_privacy_insert_admission BEFORE INSERT ON cf_memories
WHEN EXISTS (
  SELECT 1 FROM cf_memory_privacy_receipts r WHERE r.uid = NEW.uid AND r.expires_at > unixepoch()
    AND (NEW.privacy_receipt_id IS NULL OR r.receipt_id = NEW.privacy_receipt_id)
)
BEGIN
  SELECT RAISE(ABORT, 'memory_privacy_deleted');
END;

CREATE TRIGGER cf_memory_privacy_update_admission BEFORE UPDATE ON cf_memories
WHEN EXISTS (
  SELECT 1 FROM cf_memory_privacy_receipts r WHERE r.uid = OLD.uid AND r.expires_at > unixepoch()
    AND r.receipt_id = OLD.privacy_receipt_id
)
BEGIN
  SELECT RAISE(ABORT, 'memory_privacy_deleted');
END;

CREATE TRIGGER cf_memory_privacy_identity_immutable BEFORE UPDATE OF uid, id, privacy_receipt_id ON cf_memories
WHEN NEW.uid IS NOT OLD.uid OR NEW.id IS NOT OLD.id
  OR (OLD.privacy_receipt_id IS NOT NULL AND NEW.privacy_receipt_id IS NOT OLD.privacy_receipt_id)
BEGIN
  SELECT RAISE(ABORT, 'memory_privacy_identity_changed');
END;
