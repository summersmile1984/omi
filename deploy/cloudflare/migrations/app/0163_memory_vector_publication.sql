-- External writes have immutable attempt IDs. This journal owns every vector
-- before Vectorize is called, including writes whose response is lost.
CREATE TABLE cf_memory_vector_artifacts (
  vector_id TEXT PRIMARY KEY,
  uid TEXT NOT NULL,
  source_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  sub_id TEXT NOT NULL,
  source_version INTEGER NOT NULL,
  model TEXT NOT NULL,
  writer_until INTEGER NOT NULL,
  writer_done INTEGER NOT NULL DEFAULT 0 CHECK (writer_done IN (0, 1)),
  retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
  observed_present INTEGER NOT NULL DEFAULT 0 CHECK (observed_present IN (0, 1)),
  delete_mutation TEXT,
  UNIQUE (attempt_id, sub_id)
);
CREATE INDEX cf_memory_vector_artifacts_owner_idx
  ON cf_memory_vector_artifacts(uid, source_id, attempt_id);
CREATE INDEX cf_memory_vector_artifacts_cleanup_idx
  ON cf_memory_vector_artifacts(retired, writer_done, writer_until);

INSERT INTO cf_memory_vector_artifacts
  (vector_id, uid, source_id, attempt_id, sub_id, source_version, model,
   writer_until, writer_done)
SELECT vector_id, uid, source_id, 'legacy:' || vector_id, sub_id,
  source_version, model, unixepoch() + 900, 0
FROM cf_vector_projection_state WHERE projection_kind = 'memory';

CREATE TRIGGER IF NOT EXISTS adf_i_memory_vector_artifacts
BEFORE INSERT ON cf_memory_vector_artifacts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones
             WHERE uid = NEW.uid AND expires_at > unixepoch())
BEGIN
  SELECT RAISE(ABORT, 'account_deletion_in_progress');
END;

-- Deletion may advance cleanup receipts and release an existing writer. It
-- must not change ownership, extend a writer, or revive retired artifacts.
CREATE TRIGGER IF NOT EXISTS adf_u_memory_vector_artifacts
BEFORE UPDATE ON cf_memory_vector_artifacts
WHEN (EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones
             WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()))
  AND (NEW.uid != OLD.uid OR NEW.vector_id != OLD.vector_id
    OR NEW.source_id != OLD.source_id OR NEW.attempt_id != OLD.attempt_id
    OR NEW.sub_id != OLD.sub_id OR NEW.source_version != OLD.source_version
    OR NEW.model != OLD.model OR NEW.writer_until > OLD.writer_until
    OR NEW.writer_done < OLD.writer_done OR NEW.retired < OLD.retired
    OR NEW.observed_present < OLD.observed_present)
BEGIN
  SELECT RAISE(ABORT, 'account_deletion_in_progress');
END;

-- Revision changes revoke serving mappings in the canonical transaction.
-- Artifact rows remain until the external deletion is observed.
CREATE TRIGGER cf_memory_vector_mapping_revision
AFTER UPDATE OF item_revision ON cf_memories
BEGIN
  DELETE FROM cf_vector_projection_state
  WHERE uid = NEW.uid AND source_id = NEW.id AND projection_kind = 'memory'
    AND source_version != NEW.item_revision;
END;
CREATE TRIGGER cf_memory_vector_mapping_delete
AFTER DELETE ON cf_memories
BEGIN
  DELETE FROM cf_vector_projection_state
  WHERE uid = OLD.uid AND source_id = OLD.id AND projection_kind = 'memory';
END;
