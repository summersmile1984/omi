-- Outcome attribution, source relationships and result receipts share one exact D1 snapshot.
ALTER TABLE cf_candidate_write_guard ADD COLUMN outcomes_json TEXT NOT NULL DEFAULT '[]'
 CHECK(json_valid(outcomes_json) AND json_type(outcomes_json)='array');
CREATE TRIGGER cf_candidate_guard_outcomes BEFORE INSERT ON cf_candidate_write_guard
BEGIN
 SELECT (CASE WHEN EXISTS(
  SELECT 1 FROM json_each(NEW.outcomes_json) expected
  WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
   THEN EXISTS(SELECT 1 FROM cf_task_outcomes WHERE uid=NEW.uid AND outcome_id=json_extract(expected.value,'$.id'))
   ELSE NOT EXISTS(SELECT 1 FROM cf_task_outcomes m WHERE m.uid=NEW.uid
    AND ((m.outcome_id IS json_extract(expected.value,'$.before.outcome_id') AND (m.account_generation IS json_extract(expected.value,'$.before.account_generation') AND m.attribution_chain_id IS json_extract(expected.value,'$.before.attribution_chain_id'))) AND (m.request_fingerprint IS json_extract(expected.value,'$.before.request_fingerprint') AND (m.payload_json IS json_extract(expected.value,'$.before.payload_json') AND m.occurred_at IS json_extract(expected.value,'$.before.occurred_at'))))) END
 ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
ALTER TABLE cf_candidate_write_guard ADD COLUMN artifacts_json TEXT NOT NULL DEFAULT '[]'
 CHECK(json_valid(artifacts_json) AND json_type(artifacts_json)='array');
CREATE TRIGGER cf_candidate_guard_artifacts BEFORE INSERT ON cf_candidate_write_guard
BEGIN
 SELECT (CASE WHEN EXISTS(
  SELECT 1 FROM json_each(NEW.artifacts_json) expected
  WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
   THEN EXISTS(SELECT 1 FROM cf_workstream_artifacts WHERE uid=NEW.uid AND artifact_id=json_extract(expected.value,'$.id'))
   ELSE NOT EXISTS(SELECT 1 FROM cf_workstream_artifacts m WHERE m.uid=NEW.uid
    AND (((m.artifact_id IS json_extract(expected.value,'$.before.artifact_id') AND (m.workstream_id IS json_extract(expected.value,'$.before.workstream_id') AND m.logical_key IS json_extract(expected.value,'$.before.logical_key'))) AND ((m.version IS json_extract(expected.value,'$.before.version') AND m.supersedes_artifact_id IS json_extract(expected.value,'$.before.supersedes_artifact_id')) AND (m.kind IS json_extract(expected.value,'$.before.kind') AND m.uri IS json_extract(expected.value,'$.before.uri')))) AND ((m.content_hash IS json_extract(expected.value,'$.before.content_hash') AND (m.source_run_id IS json_extract(expected.value,'$.before.source_run_id') AND m.evidence_event_ids_json IS json_extract(expected.value,'$.before.evidence_event_ids_json'))) AND ((m.evidence_refs_json IS json_extract(expected.value,'$.before.evidence_refs_json') AND m.status IS json_extract(expected.value,'$.before.status')) AND (m.created_at IS json_extract(expected.value,'$.before.created_at') AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')))))) END
 ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;
CREATE TRIGGER cf_candidate_outcomes_insert BEFORE INSERT ON cf_task_outcomes
BEGIN
 SELECT (CASE WHEN NOT EXISTS(
  SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.outcomes_json) expected
  WHERE guard.uid=NEW.uid AND guard.account_generation=NEW.account_generation
   AND json_extract(expected.value,'$.id')=NEW.outcome_id
 ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER cf_candidate_outcomes_update BEFORE UPDATE ON cf_task_outcomes
BEGIN
 SELECT (CASE WHEN NOT EXISTS(
  SELECT 1 FROM cf_candidate_write_guard guard,json_each(guard.outcomes_json) expected
  WHERE guard.uid=NEW.uid AND guard.account_generation=NEW.account_generation
   AND json_extract(expected.value,'$.id')=NEW.outcome_id
 ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
