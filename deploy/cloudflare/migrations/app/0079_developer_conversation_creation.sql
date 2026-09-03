CREATE TABLE IF NOT EXISTS cf_developer_webhook_outbox (
  delivery_id TEXT PRIMARY KEY NOT NULL CHECK (length(delivery_id) BETWEEN 1 AND 64),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  webhook_type TEXT NOT NULL CHECK (webhook_type = 'memory_created'),
  conversation_id TEXT NOT NULL CHECK (length(conversation_id) BETWEEN 1 AND 256),
  webhook_url TEXT NOT NULL CHECK (length(webhook_url) BETWEEN 9 AND 4096),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(payload_json) <= 1000000),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  not_before INTEGER NOT NULL,
  lease_until INTEGER,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (uid, webhook_type, conversation_id)
);

CREATE INDEX IF NOT EXISTS cf_developer_webhook_outbox_delivery_idx
  ON cf_developer_webhook_outbox(status, not_before, created_at);

CREATE TRIGGER IF NOT EXISTS adf_i_developer_webhook_outbox
BEFORE INSERT ON cf_developer_webhook_outbox
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_developer_webhook_outbox
BEFORE UPDATE ON cf_developer_webhook_outbox
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
