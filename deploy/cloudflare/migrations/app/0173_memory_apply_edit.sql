-- Interactive edits join the existing account/head transaction. The legacy
-- projection trigger still advances the physical revision exactly once.
ALTER TABLE cf_memory_apply_guard ADD COLUMN expected_items_json TEXT NOT NULL DEFAULT '[]'
  CHECK (json_valid(expected_items_json) AND json_type(expected_items_json) = 'array');

CREATE TRIGGER cf_memory_apply_edit_admission BEFORE INSERT ON cf_memory_apply_guard
WHEN json_array_length(NEW.expected_items_json) > 0
BEGIN
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM cf_memories m JOIN json_each(NEW.expected_items_json) e
      ON m.id = json_extract(e.value, '$.id')
    WHERE m.uid = NEW.uid AND m.is_locked != 0
  ) THEN RAISE(ABORT, 'memory_locked_for_mutation') END);
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.expected_items_json) e
    WHERE NOT EXISTS (
      SELECT 1 FROM cf_memories m
      WHERE m.uid = NEW.uid AND m.id = json_extract(e.value, '$.id')
        AND m.deleted_at IS NULL AND m.invalid_at IS NULL AND m.source_state = 'active'
        AND m.status = 'active' AND m.superseded_by IS NULL
        AND m.account_generation = NEW.account_generation
        AND m.item_revision = json_extract(e.value, '$.item_revision')
        AND m.version = json_extract(e.value, '$.version')
        AND m.canonical_metadata_json IS json_extract(e.value, '$.canonical_metadata_json')
        AND m.capture_device_ids_json IS json_extract(e.value, '$.capture_device_ids_json')
        AND m.primary_capture_device IS json_extract(e.value, '$.primary_capture_device')
    )
  ) THEN RAISE(ABORT, 'memory_apply_target_changed') END);
END;
