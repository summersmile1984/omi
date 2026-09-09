-- Review resolution is admitted against the exact canonical source inside the
-- existing mutation/privacy batch. Historical timestamp-based queues have no
-- canonical source authority and remain available only as stale projections.
CREATE TABLE cf_memory_review_apply_guard (
  uid TEXT PRIMARY KEY NOT NULL,
  review_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('accept', 'correct', 'reject', 'drop')),
  source_commit_id TEXT NOT NULL,
  source_item_revision INTEGER NOT NULL CHECK (source_item_revision > 0),
  source_content_hash TEXT NOT NULL
);
CREATE TRIGGER cf_memory_review_apply_admission BEFORE INSERT ON cf_memory_review_apply_guard
BEGIN
  SELECT (CASE WHEN NOT EXISTS (
    SELECT 1 FROM cf_memory_review_queue q JOIN cf_memories m ON m.uid = q.uid AND m.id = q.fact_id
    WHERE q.uid = NEW.uid AND q.review_id = NEW.review_id AND q.fact_id = NEW.memory_id
      AND q.authority = 'canonical_memory' AND q.status IN ('pending', 'pending_review')
      AND q.source_commit_id = NEW.source_commit_id AND q.source_item_revision = NEW.source_item_revision
      AND q.source_content_hash = NEW.source_content_hash AND m.status = 'active'
      AND m.deleted_at IS NULL AND m.invalid_at IS NULL AND m.source_state = 'active'
      AND m.item_revision = NEW.source_item_revision
      AND json_extract(m.canonical_metadata_json, '$.ledger_commit_id') = NEW.source_commit_id
      AND json_extract(m.canonical_metadata_json, '$.content_hash') = NEW.source_content_hash
      AND json_extract(m.canonical_metadata_json, '$.promotion.route') = 'review'
  ) THEN RAISE(ABORT, 'memory_review_source_changed') END);
  SELECT (CASE WHEN NOT EXISTS (
    SELECT 1 FROM cf_memory_apply_guard g, json_each(g.expected_items_json) e
    WHERE g.uid = NEW.uid AND json_extract(e.value, '$.id') = NEW.memory_id
      AND NEW.decision IN ('accept', 'correct')
  ) AND NOT EXISTS (
    SELECT 1 FROM cf_memory_privacy_apply_guard g, json_each(g.expected_items_json) e
    WHERE g.uid = NEW.uid AND json_extract(e.value, '$.id') = NEW.memory_id
      AND NEW.decision IN ('reject', 'drop')
  ) THEN RAISE(ABORT, 'memory_review_apply_required') END);
END;
CREATE TRIGGER cf_memory_review_apply_completion BEFORE DELETE ON cf_memory_review_apply_guard
BEGIN
  SELECT (CASE WHEN NOT EXISTS (
    SELECT 1 FROM cf_memory_review_queue q JOIN cf_memory_apply_control c ON c.uid = q.uid
    WHERE q.uid = OLD.uid AND q.review_id = OLD.review_id AND q.decision = OLD.decision
      AND q.status = CASE WHEN OLD.decision IN ('accept', 'correct') THEN 'accepted'
        WHEN OLD.decision = 'reject' THEN 'rejected' ELSE 'dropped' END
      AND q.resolution_commit_id = c.head_commit_id AND q.candidate_json = '{}'
      AND q.source_commit_id = '' AND q.source_content_hash = '' AND q.correction_json IS NULL
      AND q.permitted_uses_json = '[]'
  ) THEN RAISE(ABORT, 'memory_review_resolution_missing') END);
END;
CREATE TRIGGER IF NOT EXISTS adf_i_memory_review_apply_guard BEFORE INSERT ON cf_memory_review_apply_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_memory_review_apply_guard BEFORE UPDATE ON cf_memory_review_apply_guard
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER cf_memory_review_apply_immutable BEFORE UPDATE ON cf_memory_review_apply_guard
BEGIN SELECT RAISE(ABORT, 'memory review guard immutable'); END;
