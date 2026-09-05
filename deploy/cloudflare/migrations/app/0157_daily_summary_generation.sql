ALTER TABLE cf_daily_summaries ADD COLUMN memories_learned_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE cf_daily_summaries ADD COLUMN regenerated_at INTEGER;
ALTER TABLE cf_daily_summaries ADD COLUMN generation_token TEXT NOT NULL DEFAULT '';

-- One day has one generator across requests and isolates. Completed cooldowns
-- survive summary deletion; a delete revokes the active writer's authority.
CREATE TABLE cf_daily_summary_generation (
  uid TEXT NOT NULL,
  date TEXT NOT NULL,
  token TEXT NOT NULL DEFAULT '',
  lease_until INTEGER NOT NULL DEFAULT 0,
  create_until INTEGER NOT NULL DEFAULT 0,
  regen_until INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, date)
);

CREATE TRIGGER IF NOT EXISTS adf_i_daily_summary_generation
BEFORE INSERT ON cf_daily_summary_generation
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_daily_summary_generation
BEFORE UPDATE ON cf_daily_summary_generation
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS daily_summary_delete_revokes_generation
AFTER DELETE ON cf_daily_summaries
WHEN NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = OLD.uid)
AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = OLD.uid)
BEGIN
  UPDATE cf_daily_summary_generation SET token = '', lease_until = 0
  WHERE uid = OLD.uid AND date = OLD.date;
END;
