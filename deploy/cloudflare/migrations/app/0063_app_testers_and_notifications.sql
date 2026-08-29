CREATE TABLE IF NOT EXISTS cf_app_testers (
  uid TEXT PRIMARY KEY NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  added_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cf_app_tester_access (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  app_id TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 256),
  added_at INTEGER NOT NULL,
  PRIMARY KEY (uid, app_id),
  FOREIGN KEY (uid) REFERENCES cf_app_testers(uid) ON DELETE CASCADE,
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_app_tester_access_app_idx
  ON cf_app_tester_access(app_id, uid);

-- Notifications are shared infrastructure: fair-use and app moderation both
-- publish to the same durable FCM outbox rather than owning parallel senders.
CREATE TABLE IF NOT EXISTS cf_notification_outbox (
  notification_id TEXT PRIMARY KEY NOT NULL CHECK (length(notification_id) BETWEEN 1 AND 64),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('fair_use', 'app_moderation')),
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

CREATE INDEX IF NOT EXISTS cf_notification_outbox_delivery_idx
  ON cf_notification_outbox(status, not_before, created_at);

-- Preserve any pending fair-use delivery across the additive cutover. The old
-- table remains for one-version rollback compatibility but receives no new rows.
INSERT OR IGNORE INTO cf_notification_outbox (
  notification_id, source_kind, source_id, uid, title, body, data_json,
  status, attempts, not_before, lease_until, last_error, created_at, updated_at
)
SELECT
  notification_id, 'fair_use', event_id, uid, title, body, data_json,
  status, attempts, not_before, lease_until, last_error, created_at, updated_at
FROM cf_fair_use_notification_outbox;

CREATE TRIGGER IF NOT EXISTS adf_i_app_testers
BEFORE INSERT ON cf_app_testers
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_testers
BEFORE UPDATE ON cf_app_testers
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_app_tester_access
BEFORE INSERT ON cf_app_tester_access
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_tester_access
BEFORE UPDATE ON cf_app_tester_access
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_notification_outbox
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

CREATE TRIGGER IF NOT EXISTS adf_u_notification_outbox
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
