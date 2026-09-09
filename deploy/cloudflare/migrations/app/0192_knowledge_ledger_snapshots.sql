-- Migration publication owns this atomic completion/projection pair. Public
-- snapshot reads cannot create or renew it. Canonical control remains authority.
CREATE TABLE cf_knowledge_ledger_snapshots (
  uid TEXT PRIMARY KEY NOT NULL,
  completion_json TEXT NOT NULL CHECK(json_valid(completion_json) AND json_type(completion_json)='object'),
  projection_json TEXT CHECK(projection_json IS NULL OR (json_valid(projection_json) AND json_type(projection_json)='object')),
  CHECK(projection_json IS NULL OR json_extract(projection_json,'$.uid')=uid)
);

CREATE TRIGGER IF NOT EXISTS adf_i_knowledge_ledger_snapshots BEFORE INSERT ON cf_knowledge_ledger_snapshots
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
 OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_knowledge_ledger_snapshots BEFORE UPDATE ON cf_knowledge_ledger_snapshots
WHEN EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
 OR EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

-- Privacy retirement removes derived text at the same commit, including when
-- an older writer reaches physical deletion before the canonical finalizer.
CREATE TRIGGER cf_knowledge_ledger_snapshot_privacy_update
AFTER UPDATE OF deleted_at,source_state,status ON cf_memories
WHEN NEW.deleted_at IS NOT NULL OR NEW.status='tombstoned' OR NEW.source_state IN ('tombstoned','purged')
BEGIN DELETE FROM cf_knowledge_ledger_snapshots WHERE uid=NEW.uid; END;
CREATE TRIGGER cf_knowledge_ledger_snapshot_privacy_delete AFTER DELETE ON cf_memories
BEGIN DELETE FROM cf_knowledge_ledger_snapshots WHERE uid=OLD.uid; END;
