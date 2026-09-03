-- Reviewed historical chat-session/message replay markers.
--
-- The import ledger is not a replacement for the live chat authority.  It
-- records the exact source/export fingerprint that was admitted and gives a
-- future apply batch an immutable, idempotent marker.  Session/message rows
-- retain only the marker and generation needed for post-apply verification;
-- no provider credential, Firebase id, or raw source uid is stored here.
ALTER TABLE cf_chat_sessions
  ADD COLUMN history_import_id TEXT
  CHECK (history_import_id IS NULL OR length(history_import_id) = 64);

ALTER TABLE cf_chat_sessions
  ADD COLUMN history_source_row_sha256 TEXT
  CHECK (
    history_source_row_sha256 IS NULL OR
    (length(history_source_row_sha256) = 64 AND history_source_row_sha256 NOT GLOB '*[^0-9a-f]*')
  );

ALTER TABLE cf_chat_sessions
  ADD COLUMN history_account_generation INTEGER
  CHECK (history_account_generation IS NULL OR history_account_generation >= 0);

ALTER TABLE cf_chat_messages
  ADD COLUMN history_import_id TEXT
  CHECK (history_import_id IS NULL OR length(history_import_id) = 64);

ALTER TABLE cf_chat_messages
  ADD COLUMN history_source_row_sha256 TEXT
  CHECK (
    history_source_row_sha256 IS NULL OR
    (length(history_source_row_sha256) = 64 AND history_source_row_sha256 NOT GLOB '*[^0-9a-f]*')
  );

ALTER TABLE cf_chat_messages
  ADD COLUMN history_account_generation INTEGER
  CHECK (history_account_generation IS NULL OR history_account_generation >= 0);

CREATE INDEX IF NOT EXISTS cf_chat_sessions_history_import_idx
  ON cf_chat_sessions(uid, history_import_id, history_account_generation);

CREATE INDEX IF NOT EXISTS cf_chat_messages_history_import_idx
  ON cf_chat_messages(uid, history_import_id, history_account_generation);

CREATE TABLE IF NOT EXISTS cf_chat_history_import_ledger (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  import_id TEXT NOT NULL CHECK (length(import_id) = 64 AND import_id NOT GLOB '*[^0-9a-f]*'),
  entity_kind TEXT NOT NULL CHECK (entity_kind IN ('session', 'message')),
  entity_id TEXT NOT NULL CHECK (length(entity_id) BETWEEN 1 AND 256),
  source_export_sha256 TEXT NOT NULL CHECK (length(source_export_sha256) = 64 AND source_export_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_row_sha256 TEXT NOT NULL CHECK (length(source_row_sha256) = 64 AND source_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
  action TEXT NOT NULL CHECK (action = 'stage'),
  status TEXT NOT NULL CHECK (status IN ('planned', 'applied', 'failed')),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2048),
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (uid, import_id),
  UNIQUE (uid, entity_kind, entity_id),
  CHECK (status = 'planned' OR last_error IS NULL OR length(last_error) > 0)
);

CREATE INDEX IF NOT EXISTS cf_chat_history_import_ledger_uid_status_idx
  ON cf_chat_history_import_ledger(uid, status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_history_import_ledger
BEFORE INSERT ON cf_chat_history_import_ledger
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_history_import_ledger
BEFORE UPDATE ON cf_chat_history_import_ledger
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_chat_history_sessions
BEFORE INSERT ON cf_chat_sessions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_history_sessions
BEFORE UPDATE OF history_import_id, history_source_row_sha256, history_account_generation ON cf_chat_sessions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_chat_history_messages
BEFORE INSERT ON cf_chat_messages
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_history_messages
BEFORE UPDATE OF history_import_id, history_source_row_sha256, history_account_generation ON cf_chat_messages
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
