-- Closed history is a read participant; existing active-item write gates stay intact.
CREATE TABLE cf_memory_ledger_read_guard (
  uid TEXT NOT NULL, memory_id TEXT NOT NULL, snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
  PRIMARY KEY(uid,memory_id)
);
CREATE TRIGGER cf_memory_ledger_read_admission BEFORE INSERT ON cf_memory_ledger_read_guard
BEGIN
  SELECT (CASE WHEN NOT EXISTS (SELECT 1 FROM cf_memory_apply_guard WHERE uid=NEW.uid)
    THEN RAISE(ABORT,'memory_ledger_missing_apply') END);
  SELECT (CASE WHEN NOT EXISTS (
    SELECT 1 FROM cf_memories m JOIN cf_memory_apply_guard g ON g.uid=m.uid
    WHERE m.uid=NEW.uid AND m.id=NEW.memory_id
      AND m.account_generation=g.account_generation AND m.deleted_at IS NULL
        AND m.account_generation IS json_extract(NEW.snapshot_json, '$.account_generation')
        AND m.arguments_json IS json_extract(NEW.snapshot_json, '$.arguments_json')
        AND m.canonical_metadata_json IS json_extract(NEW.snapshot_json, '$.canonical_metadata_json')
        AND m.capture_device_ids_json IS json_extract(NEW.snapshot_json, '$.capture_device_ids_json')
        AND m.captured_at IS json_extract(NEW.snapshot_json, '$.captured_at')
        AND m.content IS json_extract(NEW.snapshot_json, '$.content')
        AND m.deleted_at IS json_extract(NEW.snapshot_json, '$.deleted_at')
        AND m.evidence_json IS json_extract(NEW.snapshot_json, '$.evidence_json')
        AND m.expires_at IS json_extract(NEW.snapshot_json, '$.expires_at')
        AND m.id IS json_extract(NEW.snapshot_json, '$.id')
        AND m.invalid_at IS json_extract(NEW.snapshot_json, '$.invalid_at')
        AND m.is_locked IS json_extract(NEW.snapshot_json, '$.is_locked')
        AND m.item_revision IS json_extract(NEW.snapshot_json, '$.item_revision')
        AND m.kg_extracted IS json_extract(NEW.snapshot_json, '$.kg_extracted')
        AND m.memory_tier IS json_extract(NEW.snapshot_json, '$.memory_tier')
        AND m.predicate IS json_extract(NEW.snapshot_json, '$.predicate')
        AND m.primary_capture_device IS json_extract(NEW.snapshot_json, '$.primary_capture_device')
        AND m.processing_state IS json_extract(NEW.snapshot_json, '$.processing_state')
        AND m.sensitivity_labels_json IS json_extract(NEW.snapshot_json, '$.sensitivity_labels_json')
        AND m.source_state IS json_extract(NEW.snapshot_json, '$.source_state')
        AND m.status IS json_extract(NEW.snapshot_json, '$.status')
        AND m.subject_entity_id IS json_extract(NEW.snapshot_json, '$.subject_entity_id')
        AND m.superseded_by IS json_extract(NEW.snapshot_json, '$.superseded_by')
        AND m.uid IS json_extract(NEW.snapshot_json, '$.uid')
        AND m.updated_at IS json_extract(NEW.snapshot_json, '$.updated_at')
        AND m.user_asserted IS json_extract(NEW.snapshot_json, '$.user_asserted')
        AND m.user_review IS json_extract(NEW.snapshot_json, '$.user_review')
        AND m.version IS json_extract(NEW.snapshot_json, '$.version')
        AND m.visibility IS json_extract(NEW.snapshot_json, '$.visibility')
  ) THEN RAISE(ABORT,'memory_ledger_source_changed') END);
END;

-- A closed standalone source can produce only one replacement, even when two
-- devices use different operation UUIDs. Its receipt shares the canonical batch.
CREATE TABLE cf_memory_ledger_reopens (
  uid TEXT NOT NULL, source_memory_id TEXT NOT NULL, replacement_memory_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK(account_generation>=0),
  receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
  PRIMARY KEY(uid,source_memory_id),
  CHECK(json_extract(receipt_json,'$.uid')=uid),
  CHECK(json_extract(receipt_json,'$.source_memory_id')=source_memory_id),
  CHECK(json_extract(receipt_json,'$.replacement_memory_id')=replacement_memory_id),
  CHECK(json_extract(receipt_json,'$.account_generation')=account_generation)
);
CREATE TRIGGER cf_memory_ledger_reopen_admission BEFORE INSERT ON cf_memory_ledger_reopens
BEGIN
  SELECT (CASE WHEN NOT EXISTS (
    SELECT 1 FROM cf_memory_apply_guard g JOIN cf_memories m ON m.uid=g.uid
    WHERE g.uid=NEW.uid AND g.account_generation=NEW.account_generation
      AND m.id=NEW.replacement_memory_id AND m.status='active' AND m.deleted_at IS NULL
  ) THEN RAISE(ABORT,'memory_ledger_missing_apply') END);
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_ledger_read_guard BEFORE INSERT ON cf_memory_ledger_read_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
 OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_memory_ledger_read_guard BEFORE UPDATE ON cf_memory_ledger_read_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
 OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_ledger_reopens BEFORE INSERT ON cf_memory_ledger_reopens
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
 OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_memory_ledger_reopens BEFORE UPDATE ON cf_memory_ledger_reopens
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
 OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
