-- Read-only context dependencies do not grant mutation authority. Hidden owner
-- rejection examples may inform unchanged source writes, even when locked.
-- Existing interactive writers retain their original expected_items_json gate.
ALTER TABLE cf_memory_apply_guard ADD COLUMN observed_items_json TEXT NOT NULL DEFAULT '[]'
  CHECK (json_valid(observed_items_json) AND json_type(observed_items_json) = 'array');

CREATE TRIGGER cf_memory_apply_observation_admission BEFORE INSERT ON cf_memory_apply_guard
WHEN json_array_length(NEW.observed_items_json) > 0
BEGIN
  SELECT (CASE WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.observed_items_json) e
    WHERE NOT EXISTS (
      SELECT 1 FROM cf_memories m
      WHERE m.uid = NEW.uid AND m.id = json_extract(e.value, '$.id')
        AND m.deleted_at IS NULL AND m.invalid_at IS NULL AND m.source_state = 'active'
        AND m.status IN ('active', 'hidden') AND m.superseded_by IS NULL
        AND m.status = json_extract(e.value, '$.status')
        AND m.is_locked = json_extract(e.value, '$.is_locked')
        AND m.account_generation = NEW.account_generation
        AND m.item_revision = json_extract(e.value, '$.item_revision')
        AND m.version = json_extract(e.value, '$.version')
        AND m.canonical_metadata_json IS json_extract(e.value, '$.canonical_metadata_json')
        AND m.capture_device_ids_json IS json_extract(e.value, '$.capture_device_ids_json')
        AND m.primary_capture_device IS json_extract(e.value, '$.primary_capture_device')
    )
  ) THEN RAISE(ABORT, 'memory_apply_observation_changed') END);
END;
