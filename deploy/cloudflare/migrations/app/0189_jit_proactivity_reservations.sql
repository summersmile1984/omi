-- Content-free JIT reservations share the canonical snapshot transaction.

CREATE TABLE cf_jit_proactivity_events (
  uid TEXT NOT NULL,
  event_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  PRIMARY KEY(uid,event_id),
  CHECK(account_generation>=0 AND account_generation IS NOT NULL),
  CHECK(json_extract(record_json,'$.uid') IS uid)
);
ALTER TABLE cf_candidate_write_guard ADD COLUMN jit_events_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(jit_events_json) AND json_type(jit_events_json)='array');
CREATE TRIGGER cf_candidate_guard_jit_events BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.jit_events_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_jit_proactivity_events WHERE uid=NEW.uid AND event_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_jit_proactivity_events_insert BEFORE INSERT ON cf_jit_proactivity_events
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_events_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.event_id
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_i_jit_proactivity_events BEFORE INSERT ON cf_jit_proactivity_events
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TRIGGER cf_jit_proactivity_events_update BEFORE UPDATE ON cf_jit_proactivity_events
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_events_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.event_id
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_u_jit_proactivity_events BEFORE UPDATE ON cf_jit_proactivity_events
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TABLE cf_jit_proactivity_budget_controls (
  uid TEXT NOT NULL,
  control_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  PRIMARY KEY(uid,control_id),
  CHECK(account_generation>=0 AND account_generation IS NOT NULL),
  CHECK(json_extract(record_json,'$.uid') IS uid)
);
ALTER TABLE cf_candidate_write_guard ADD COLUMN jit_budget_controls_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(jit_budget_controls_json) AND json_type(jit_budget_controls_json)='array');
CREATE TRIGGER cf_candidate_guard_jit_budget_controls BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.jit_budget_controls_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_jit_proactivity_budget_controls WHERE uid=NEW.uid AND control_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_jit_proactivity_budget_controls_insert BEFORE INSERT ON cf_jit_proactivity_budget_controls
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_budget_controls_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.control_id
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_i_jit_proactivity_budget_controls BEFORE INSERT ON cf_jit_proactivity_budget_controls
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TRIGGER cf_jit_proactivity_budget_controls_update BEFORE UPDATE ON cf_jit_proactivity_budget_controls
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_budget_controls_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.control_id
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_u_jit_proactivity_budget_controls BEFORE UPDATE ON cf_jit_proactivity_budget_controls
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TABLE cf_jit_proactivity_daily_budgets (
  uid TEXT NOT NULL,
  budget_day TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  PRIMARY KEY(uid,budget_day),
  CHECK(account_generation>=0 AND account_generation IS NOT NULL),
  CHECK(json_extract(record_json,'$.uid') IS uid)
);
ALTER TABLE cf_candidate_write_guard ADD COLUMN jit_budgets_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(jit_budgets_json) AND json_type(jit_budgets_json)='array');
CREATE TRIGGER cf_candidate_guard_jit_budgets BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.jit_budgets_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_jit_proactivity_daily_budgets WHERE uid=NEW.uid AND budget_day=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_jit_proactivity_daily_budgets_insert BEFORE INSERT ON cf_jit_proactivity_daily_budgets
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_budgets_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.budget_day
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_i_jit_proactivity_daily_budgets BEFORE INSERT ON cf_jit_proactivity_daily_budgets
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TRIGGER cf_jit_proactivity_daily_budgets_update BEFORE UPDATE ON cf_jit_proactivity_daily_budgets
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_budgets_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.budget_day
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_u_jit_proactivity_daily_budgets BEFORE UPDATE ON cf_jit_proactivity_daily_budgets
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TABLE cf_jit_proactivity_candidate_turns (
  uid TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
  account_generation INTEGER GENERATED ALWAYS AS (json_extract(record_json,'$.account_generation')) STORED,
  PRIMARY KEY(uid,candidate_id),
  CHECK(account_generation>=0 AND account_generation IS NOT NULL),
  CHECK(json_extract(record_json,'$.uid') IS uid)
);
ALTER TABLE cf_candidate_write_guard ADD COLUMN jit_turns_json TEXT NOT NULL DEFAULT '[]'
  CHECK(json_valid(jit_turns_json) AND json_type(jit_turns_json)='array');
