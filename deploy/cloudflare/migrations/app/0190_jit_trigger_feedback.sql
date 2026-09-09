-- Content-free feedback and its planned-notification link join one canonical
-- memory batch. Reuse the existing account/head and Candidate snapshot guards.
CREATE TABLE cf_jit_trigger_feedback (
  uid TEXT NOT NULL,
  feedback_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  PRIMARY KEY(uid,feedback_id),
  CHECK(account_generation >= 0 AND account_generation IS NOT NULL),
  CHECK(json_extract(record_json,'$.uid') IS uid),
  CHECK(json_extract(record_json,'$.feedback_id') IS feedback_id)
);
ALTER TABLE cf_candidate_write_guard ADD COLUMN jit_feedback_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(jit_feedback_json) AND json_type(jit_feedback_json)='array');
CREATE TRIGGER cf_candidate_guard_jit_feedback BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.jit_feedback_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_jit_trigger_feedback WHERE uid=NEW.uid AND feedback_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
CREATE TRIGGER cf_jit_trigger_feedback_insert BEFORE INSERT ON cf_jit_trigger_feedback
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_feedback_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.feedback_id
      AND json_extract(expected.value,'$.before') IS NULL
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_memory_apply_guard guard
    JOIN cf_memory_apply_control control ON control.uid=guard.uid
    JOIN cf_memory_operations operation ON operation.uid=guard.uid
    JOIN cf_memories memory ON memory.uid=guard.uid
    JOIN cf_jit_proactivity_events event ON event.uid=guard.uid
    WHERE guard.uid=NEW.uid AND control.account_generation=NEW.account_generation
      AND operation.operation_id IN (SELECT value FROM json_each(guard.operation_ids_json))
      AND json_extract(operation.operation_json,'$.operation_type')='ledger_mutation'
      AND substr(json_extract(operation.operation_json,'$.source_packet_id'),1,
        length('user_mutation:jit_trigger_feedback:' || NEW.feedback_id || ':'))
        = 'user_mutation:jit_trigger_feedback:' || NEW.feedback_id || ':'
      AND memory.id=json_extract(NEW.record_json,'$.trigger_memory_id')
      AND memory.item_revision=json_extract(NEW.record_json,'$.applied_trigger_revision')
      AND memory.item_revision=json_extract(NEW.record_json,'$.expected_trigger_revision')+1
      AND memory.account_generation=NEW.account_generation
      AND json_extract(memory.canonical_metadata_json,'$.ledger_commit_id')=control.head_commit_id
      AND event.event_id=json_extract(NEW.record_json,'$.event_id')
      AND json_extract(event.record_json,'$.feedback_id')=NEW.feedback_id
  ) THEN RAISE(ABORT,'trigger_feedback_canonical_apply_required') END);
END;
CREATE TRIGGER cf_jit_trigger_feedback_immutable BEFORE UPDATE ON cf_jit_trigger_feedback
BEGIN SELECT RAISE(ABORT,'trigger feedback receipt immutable'); END;
CREATE TRIGGER IF NOT EXISTS adf_i_jit_trigger_feedback BEFORE INSERT ON cf_jit_trigger_feedback
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_jit_trigger_feedback BEFORE UPDATE ON cf_jit_trigger_feedback
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
