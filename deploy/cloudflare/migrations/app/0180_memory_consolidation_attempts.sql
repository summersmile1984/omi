-- Original per-source consolidation retry state, with D1 CAS and apply fencing.
CREATE TABLE cf_memory_consolidation_attempts (
  uid TEXT NOT NULL,
  retry_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  source_item_revision INTEGER NOT NULL CHECK (source_item_revision >= 1),
  source_content_hash TEXT,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
  state_json TEXT NOT NULL CHECK (json_valid(state_json)),
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL CHECK (next_attempt_at >= 0),
  PRIMARY KEY (uid, retry_id),
  CHECK (json_extract(state_json, '$.uid') = uid),
  CHECK (json_extract(state_json, '$.memory_id') = memory_id),
  CHECK (json_extract(state_json, '$.source_item_revision') = source_item_revision),
  CHECK (json_extract(state_json, '$.source_content_hash') IS source_content_hash),
  CHECK (json_extract(state_json, '$.attempt_count') BETWEEN 0 AND 3),
  CHECK (json_extract(state_json, '$.status') IN ('retryable', 'in_progress', 'terminal_review', 'quarantined')),
  CHECK (lease_until IS CAST(strftime('%s', json_extract(state_json, '$.lease_expires_at')) AS INTEGER))
);
CREATE INDEX cf_memory_consolidation_attempts_source
  ON cf_memory_consolidation_attempts(uid, memory_id, source_item_revision);
CREATE INDEX cf_memory_consolidation_attempts_due
  ON cf_memory_consolidation_attempts(next_attempt_at, lease_until, uid);

CREATE TRIGGER IF NOT EXISTS adf_i_memory_consolidation_attempts BEFORE INSERT ON cf_memory_consolidation_attempts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_memory_consolidation_attempts BEFORE UPDATE ON cf_memory_consolidation_attempts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER cf_memory_consolidation_source_delete AFTER DELETE ON cf_memories
BEGIN DELETE FROM cf_memory_consolidation_attempts WHERE uid = OLD.uid AND memory_id = OLD.id; END;

ALTER TABLE cf_memory_apply_guard ADD COLUMN consolidation_claims_json TEXT NOT NULL DEFAULT '[]'
  CHECK (json_valid(consolidation_claims_json) AND json_type(consolidation_claims_json) = 'array');
CREATE TRIGGER cf_memory_consolidation_lease_admission BEFORE INSERT ON cf_memory_apply_guard
BEGIN
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.consolidation_claims_json) claim
    WHERE NOT EXISTS (
      SELECT 1 FROM cf_memory_consolidation_attempts a
      JOIN cf_memory_apply_control c ON c.uid = a.uid
      WHERE a.uid = NEW.uid AND a.retry_id = json_extract(claim.value, '$.retry_id')
        AND a.account_generation = json_extract(claim.value, '$.account_generation')
        AND a.source_generation = json_extract(claim.value, '$.source_generation')
        AND c.account_generation = a.account_generation AND c.source_generation = a.source_generation
        AND a.state_json = json_extract(claim.value, '$.state_json')
        AND json_extract(a.state_json, '$.status') = 'in_progress'
        AND a.lease_until > unixepoch()
    )
  ) THEN RAISE(ABORT, 'memory_consolidation_lease_changed') END);
END;
