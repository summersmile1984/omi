-- A publication receipt is not evidence that ANN queries can see its vectors.
-- Legacy publications deliberately have no completeness/query proof; the
-- existing reconciler republishes them with the new opaque metadata key.
ALTER TABLE cf_memory_vector_artifacts ADD COLUMN publication_size INTEGER NOT NULL DEFAULT 0
  CHECK (publication_size BETWEEN 0 AND 1000);
ALTER TABLE cf_memory_vector_artifacts ADD COLUMN query_ready INTEGER NOT NULL DEFAULT 0
  CHECK (query_ready IN (0, 1));
ALTER TABLE cf_memory_apply_control ADD COLUMN projection_sequence INTEGER NOT NULL DEFAULT 0
  CHECK (projection_sequence >= 0);

CREATE VIEW cf_memory_vector_publications AS
SELECT s.uid, s.source_id, s.source_version, s.model, a.attempt_id,
  COUNT(*) AS vector_count, MIN(a.query_ready) AS query_ready
FROM cf_vector_projection_state s
JOIN cf_memory_vector_artifacts a
  ON a.vector_id = s.vector_id AND a.uid = s.uid AND a.source_id = s.source_id
  AND a.sub_id = s.sub_id AND a.source_version = s.source_version AND a.model = s.model
WHERE s.projection_kind = 'memory' AND a.retired = 0 AND a.writer_done = 1
  AND a.publication_size > 0
GROUP BY s.uid, s.source_id, s.source_version, s.model, a.attempt_id
HAVING COUNT(*) = MAX(a.publication_size) AND MIN(a.publication_size) = MAX(a.publication_size)
  AND COUNT(*) = (SELECT COUNT(*) FROM cf_vector_projection_state all_parts
    WHERE all_parts.uid = s.uid AND all_parts.source_id = s.source_id
      AND all_parts.projection_kind = 'memory');

-- This transport sequence is separate from the canonical business ledger.
-- It detects a concurrent mapping replacement between readiness and retrieval.
-- Privacy cleanup must still be able to retract mappings after its fence closes.
CREATE TRIGGER cf_memory_projection_sequence_insert AFTER INSERT ON cf_vector_projection_state
WHEN NEW.projection_kind = 'memory'
BEGIN
  UPDATE cf_memory_apply_control SET projection_sequence = projection_sequence + 1
  WHERE uid = NEW.uid
    AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
    AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid);
END;
CREATE TRIGGER cf_memory_projection_sequence_delete AFTER DELETE ON cf_vector_projection_state
WHEN OLD.projection_kind = 'memory'
BEGIN
  UPDATE cf_memory_apply_control SET projection_sequence = projection_sequence + 1
  WHERE uid = OLD.uid
    AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = OLD.uid)
    AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = OLD.uid);
END;
CREATE TRIGGER cf_memory_projection_sequence_update AFTER UPDATE ON cf_vector_projection_state
WHEN OLD.projection_kind = 'memory' OR NEW.projection_kind = 'memory'
BEGIN
  UPDATE cf_memory_apply_control SET projection_sequence = projection_sequence + 1
  WHERE ((OLD.projection_kind = 'memory' AND uid = OLD.uid)
      OR (NEW.projection_kind = 'memory' AND uid = NEW.uid))
    AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents d WHERE d.uid = cf_memory_apply_control.uid)
    AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones d WHERE d.uid = cf_memory_apply_control.uid);
END;
