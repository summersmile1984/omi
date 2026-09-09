-- Canonical user mutations retain the upstream per-memory graph assertion in
-- the same guarded transaction as the item revision, operation and commit.
CREATE TABLE cf_memory_graph_assertions (
  uid TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  item_revision INTEGER NOT NULL CHECK (item_revision > 0),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  assertion_json TEXT NOT NULL CHECK (json_valid(assertion_json) AND json_type(assertion_json) = 'object'),
  PRIMARY KEY (uid, memory_id),
  FOREIGN KEY (uid, memory_id) REFERENCES cf_memories(uid, id) ON DELETE CASCADE,
  CHECK (json_extract(assertion_json, '$.uid') IS uid),
  CHECK (json_extract(assertion_json, '$.memory_id') IS memory_id),
  CHECK (json_extract(assertion_json, '$.item_revision') IS item_revision)
);

CREATE TRIGGER cf_memory_graph_insert_admission BEFORE INSERT ON cf_memory_graph_assertions
BEGIN
  SELECT (CASE WHEN NOT EXISTS (SELECT 1 FROM cf_memory_apply_guard g
    WHERE g.uid = NEW.uid AND g.account_generation = NEW.account_generation
      AND (EXISTS (SELECT 1 FROM json_each(g.expected_items_json) e
        WHERE json_extract(e.value, '$.id') = NEW.memory_id)
      OR EXISTS (SELECT 1 FROM json_each(g.new_ids_json) n WHERE n.value = NEW.memory_id)))
    THEN RAISE(ABORT, 'memory_graph_apply_required') END);
  SELECT (CASE WHEN NOT EXISTS (SELECT 1 FROM cf_memories m
    WHERE m.uid = NEW.uid AND m.id = NEW.memory_id AND m.item_revision = NEW.item_revision
      AND m.account_generation = NEW.account_generation AND m.status = 'active'
      AND m.source_state = 'active' AND m.memory_tier = 'long_term'
      AND m.deleted_at IS NULL AND m.invalid_at IS NULL
      AND json_extract(m.canonical_metadata_json, '$.graph_ready') = 1
      AND json_extract(m.canonical_metadata_json, '$.graph_assertion_id') = json_extract(NEW.assertion_json, '$.assertion_id')
      AND json_extract(m.canonical_metadata_json, '$.graph_plan_hash') = json_extract(NEW.assertion_json, '$.graph_plan_hash')
      AND json_extract(m.canonical_metadata_json, '$.content_hash') = json_extract(NEW.assertion_json, '$.content_hash')
      AND json_extract(m.canonical_metadata_json, '$.ledger_commit_id') = json_extract(NEW.assertion_json, '$.commit_id')
      AND json_extract(m.canonical_metadata_json, '$.ledger_sequence') = json_extract(NEW.assertion_json, '$.commit_sequence')
  ) THEN RAISE(ABORT, 'memory_graph_revision_changed') END);
END;

-- Assertions are immutable snapshots. An item update invalidates the previous
-- version first; the canonical transaction inserts its admitted replacement.
CREATE TRIGGER cf_memory_graph_immutable BEFORE UPDATE ON cf_memory_graph_assertions
BEGIN SELECT RAISE(ABORT, 'memory_graph_immutable'); END;
CREATE TRIGGER cf_memory_graph_revoke_revision AFTER UPDATE ON cf_memories
BEGIN
  DELETE FROM cf_memory_graph_assertions WHERE uid = NEW.uid AND memory_id = NEW.id
    AND (item_revision != NEW.item_revision OR account_generation != NEW.account_generation
      OR NEW.status != 'active' OR NEW.source_state != 'active' OR NEW.memory_tier != 'long_term'
      OR NEW.deleted_at IS NOT NULL OR NEW.invalid_at IS NOT NULL
      OR COALESCE(json_extract(NEW.canonical_metadata_json, '$.graph_ready'), 0) != 1);
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memory_graph_assertions BEFORE INSERT ON cf_memory_graph_assertions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_memory_graph_assertions BEFORE UPDATE ON cf_memory_graph_assertions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
