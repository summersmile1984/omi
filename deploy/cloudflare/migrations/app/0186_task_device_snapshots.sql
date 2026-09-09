-- Preserve existing snapshot rows while giving each account generation and
-- open-loop runtime/workstream an independent current-state owner.
CREATE TABLE cf_task_context_snapshots_next (
  uid TEXT NOT NULL,
  device_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK(account_generation>=0),
  snapshot_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  generated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  receipt_id TEXT,
  scope_key TEXT GENERATED ALWAYS AS (json_array(account_generation,device_id)) STORED NOT NULL,
  UNIQUE(uid,scope_key)
);
INSERT INTO cf_task_context_snapshots_next(uid,device_id,account_generation,snapshot_id,request_fingerprint,payload_json,generated_at,expires_at,updated_at) SELECT uid,device_id,account_generation,snapshot_id,request_fingerprint,payload_json,generated_at,expires_at,updated_at FROM cf_task_context_snapshots;
DROP TABLE cf_task_context_snapshots;
ALTER TABLE cf_task_context_snapshots_next RENAME TO cf_task_context_snapshots;
CREATE INDEX cf_task_context_snapshots_device_idx ON cf_task_context_snapshots(uid,account_generation,device_id);
CREATE TRIGGER IF NOT EXISTS adf_i_task_context_snapshots
BEFORE INSERT ON cf_task_context_snapshots
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN
  SELECT RAISE(ABORT,'account deletion fence');
END;
CREATE TRIGGER IF NOT EXISTS adf_u_task_context_snapshots
BEFORE UPDATE ON cf_task_context_snapshots
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN
  SELECT RAISE(ABORT,'account deletion fence');
END;
ALTER TABLE cf_candidate_write_guard ADD COLUMN context_snapshots_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(context_snapshots_json) AND json_type(context_snapshots_json)='array');
CREATE TRIGGER cf_candidate_guard_context_snapshots BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.context_snapshots_json) expected
    WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
      THEN EXISTS(SELECT 1 FROM cf_task_context_snapshots WHERE uid=NEW.uid AND scope_key=json_extract(expected.value,'$.id'))
      ELSE NOT EXISTS(SELECT 1 FROM cf_task_context_snapshots m WHERE m.uid=NEW.uid
        AND m.device_id IS json_extract(expected.value,'$.before.device_id')
        AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')
        AND m.snapshot_id IS json_extract(expected.value,'$.before.snapshot_id')
        AND m.request_fingerprint IS json_extract(expected.value,'$.before.request_fingerprint')
        AND m.payload_json IS json_extract(expected.value,'$.before.payload_json')
        AND m.generated_at IS json_extract(expected.value,'$.before.generated_at')
        AND m.expires_at IS json_extract(expected.value,'$.before.expires_at')
        AND m.updated_at IS json_extract(expected.value,'$.before.updated_at')
        AND m.receipt_id IS json_extract(expected.value,'$.before.receipt_id')
        AND m.scope_key IS json_extract(expected.value,'$.before.scope_key')
      ) END
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
CREATE TRIGGER cf_candidate_context_snapshots_insert BEFORE INSERT ON cf_task_context_snapshots
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.context_snapshots_json) expected
    WHERE guard.uid=NEW.uid AND guard.account_generation=NEW.account_generation
      AND json_extract(expected.value,'$.id')=json_array(NEW.account_generation,NEW.device_id)
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER cf_candidate_context_snapshots_update BEFORE UPDATE ON cf_task_context_snapshots
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.context_snapshots_json) expected
    WHERE guard.uid=NEW.uid AND guard.account_generation=NEW.account_generation
      AND json_extract(expected.value,'$.id')=json_array(NEW.account_generation,NEW.device_id)
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TABLE cf_task_open_loop_snapshots_next (
  uid TEXT NOT NULL,
  device_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK(account_generation>=0),
  snapshot_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  generated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  receipt_id TEXT,
  scope_key TEXT GENERATED ALWAYS AS (json_array(account_generation,device_id,json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.runtime_id'),json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.workstream_id'))) STORED NOT NULL,
  UNIQUE(uid,scope_key)
);
INSERT INTO cf_task_open_loop_snapshots_next(uid,device_id,account_generation,snapshot_id,request_fingerprint,payload_json,generated_at,expires_at,updated_at) SELECT uid,device_id,account_generation,snapshot_id,request_fingerprint,payload_json,generated_at,expires_at,updated_at FROM cf_task_open_loop_snapshots;
DROP TABLE cf_task_open_loop_snapshots;
ALTER TABLE cf_task_open_loop_snapshots_next RENAME TO cf_task_open_loop_snapshots;
CREATE INDEX cf_task_open_loop_snapshots_device_idx ON cf_task_open_loop_snapshots(uid,account_generation,device_id);
CREATE TRIGGER IF NOT EXISTS adf_i_task_open_loop_snapshots
BEFORE INSERT ON cf_task_open_loop_snapshots
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN
  SELECT RAISE(ABORT,'account deletion fence');
