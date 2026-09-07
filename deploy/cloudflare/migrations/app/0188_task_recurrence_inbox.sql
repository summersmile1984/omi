-- Canonical workflow inbox; frozen first signals precede memory watermark advancement.
CREATE TABLE cf_task_recurrence_inbox (
 uid TEXT NOT NULL,
 receipt_id TEXT NOT NULL,
 record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
 account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
 status TEXT GENERATED ALWAYS AS (json_extract(record_json,'$.status')) STORED,
 updated_at INTEGER GENERATED ALWAYS AS (unixepoch(json_extract(record_json,'$.updated_at'))) STORED,
 PRIMARY KEY(uid,receipt_id),
 CHECK(account_generation IS NOT NULL AND account_generation>=0),
 CHECK(status IS NOT NULL AND status IN ('pending','completed')),
 CHECK(updated_at IS NOT NULL),
 CHECK(json_extract(record_json,'$.receipt_id') IS receipt_id)
);
CREATE INDEX cf_task_recurrence_pending ON cf_task_recurrence_inbox(status,updated_at,uid,receipt_id);
ALTER TABLE cf_candidate_write_guard ADD COLUMN recurrences_json TEXT NOT NULL DEFAULT '[]'
 CHECK(json_valid(recurrences_json) AND json_type(recurrences_json)='array');
CREATE TRIGGER cf_candidate_guard_recurrences BEFORE INSERT ON cf_candidate_write_guard
BEGIN
 SELECT (CASE WHEN EXISTS(
  SELECT 1 FROM json_each(NEW.recurrences_json) expected
  WHERE json_extract(expected.value,'$.before') IS NOT
   (SELECT record_json FROM cf_task_recurrence_inbox WHERE uid=NEW.uid AND receipt_id=json_extract(expected.value,'$.id'))
 ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
CREATE TRIGGER cf_task_recurrence_insert BEFORE INSERT ON cf_task_recurrence_inbox
BEGIN
 SELECT (CASE WHEN NOT EXISTS(
  SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.recurrences_json) expected
  WHERE guard.uid=NEW.uid AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
   AND json_extract(expected.value,'$.id')=NEW.receipt_id
 ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER cf_task_recurrence_update BEFORE UPDATE ON cf_task_recurrence_inbox
BEGIN
 SELECT (CASE WHEN NOT EXISTS(
  SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.recurrences_json) expected
  WHERE guard.uid=NEW.uid AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
   AND json_extract(expected.value,'$.id')=NEW.receipt_id
 ) THEN RAISE(ABORT,'candidate_apply_required') END);
 SELECT (CASE WHEN OLD.uid IS NOT NEW.uid OR OLD.receipt_id IS NOT NEW.receipt_id
  OR json_extract(OLD.record_json,'$.signal') IS NOT json_extract(NEW.record_json,'$.signal')
  OR json_extract(OLD.record_json,'$.loop_key') IS NOT json_extract(NEW.record_json,'$.loop_key')
  OR json_extract(OLD.record_json,'$.account_generation') IS NOT json_extract(NEW.record_json,'$.account_generation')
  OR json_extract(OLD.record_json,'$.created_at') IS NOT json_extract(NEW.record_json,'$.created_at')
  OR (json_extract(OLD.record_json,'$.status')='completed' AND json_extract(NEW.record_json,'$.status')!='completed')
  THEN RAISE(ABORT,'recurrence_proposal_frozen') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_i_task_recurrence_inbox BEFORE INSERT ON cf_task_recurrence_inbox
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
 OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_task_recurrence_inbox BEFORE UPDATE ON cf_task_recurrence_inbox
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
 OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
