-- All interactive memory writers share this table's lock authority. Evaluate
-- the current row inside the write transaction, including multi-row review
-- resolution and its outbox/receipt writes. Privacy tombstoning and account
-- erasure remain available even while a memory is locked.
CREATE TRIGGER cf_memories_locked_mutation
BEFORE UPDATE OF content, category, visibility, tags_json, headline, predicate,
  arguments_json, subject_entity_id, subject_attribution, object_entity_ids_json,
  qualifiers_json, capture_confidence, veracity, uncertainty_reasons_json,
  durability, reviewed, user_review, edited, is_read, is_dismissed, is_baseline,
  invalid_at, superseded_by
ON cf_memories
WHEN OLD.is_locked != 0
BEGIN SELECT RAISE(ABORT, 'memory_locked_for_mutation'); END;
