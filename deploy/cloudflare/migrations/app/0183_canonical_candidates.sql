-- Original universal Candidate records; historical cf_task_candidates stay
-- readable until an explicit staged-row mutation adopts them into this owner.
CREATE TABLE cf_candidates (
  uid TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  status TEXT GENERATED ALWAYS AS (json_extract(record_json,'$.status')) STORED,
  created_at INTEGER GENERATED ALWAYS AS (unixepoch(json_extract(record_json,'$.created_at'))) STORED,
  PRIMARY KEY(uid,candidate_id),
  CHECK(candidate_id=json_extract(record_json,'$.candidate_id')),
  CHECK(account_generation>=0 AND account_generation IS NOT NULL),
  CHECK(status IN ('pending','accepted','rejected','expired') AND status IS NOT NULL),
  CHECK(created_at IS NOT NULL)
);
CREATE INDEX cf_candidates_page ON cf_candidates(uid,account_generation,status,created_at DESC,candidate_id);
CREATE TABLE cf_candidate_aliases (
  uid TEXT NOT NULL,
  key_hash TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  PRIMARY KEY(uid,key_hash)
);
CREATE TABLE cf_candidate_claims (
  uid TEXT NOT NULL,
  semantic_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  PRIMARY KEY(uid,semantic_id)
);
ALTER TABLE cf_action_items ADD COLUMN account_generation INTEGER NOT NULL DEFAULT 0 CHECK(account_generation>=0);
ALTER TABLE cf_action_items ADD COLUMN candidate_id TEXT;
ALTER TABLE cf_action_items ADD COLUMN capture_confidence REAL CHECK(capture_confidence BETWEEN 0 AND 1);
ALTER TABLE cf_action_items ADD COLUMN ownership_confidence REAL CHECK(ownership_confidence BETWEEN 0 AND 1);
ALTER TABLE cf_goals ADD COLUMN account_generation INTEGER NOT NULL DEFAULT 0 CHECK(account_generation>=0);
CREATE TABLE cf_candidate_integration_outbox (
  uid TEXT NOT NULL,
  outbox_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  status TEXT GENERATED ALWAYS AS (json_extract(record_json,'$.status')) STORED,
  PRIMARY KEY(uid,outbox_id),
  CHECK(outbox_id=json_extract(record_json,'$.outbox_id')),
  CHECK(account_generation>=0 AND account_generation IS NOT NULL),
  CHECK(status IN ('pending','processing','failed','completed','suppressed','dead_letter') AND status IS NOT NULL)
);
CREATE INDEX cf_candidate_integration_ready ON cf_candidate_integration_outbox(uid,account_generation,status,outbox_id);
CREATE TABLE cf_candidate_write_guard (
  uid TEXT PRIMARY KEY,
  account_generation INTEGER NOT NULL CHECK(account_generation>=0),
  candidates_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(candidates_json) AND json_type(candidates_json)='array'),
  aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json) AND json_type(aliases_json)='array'),
  claims_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(claims_json) AND json_type(claims_json)='array'),
  integrations_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(integrations_json) AND json_type(integrations_json)='array'),
  tasks_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tasks_json) AND json_type(tasks_json)='array'),
  workstreams_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(workstreams_json) AND json_type(workstreams_json)='array'),
  goals_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(goals_json) AND json_type(goals_json)='array')
);
CREATE TRIGGER cf_candidate_guard_account BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
    OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
    OR NEW.account_generation != COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=NEW.uid),0)
    THEN RAISE(ABORT,'candidate_generation_changed') END);
END;

CREATE TRIGGER cf_candidate_guard_candidates BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.candidates_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_candidates WHERE uid=NEW.uid AND candidate_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_candidate_candidates_insert BEFORE INSERT ON cf_candidates
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.candidates_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.candidate_id
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;

CREATE TRIGGER cf_candidate_candidates_update BEFORE UPDATE ON cf_candidates
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.candidates_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.candidate_id
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;

CREATE TRIGGER cf_candidate_guard_aliases BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.aliases_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_candidate_aliases WHERE uid=NEW.uid AND key_hash=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_candidate_aliases_insert BEFORE INSERT ON cf_candidate_aliases
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.aliases_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.key_hash
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;