CREATE TRIGGER cf_candidate_guard_jit_turns BEFORE INSERT ON cf_candidate_write_guard
BEGIN
  SELECT (CASE WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.jit_turns_json) expected
    WHERE json_extract(expected.value,'$.before') IS NOT
      (SELECT record_json FROM cf_jit_proactivity_candidate_turns WHERE uid=NEW.uid AND candidate_id=json_extract(expected.value,'$.id'))
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER cf_jit_proactivity_candidate_turns_insert BEFORE INSERT ON cf_jit_proactivity_candidate_turns
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_turns_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.candidate_id
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_i_jit_proactivity_candidate_turns BEFORE INSERT ON cf_jit_proactivity_candidate_turns
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TRIGGER cf_jit_proactivity_candidate_turns_update BEFORE UPDATE ON cf_jit_proactivity_candidate_turns
BEGIN
  SELECT (CASE WHEN NOT EXISTS(
    SELECT 1 FROM cf_candidate_write_guard guard, json_each(guard.jit_turns_json) expected
    WHERE guard.uid=NEW.uid AND json_extract(expected.value,'$.id')=NEW.candidate_id
      AND guard.account_generation=NEW.account_generation
  ) THEN RAISE(ABORT,'candidate_apply_required') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_u_jit_proactivity_candidate_turns BEFORE UPDATE ON cf_jit_proactivity_candidate_turns
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TABLE cf_jit_reservation_guard (
  uid TEXT PRIMARY KEY,
  flags_json TEXT NOT NULL,
  control_json TEXT NOT NULL,
  time_zone TEXT NOT NULL,
  trigger_id TEXT,
  trigger_generation INTEGER,
  trigger_revision INTEGER,
  trigger_metadata_json TEXT
);
CREATE TRIGGER cf_jit_reservation_authority BEFORE INSERT ON cf_jit_reservation_guard
BEGIN
  SELECT (CASE WHEN NOT EXISTS(SELECT 1 FROM cf_candidate_write_guard WHERE uid=NEW.uid)
    THEN RAISE(ABORT,'candidate_apply_required') END);
  SELECT (CASE WHEN NEW.flags_json IS NOT json_array(
    (SELECT rollout FROM cf_jit_flags WHERE uid=NEW.uid),
    (SELECT kill_switch FROM cf_jit_flags WHERE uid=NEW.uid),
    (SELECT rollout FROM cf_jit_flags WHERE uid=''),
    (SELECT kill_switch FROM cf_jit_flags WHERE uid=''),
    COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=NEW.uid),0))
    THEN RAISE(ABORT,'candidate_snapshot_changed') END);
  SELECT (CASE WHEN NEW.control_json IS NOT
    (SELECT control_json FROM cf_memory_apply_control WHERE uid=NEW.uid)
    OR NEW.time_zone IS NOT (SELECT time_zone FROM cf_user_fcm_tokens WHERE uid=NEW.uid
      ORDER BY updated_at DESC,device_key DESC LIMIT 1)
    THEN RAISE(ABORT,'candidate_snapshot_changed') END);
  SELECT (CASE WHEN NEW.trigger_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM cf_memories WHERE uid=NEW.uid AND id=NEW.trigger_id
      AND account_generation IS NEW.trigger_generation
      AND item_revision IS NEW.trigger_revision
      AND canonical_metadata_json IS NEW.trigger_metadata_json
  ) THEN RAISE(ABORT,'candidate_snapshot_changed') END);
END;

CREATE TRIGGER IF NOT EXISTS adf_i_jit_reservation_guard BEFORE INSERT ON cf_jit_reservation_guard
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_jit_reservation_guard BEFORE UPDATE ON cf_jit_reservation_guard
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
