-- Durable account discovery/cursor; per-source attempts remain in migration 0180.
CREATE TABLE cf_memory_consolidation_dispatch (
 uid TEXT NOT NULL,
 account_generation INTEGER NOT NULL CHECK(account_generation>=0),
 wake_sequence INTEGER NOT NULL DEFAULT 1 CHECK(wake_sequence>=1),
 handled_sequence INTEGER NOT NULL DEFAULT 0 CHECK(handled_sequence>=0 AND handled_sequence<=wake_sequence),
 cycle_sequence INTEGER CHECK(cycle_sequence>=0 AND cycle_sequence<=wake_sequence),
 cursor_captured_at INTEGER,
 cursor_memory_id TEXT,
 cycle_blocked INTEGER NOT NULL DEFAULT 0 CHECK(cycle_blocked IN (0,1)),
 cycle_applied INTEGER NOT NULL DEFAULT 0 CHECK(cycle_applied IN (0,1)),
 cycle_retry_at INTEGER,
 next_attempt_at INTEGER NOT NULL CHECK(next_attempt_at>=0),
 lease_owner TEXT,
 lease_until INTEGER,
 PRIMARY KEY(uid,account_generation),
 CHECK((lease_owner IS NULL)=(lease_until IS NULL)),
 CHECK((cursor_captured_at IS NULL)=(cursor_memory_id IS NULL))
);
CREATE INDEX cf_memory_consolidation_dispatch_due ON cf_memory_consolidation_dispatch(next_attempt_at,uid);
CREATE INDEX cf_memory_consolidation_scan ON cf_memories(uid,account_generation,captured_at,id);
CREATE TRIGGER IF NOT EXISTS adf_i_memory_consolidation_dispatch BEFORE INSERT ON cf_memory_consolidation_dispatch
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
 OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_memory_consolidation_dispatch BEFORE UPDATE ON cf_memory_consolidation_dispatch
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
 OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT,'account deletion fence'); END;

CREATE TRIGGER cf_memory_consolidation_wake_insert AFTER INSERT ON cf_memories
WHEN NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
 AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN
 INSERT INTO cf_memory_consolidation_dispatch(uid,account_generation,next_attempt_at)
 VALUES(NEW.uid,NEW.account_generation,unixepoch())
 ON CONFLICT(uid,account_generation) DO UPDATE SET wake_sequence=wake_sequence+1,
 next_attempt_at=MIN(next_attempt_at,unixepoch());
END;

CREATE TRIGGER cf_memory_consolidation_wake_update AFTER UPDATE ON cf_memories
WHEN NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=NEW.uid)
 AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=NEW.uid)
BEGIN
 INSERT INTO cf_memory_consolidation_dispatch(uid,account_generation,next_attempt_at)
 VALUES(NEW.uid,NEW.account_generation,unixepoch())
 ON CONFLICT(uid,account_generation) DO UPDATE SET wake_sequence=wake_sequence+1,
 next_attempt_at=MIN(next_attempt_at,unixepoch());
END;

CREATE TRIGGER cf_memory_consolidation_wake_delete AFTER DELETE ON cf_memories
WHEN NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=OLD.uid)
 AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=OLD.uid)
BEGIN
 INSERT INTO cf_memory_consolidation_dispatch(uid,account_generation,next_attempt_at)
 VALUES(OLD.uid,OLD.account_generation,unixepoch())
 ON CONFLICT(uid,account_generation) DO UPDATE SET wake_sequence=wake_sequence+1,
 next_attempt_at=MIN(next_attempt_at,unixepoch());
END;

-- Seed existing and legacy principals; the scan uses original eligibility.
INSERT INTO cf_memory_consolidation_dispatch(uid,account_generation,next_attempt_at)
 SELECT DISTINCT uid,account_generation,unixepoch() FROM cf_memories m
 WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=m.uid)
 AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=m.uid);

ALTER TABLE cf_memory_apply_guard ADD COLUMN consolidation_dispatch_token TEXT;
CREATE TRIGGER cf_memory_consolidation_dispatch_admission BEFORE INSERT ON cf_memory_apply_guard
WHEN NEW.consolidation_dispatch_token IS NOT NULL
BEGIN
 SELECT (CASE WHEN NOT EXISTS (
  SELECT 1 FROM cf_memory_consolidation_dispatch d WHERE d.uid=NEW.uid
  AND d.account_generation=NEW.account_generation AND d.lease_owner=NEW.consolidation_dispatch_token
  AND d.lease_until>unixepoch()
 ) THEN RAISE(ABORT,'memory_consolidation_dispatch_changed') END);
END;
