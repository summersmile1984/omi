CREATE TABLE IF NOT EXISTS cf_shared_conversation_index (
  conversation_id TEXT PRIMARY KEY NOT NULL,
  uid TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ('shared', 'public')),
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (uid, conversation_id)
    REFERENCES cf_conversations(uid, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_shared_conversation_index_uid_idx
  ON cf_shared_conversation_index(uid, updated_at DESC);

-- A public URL contains only the conversation id, so an id shared by two
-- accounts is ambiguous. Preserve the legacy URL contract only where the owner
-- is unique; the API rejects later collisions instead of exposing either row.
INSERT INTO cf_shared_conversation_index (conversation_id, uid, visibility, updated_at)
SELECT id, MIN(uid), MIN(visibility), MAX(updated_at)
FROM cf_conversations
WHERE visibility IN ('shared', 'public')
GROUP BY id
HAVING COUNT(*) = 1;

CREATE TRIGGER IF NOT EXISTS conversation_share_owner_collision
BEFORE INSERT ON cf_shared_conversation_index
WHEN EXISTS (
  SELECT 1 FROM cf_shared_conversation_index
  WHERE conversation_id = NEW.conversation_id AND uid <> NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'conversation share id collision');
END;

CREATE TRIGGER IF NOT EXISTS conversation_share_owner_immutable
BEFORE UPDATE ON cf_shared_conversation_index
WHEN OLD.uid <> NEW.uid
BEGIN
  SELECT RAISE(ABORT, 'conversation share owner is immutable');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_shared_conversation_index
BEFORE INSERT ON cf_shared_conversation_index
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid = NEW.uid AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_shared_conversation_index
BEFORE UPDATE ON cf_shared_conversation_index
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (
    SELECT 1 FROM cf_account_deletion_tombstones
    WHERE uid IN (OLD.uid, NEW.uid) AND expires_at > unixepoch()
  )
BEGIN
  SELECT RAISE(ABORT, 'account data plane is fenced');
END;
