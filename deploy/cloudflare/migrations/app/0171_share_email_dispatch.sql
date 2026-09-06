-- Share-email dispatch receipts own recipient claims, quota and publication CAS.
ALTER TABLE cf_conversations ADD COLUMN share_email_revision INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER share_email_conversation_revision AFTER UPDATE ON cf_conversations
WHEN NEW.share_email_revision = OLD.share_email_revision
BEGIN
  UPDATE cf_conversations SET share_email_revision = OLD.share_email_revision + 1
  WHERE uid = NEW.uid AND id = NEW.id;
END;

CREATE TABLE cf_share_email_quota (
  uid TEXT NOT NULL,
  day TEXT NOT NULL,
  used INTEGER NOT NULL CHECK (used >= 0),
  PRIMARY KEY (uid, day)
);
CREATE TRIGGER share_email_quota_insert BEFORE INSERT ON cf_share_email_quota WHEN NEW.used > 30
BEGIN SELECT RAISE(ABORT, 'share_email_daily_quota_exceeded'); END;
CREATE TRIGGER share_email_quota_update BEFORE UPDATE ON cf_share_email_quota WHEN NEW.used > 30
BEGIN SELECT RAISE(ABORT, 'share_email_daily_quota_exceeded'); END;

CREATE TABLE cf_share_email_dispatches (
  id TEXT PRIMARY KEY NOT NULL,
  uid TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('prepared','dispatching','sent','ambiguous','rejected')),
  quota_day TEXT NOT NULL,
  recipient_count INTEGER NOT NULL DEFAULT 0 CHECK (recipient_count BETWEEN 0 AND 5),
  was_private INTEGER NOT NULL CHECK (was_private IN (0,1)),
  publish_revision INTEGER,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)),
  provider_message_id TEXT,
  UNIQUE (id, uid, conversation_id),
  FOREIGN KEY (uid, conversation_id) REFERENCES cf_conversations(uid, id) ON DELETE CASCADE
);
CREATE INDEX cf_share_email_dispatch_owner ON cf_share_email_dispatches(uid,created_at);
CREATE INDEX cf_share_email_dispatch_expiry ON cf_share_email_dispatches(phase,expires_at);

CREATE VIEW cf_share_email_receipts AS
SELECT id,uid,conversation_id,phase,quota_day,recipient_count,created_at,provider_message_id
FROM cf_share_email_dispatches;

CREATE TABLE cf_share_email_recipients (
  uid TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  email TEXT NOT NULL,
  dispatch_id TEXT NOT NULL,
  PRIMARY KEY (uid,conversation_id,email),
  FOREIGN KEY (dispatch_id,uid,conversation_id)
    REFERENCES cf_share_email_dispatches(id,uid,conversation_id) ON DELETE CASCADE
);

CREATE TRIGGER share_email_dispatch_transition BEFORE UPDATE OF phase ON cf_share_email_dispatches
WHEN OLD.phase <> NEW.phase AND NOT (
  (OLD.phase = 'prepared' AND NEW.phase IN ('dispatching','rejected')) OR
  (OLD.phase = 'dispatching' AND NEW.phase IN ('sent','ambiguous','rejected'))
)
BEGIN SELECT RAISE(ABORT, 'invalid share email transition'); END;

CREATE TRIGGER share_email_dispatch_identity BEFORE UPDATE ON cf_share_email_dispatches
WHEN OLD.id <> NEW.id OR OLD.uid <> NEW.uid OR OLD.conversation_id <> NEW.conversation_id
  OR OLD.quota_day <> NEW.quota_day OR OLD.created_at <> NEW.created_at OR OLD.was_private <> NEW.was_private
BEGIN SELECT RAISE(ABORT, 'share email identity is immutable'); END;

CREATE TRIGGER share_email_rejected AFTER UPDATE OF phase ON cf_share_email_dispatches
WHEN OLD.phase <> 'rejected' AND NEW.phase = 'rejected'
BEGIN
  UPDATE cf_share_email_quota SET used = used - OLD.recipient_count WHERE uid = OLD.uid AND day = OLD.quota_day;
  UPDATE cf_conversations SET visibility = 'private', updated_at = MAX(updated_at + 1, unixepoch())
    WHERE uid = OLD.uid AND id = OLD.conversation_id AND OLD.was_private = 1
      AND share_email_revision = OLD.publish_revision;
  DELETE FROM cf_shared_conversation_index WHERE uid = OLD.uid AND conversation_id = OLD.conversation_id
    AND EXISTS (SELECT 1 FROM cf_conversations c WHERE c.uid = OLD.uid AND c.id = OLD.conversation_id AND c.visibility = 'private');
  DELETE FROM cf_share_email_recipients WHERE dispatch_id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS adf_i_share_email_quota BEFORE INSERT ON cf_share_email_quota
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_share_email_quota BEFORE UPDATE ON cf_share_email_quota
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_share_email_dispatches BEFORE INSERT ON cf_share_email_dispatches
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_share_email_dispatches BEFORE UPDATE ON cf_share_email_dispatches
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_share_email_recipients BEFORE INSERT ON cf_share_email_recipients
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_share_email_recipients BEFORE UPDATE ON cf_share_email_recipients
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid,NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid,NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
