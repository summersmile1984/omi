-- Canonical cross-surface attention policy. Existing feedback/intervention
-- tables remain the single physical owners; their exact snapshots join CAS.
CREATE TABLE cf_task_attention_overrides (
  uid TEXT NOT NULL,
  override_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  expires_at INTEGER GENERATED ALWAYS AS (unixepoch(json_extract(record_json,'$.expires_at'))) STORED,
  PRIMARY KEY(uid,override_id),
  CHECK(account_generation>=0 AND account_generation IS NOT NULL),
  CHECK(expires_at IS NOT NULL)
);
CREATE INDEX cf_task_attention_active ON cf_task_attention_overrides(uid,account_generation,expires_at);
ALTER TABLE cf_candidate_write_guard ADD COLUMN attention_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(attention_json) AND json_type(attention_json)='array');
ALTER TABLE cf_candidate_write_guard ADD COLUMN interventions_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(interventions_json) AND json_type(interventions_json)='array');
ALTER TABLE cf_candidate_write_guard ADD COLUMN feedback_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(feedback_json) AND json_type(feedback_json)='array');

CREATE TRIGGER cf_candidate_guard_attention BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.attention_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_task_attention_overrides WHERE uid=NEW.uid AND override_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_task_attention_overrides_insert BEFORE INSERT ON cf_task_attention_overrides
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.attention_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.override_id
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;

CREATE TRIGGER cf_task_attention_overrides_update BEFORE UPDATE ON cf_task_attention_overrides
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.attention_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.override_id
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;



CREATE TRIGGER cf_candidate_guard_interventions BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.interventions_json) expected
    WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
      THEN EXISTS(SELECT 1 FROM cf_task_interventions WHERE uid=NEW.uid AND intervention_id=json_extract(expected.value,'$.id'))
      ELSE NOT EXISTS(SELECT 1 FROM cf_task_interventions m WHERE m.uid=NEW.uid
        AND m.intervention_id IS json_extract(expected.value,'$.before.intervention_id')
        AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')
        AND m.attribution_chain_id IS json_extract(expected.value,'$.before.attribution_chain_id')
        AND m.request_fingerprint IS json_extract(expected.value,'$.before.request_fingerprint')
        AND m.payload_json IS json_extract(expected.value,'$.before.payload_json')
        AND m.created_at IS json_extract(expected.value,'$.before.created_at')) END
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_candidate_guard_feedback BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.feedback_json) expected
    WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
      THEN EXISTS(SELECT 1 FROM cf_task_feedback WHERE uid=NEW.uid AND feedback_id=json_extract(expected.value,'$.id'))
      ELSE NOT EXISTS(SELECT 1 FROM cf_task_feedback m WHERE m.uid=NEW.uid
        AND m.feedback_id IS json_extract(expected.value,'$.before.feedback_id')
        AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')
        AND m.intervention_id IS json_extract(expected.value,'$.before.intervention_id')
        AND m.attribution_chain_id IS json_extract(expected.value,'$.before.attribution_chain_id')
        AND m.request_fingerprint IS json_extract(expected.value,'$.before.request_fingerprint')
        AND m.payload_json IS json_extract(expected.value,'$.before.payload_json')
        AND m.created_at IS json_extract(expected.value,'$.before.created_at')) END
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_attention_overrides
BEFORE INSERT ON cf_task_attention_overrides
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_attention_overrides
BEFORE UPDATE ON cf_task_attention_overrides
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
