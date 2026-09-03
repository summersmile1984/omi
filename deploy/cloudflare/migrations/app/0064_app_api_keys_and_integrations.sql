CREATE TABLE IF NOT EXISTS cf_app_api_keys (
  app_id TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 256),
  key_id TEXT NOT NULL CHECK (length(key_id) BETWEEN 1 AND 64),
  key_hash TEXT NOT NULL CHECK (length(key_hash) = 64 AND key_hash NOT GLOB '*[^0-9a-f]*'),
  label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (app_id, key_id),
  UNIQUE (app_id, key_hash),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_app_api_keys_created_idx
  ON cf_app_api_keys(app_id, created_at DESC, key_id DESC);

-- Integration mutation rate limiting is durable and account-deletion aware.
-- One row owns an app+user+operation UTC-hour bucket; old buckets are pruned
-- by request traffic and included in the exhaustive account purge inventory.
CREATE TABLE IF NOT EXISTS cf_integration_hourly_usage (
  app_id TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 256),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  operation TEXT NOT NULL CHECK (operation IN ('notification', 'conversation_create', 'memory_create')),
  bucket_start INTEGER NOT NULL CHECK (bucket_start >= 0),
  request_count INTEGER NOT NULL CHECK (request_count BETWEEN 1 AND 60),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (app_id, uid, operation, bucket_start),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_integration_hourly_usage_expiry_idx
  ON cf_integration_hourly_usage(bucket_start, app_id, uid);

CREATE TABLE IF NOT EXISTS cf_integration_webhook_outbox (
  delivery_id TEXT PRIMARY KEY NOT NULL CHECK (length(delivery_id) BETWEEN 1 AND 64),
  app_id TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 256),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  conversation_id TEXT NOT NULL CHECK (length(conversation_id) BETWEEN 1 AND 256),
  webhook_url TEXT NOT NULL CHECK (length(webhook_url) BETWEEN 9 AND 2048),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(payload_json) <= 1000000),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  not_before INTEGER NOT NULL,
  lease_until INTEGER,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (app_id, uid, conversation_id),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_integration_webhook_outbox_delivery_idx
  ON cf_integration_webhook_outbox(status, not_before, created_at);

CREATE TRIGGER IF NOT EXISTS adf_i_integration_hourly_usage
BEFORE INSERT ON cf_integration_hourly_usage
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_integration_hourly_usage
BEFORE UPDATE ON cf_integration_hourly_usage
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_integration_webhook_outbox
BEFORE INSERT ON cf_integration_webhook_outbox
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_integration_webhook_outbox
BEFORE UPDATE ON cf_integration_webhook_outbox
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

-- The existing outbox started with the two producers available at migration
-- 0063. Rebuild it once so integration notifications share the same durable
-- delivery authority without a parallel sender.
DROP TRIGGER IF EXISTS adf_i_notification_outbox;
DROP TRIGGER IF EXISTS adf_u_notification_outbox;
DROP INDEX IF EXISTS cf_notification_outbox_delivery_idx;
ALTER TABLE cf_notification_outbox RENAME TO cf_notification_outbox_v63;

CREATE TABLE cf_notification_outbox (
  notification_id TEXT PRIMARY KEY NOT NULL CHECK (length(notification_id) BETWEEN 1 AND 64),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('fair_use', 'app_moderation', 'integration')),
  source_id TEXT NOT NULL CHECK (length(source_id) BETWEEN 1 AND 400),
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
  data_json TEXT NOT NULL CHECK (json_valid(data_json)),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  not_before INTEGER NOT NULL,
  lease_until INTEGER,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (source_kind, source_id)
);

INSERT INTO cf_notification_outbox (
  notification_id, source_kind, source_id, uid, title, body, data_json,
  status, attempts, not_before, lease_until, last_error, created_at, updated_at
)
SELECT
  notification_id, source_kind, source_id, uid, title, body, data_json,
  status, attempts, not_before, lease_until, last_error, created_at, updated_at
FROM cf_notification_outbox_v63;

DROP TABLE cf_notification_outbox_v63;

CREATE INDEX cf_notification_outbox_delivery_idx
  ON cf_notification_outbox(status, not_before, created_at);

CREATE TRIGGER adf_i_notification_outbox
BEFORE INSERT ON cf_notification_outbox
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER adf_u_notification_outbox
BEFORE UPDATE ON cf_notification_outbox
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

-- Integration-created conversations keep their app provenance in the same
-- canonical projection read by the first-party and integration APIs.
ALTER TABLE cf_conversations ADD COLUMN app_id TEXT;
CREATE INDEX IF NOT EXISTS cf_conversations_uid_app_created_idx
  ON cf_conversations(uid, app_id, created_at DESC, id DESC);