END;
CREATE TRIGGER IF NOT EXISTS adf_u_task_open_loop_snapshots
BEFORE UPDATE ON cf_task_open_loop_snapshots
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN
  SELECT RAISE(ABORT,'account deletion fence');
END;
ALTER TABLE cf_candidate_write_guard ADD COLUMN open_loop_snapshots_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(open_loop_snapshots_json) AND json_type(open_loop_snapshots_json)='array');
CREATE TRIGGER cf_candidate_guard_open_loop_snapshots BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.open_loop_snapshots_json) expected
    WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
      THEN EXISTS(SELECT 1 FROM cf_task_open_loop_snapshots WHERE uid=NEW.uid AND scope_key=json_extract(expected.value,'$.id'))
      ELSE NOT EXISTS(SELECT 1 FROM cf_task_open_loop_snapshots m WHERE m.uid=NEW.uid
        AND m.device_id IS json_extract(expected.value,'$.before.device_id')
        AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')
        AND m.snapshot_id IS json_extract(expected.value,'$.before.snapshot_id')
        AND m.request_fingerprint IS json_extract(expected.value,'$.before.request_fingerprint')
        AND m.payload_json IS json_extract(expected.value,'$.before.payload_json')
        AND m.generated_at IS json_extract(expected.value,'$.before.generated_at')
        AND m.expires_at IS json_extract(expected.value,'$.before.expires_at')
        AND m.updated_at IS json_extract(expected.value,'$.before.updated_at')
        AND m.receipt_id IS json_extract(expected.value,'$.before.receipt_id')
        AND m.scope_key IS json_extract(expected.value,'$.before.scope_key')
      ) END
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
CREATE TRIGGER cf_candidate_open_loop_snapshots_insert BEFORE INSERT ON cf_task_open_loop_snapshots
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.open_loop_snapshots_json) expected
    WHERE guard.uid=NEW.uid AND guard.account_generation=NEW.account_generation
      AND json_extract(expected.value,'$.id')=json_array(NEW.account_generation,NEW.device_id,json_extract(NEW.payload_json,'$.runtime_id'),json_extract(NEW.payload_json,'$.workstream_id'))
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER cf_candidate_open_loop_snapshots_update BEFORE UPDATE ON cf_task_open_loop_snapshots
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.open_loop_snapshots_json) expected
    WHERE guard.uid=NEW.uid AND guard.account_generation=NEW.account_generation
      AND json_extract(expected.value,'$.id')=json_array(NEW.account_generation,NEW.device_id,json_extract(NEW.payload_json,'$.runtime_id'),json_extract(NEW.payload_json,'$.workstream_id'))
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TABLE cf_task_snapshot_receipts (
 uid TEXT NOT NULL, receipt_id TEXT NOT NULL,
 record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
 account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
 expires_at INTEGER GENERATED ALWAYS AS (unixepoch(json_extract(record_json,'$.expires_at'))) STORED,
 PRIMARY KEY(uid,receipt_id),
 CHECK(account_generation>=0 AND account_generation IS NOT NULL AND expires_at IS NOT NULL)
);
CREATE INDEX cf_task_snapshot_receipts_expiry_idx ON cf_task_snapshot_receipts(uid,expires_at);
ALTER TABLE cf_candidate_write_guard ADD COLUMN snapshot_receipts_json TEXT NOT NULL DEFAULT '[]'
 CHECK(json_valid(snapshot_receipts_json) AND json_type(snapshot_receipts_json)='array');
CREATE TRIGGER cf_candidate_guard_snapshot_receipts BEFORE INSERT ON cf_candidate_write_guard
BEGIN
 SELECT (CASE WHEN EXISTS(
  SELECT 1 FROM json_each(NEW.snapshot_receipts_json) expected
  WHERE json_extract(expected.value,'$.before') IS NOT
   (SELECT record_json FROM cf_task_snapshot_receipts WHERE uid=NEW.uid AND receipt_id=json_extract(expected.value,'$.id'))
 ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
CREATE TRIGGER cf_candidate_snapshot_receipts_i BEFORE INSERT ON cf_task_snapshot_receipts
BEGIN
 SELECT (CASE WHEN NOT EXISTS(
  SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.snapshot_receipts_json) expected
  WHERE guard.uid=NEW.uid AND guard.account_generation=NEW.account_generation
   AND json_extract(expected.value,'$.id')=NEW.receipt_id
 ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_i_task_snapshot_receipts
BEFORE INSERT ON cf_task_snapshot_receipts
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN
 SELECT RAISE(ABORT,'account deletion fence');
END;
CREATE TRIGGER cf_candidate_snapshot_receipts_u BEFORE UPDATE ON cf_task_snapshot_receipts
BEGIN
 SELECT (CASE WHEN NOT EXISTS(
  SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.snapshot_receipts_json) expected
  WHERE guard.uid=NEW.uid AND guard.account_generation=NEW.account_generation
   AND json_extract(expected.value,'$.id')=NEW.receipt_id
 ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_u_task_snapshot_receipts
BEFORE UPDATE ON cf_task_snapshot_receipts
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN
 SELECT RAISE(ABORT,'account deletion fence');
END;