CREATE TRIGGER cf_candidate_aliases_update BEFORE UPDATE ON cf_candidate_aliases
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.aliases_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.key_hash
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;

CREATE TRIGGER cf_candidate_guard_claims BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.claims_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_candidate_claims WHERE uid=NEW.uid AND semantic_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_candidate_claims_insert BEFORE INSERT ON cf_candidate_claims
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.claims_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.semantic_id
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;

CREATE TRIGGER cf_candidate_claims_update BEFORE UPDATE ON cf_candidate_claims
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.claims_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.semantic_id
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;

CREATE TRIGGER cf_candidate_guard_tasks BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.tasks_json) expected
    WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
      THEN EXISTS(SELECT 1 FROM cf_action_items WHERE uid=NEW.uid AND id=json_extract(expected.value,'$.id'))
      ELSE NOT EXISTS(SELECT 1 FROM cf_action_items m WHERE m.uid=NEW.uid
        -- Keep the exact row comparison below workerd SQL expression depth.
        AND (
          ((m.id IS json_extract(expected.value,'$.before.id') AND m.description IS json_extract(expected.value,'$.before.description')) AND (m.status IS json_extract(expected.value,'$.before.status') AND m.completed IS json_extract(expected.value,'$.before.completed')))
          AND ((m.goal_id IS json_extract(expected.value,'$.before.goal_id') AND m.workstream_id IS json_extract(expected.value,'$.before.workstream_id')) AND (m.owner IS json_extract(expected.value,'$.before.owner') AND m.due_at IS json_extract(expected.value,'$.before.due_at')))
          AND ((m.due_confidence IS json_extract(expected.value,'$.before.due_confidence') AND m.source IS json_extract(expected.value,'$.before.source')) AND (m.provenance_json IS json_extract(expected.value,'$.before.provenance_json') AND m.priority IS json_extract(expected.value,'$.before.priority')))
          AND ((m.sort_order IS json_extract(expected.value,'$.before.sort_order') AND m.indent_level IS json_extract(expected.value,'$.before.indent_level')) AND (m.recurrence_rule IS json_extract(expected.value,'$.before.recurrence_rule') AND m.recurrence_parent_id IS json_extract(expected.value,'$.before.recurrence_parent_id')))
          AND ((m.superseded_by IS json_extract(expected.value,'$.before.superseded_by') AND m.conversation_id IS json_extract(expected.value,'$.before.conversation_id')) AND (m.is_locked IS json_extract(expected.value,'$.before.is_locked') AND m.exported IS json_extract(expected.value,'$.before.exported')))
          AND ((m.export_date IS json_extract(expected.value,'$.before.export_date') AND m.export_platform IS json_extract(expected.value,'$.before.export_platform')) AND (m.apple_reminder_id IS json_extract(expected.value,'$.before.apple_reminder_id') AND m.completed_at IS json_extract(expected.value,'$.before.completed_at')))
          AND ((m.created_at IS json_extract(expected.value,'$.before.created_at') AND m.updated_at IS json_extract(expected.value,'$.before.updated_at')) AND (m.idempotency_key IS json_extract(expected.value,'$.before.idempotency_key') AND m.sync_requested IS json_extract(expected.value,'$.before.sync_requested')))
          AND ((m.deleted IS json_extract(expected.value,'$.before.deleted') AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')) AND (m.candidate_id IS json_extract(expected.value,'$.before.candidate_id') AND m.capture_confidence IS json_extract(expected.value,'$.before.capture_confidence')))
          AND m.ownership_confidence IS json_extract(expected.value,'$.before.ownership_confidence')
        )) END
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_candidate_guard_integrations BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.integrations_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_candidate_integration_outbox WHERE uid=NEW.uid AND outbox_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_candidate_integration_outbox_insert BEFORE INSERT ON cf_candidate_integration_outbox
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.integrations_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.outbox_id
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;

CREATE TRIGGER cf_candidate_integration_outbox_update BEFORE UPDATE ON cf_candidate_integration_outbox
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.integrations_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.outbox_id
      AND guard.account_generation=json_extract(NEW.record_json,'$.account_generation')
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;


CREATE TRIGGER cf_candidate_guard_workstreams BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.workstreams_json) expected
    WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
      THEN EXISTS(SELECT 1 FROM cf_workstreams WHERE uid=NEW.uid AND id=json_extract(expected.value,'$.id'))
      ELSE NOT EXISTS(SELECT 1 FROM cf_workstreams m WHERE m.uid=NEW.uid
        AND m.id IS json_extract(expected.value,'$.before.id')
        AND m.goal_id IS json_extract(expected.value,'$.before.goal_id')
        AND m.title IS json_extract(expected.value,'$.before.title')
        AND m.objective IS json_extract(expected.value,'$.before.objective')
        AND m.status IS json_extract(expected.value,'$.before.status')
        AND m.current_state_summary IS json_extract(expected.value,'$.before.current_state_summary')
        AND m.next_review_at IS json_extract(expected.value,'$.before.next_review_at')
        AND m.last_meaningful_progress_at IS json_extract(expected.value,'$.before.last_meaningful_progress_at')
        AND m.latest_event_sequence IS json_extract(expected.value,'$.before.latest_event_sequence')
        AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')
        AND m.created_at IS json_extract(expected.value,'$.before.created_at')
        AND m.updated_at IS json_extract(expected.value,'$.before.updated_at')) END
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_candidate_guard_goals BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.goals_json) expected
    WHERE CASE WHEN json_extract(expected.value,'$.before') IS NULL
      THEN EXISTS(SELECT 1 FROM cf_goals WHERE uid=NEW.uid AND id=json_extract(expected.value,'$.id'))
      ELSE NOT EXISTS(SELECT 1 FROM cf_goals m WHERE m.uid=NEW.uid
        AND m.id IS json_extract(expected.value,'$.before.id')
        AND m.title IS json_extract(expected.value,'$.before.title')
        AND m.desired_outcome IS json_extract(expected.value,'$.before.desired_outcome')
        AND m.why_it_matters IS json_extract(expected.value,'$.before.why_it_matters')
        AND m.success_criteria_json IS json_extract(expected.value,'$.before.success_criteria_json')
        AND m.horizon_at IS json_extract(expected.value,'$.before.horizon_at')
        AND m.status IS json_extract(expected.value,'$.before.status')
        AND m.focus_rank IS json_extract(expected.value,'$.before.focus_rank')
        AND m.metric_json IS json_extract(expected.value,'$.before.metric_json')
        AND m.source IS json_extract(expected.value,'$.before.source')
        AND m.relationship_disposition IS json_extract(expected.value,'$.before.relationship_disposition')
        AND m.is_active IS json_extract(expected.value,'$.before.is_active')
        AND m.latest_progress_sequence IS json_extract(expected.value,'$.before.latest_progress_sequence')
        AND m.ended_at IS json_extract(expected.value,'$.before.ended_at')
        AND m.created_at IS json_extract(expected.value,'$.before.created_at')
        AND m.updated_at IS json_extract(expected.value,'$.before.updated_at')
        AND m.account_generation IS json_extract(expected.value,'$.before.account_generation')) END
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

