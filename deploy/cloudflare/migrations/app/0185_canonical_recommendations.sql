-- Original recommendation head and job lease snapshots join the Candidate
-- generation/deletion transaction owner. Projection history remains in its
-- existing table, published atomically with attributable interventions.
CREATE TABLE cf_task_recommendation_heads (
  uid TEXT NOT NULL,
  head_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  PRIMARY KEY(uid,head_id),
  CHECK(account_generation>=0 AND account_generation IS NOT NULL)
);
ALTER TABLE cf_candidate_write_guard ADD COLUMN recommendations_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(recommendations_json) AND json_type(recommendations_json)='array');
ALTER TABLE cf_candidate_write_guard ADD COLUMN recommendation_jobs_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(recommendation_jobs_json) AND json_type(recommendation_jobs_json)='array');
CREATE TRIGGER cf_candidate_guard_recommendations BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.recommendations_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_task_recommendation_heads WHERE uid=NEW.uid AND head_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
CREATE TRIGGER cf_task_recommendation_head_insert BEFORE INSERT ON cf_task_recommendation_heads
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.recommendations_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.head_id
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_i_task_recommendation_heads BEFORE INSERT ON cf_task_recommendation_heads
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
CREATE TRIGGER cf_task_recommendation_head_update BEFORE UPDATE ON cf_task_recommendation_heads
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.recommendations_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.head_id
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_u_task_recommendation_heads BEFORE UPDATE ON cf_task_recommendation_heads
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
CREATE TRIGGER cf_candidate_guard_recommendation_jobs BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.recommendation_jobs_json) expected
    WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
      THEN EXISTS(SELECT 1 FROM cf_task_intelligence_jobs WHERE uid=NEW.uid AND job_id=json_extract(expected.value,'$.id'))
      ELSE NOT EXISTS(SELECT 1 FROM cf_task_intelligence_jobs m WHERE m.uid=NEW.uid
        AND m.job_id IS json_extract(expected.value,'$.before.job_id')
        AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')
        AND m.device_id IS json_extract(expected.value,'$.before.device_id')
        AND m.request_fingerprint IS json_extract(expected.value,'$.before.request_fingerprint')
        AND m.status IS json_extract(expected.value,'$.before.status')
        AND m.attempts IS json_extract(expected.value,'$.before.attempts')
        AND m.lease_token IS json_extract(expected.value,'$.before.lease_token')
        AND m.lease_until IS json_extract(expected.value,'$.before.lease_until')
        AND m.next_attempt_at IS json_extract(expected.value,'$.before.next_attempt_at')
        AND m.last_error IS json_extract(expected.value,'$.before.last_error')
        AND m.result_json IS json_extract(expected.value,'$.before.result_json')
        AND m.created_at IS json_extract(expected.value,'$.before.created_at')
        AND m.updated_at IS json_extract(expected.value,'$.before.updated_at')
        AND m.input_json IS json_extract(expected.value,'$.before.input_json') ) END
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