-- Apply the shared account-deletion fences to every new owner and write guard.
CREATE TRIGGER IF NOT EXISTS adf_i_candidates
BEFORE INSERT ON cf_candidates
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_candidates
BEFORE UPDATE ON cf_candidates
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_candidate_aliases
BEFORE INSERT ON cf_candidate_aliases
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_candidate_aliases
BEFORE UPDATE ON cf_candidate_aliases
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_candidate_claims
BEFORE INSERT ON cf_candidate_claims
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_candidate_claims
BEFORE UPDATE ON cf_candidate_claims
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_candidate_integration_outbox
BEFORE INSERT ON cf_candidate_integration_outbox
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_candidate_integration_outbox
BEFORE UPDATE ON cf_candidate_integration_outbox
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_candidate_write_guard
BEFORE INSERT ON cf_candidate_write_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_candidate_write_guard
BEFORE UPDATE ON cf_candidate_write_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

-- Canonical creation is admitted with its receipt in the same atomic batch.
CREATE TRIGGER cf_goal_create_generation BEFORE INSERT ON cf_goal_mutations
WHEN NEW.operation='goal-create' AND NEW.account_generation != COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=NEW.uid),0)
BEGIN
  SELECT RAISE(ABORT,'goal_account_generation_changed');
END;
