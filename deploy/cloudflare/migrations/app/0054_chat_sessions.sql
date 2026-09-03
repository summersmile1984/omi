CREATE TABLE IF NOT EXISTS cf_chat_sessions (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT 'New Chat' CHECK (length(title) BETWEEN 1 AND 500),
  preview TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  app_id TEXT,
  message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
  starred INTEGER NOT NULL DEFAULT 0 CHECK (starred IN (0, 1)),
  PRIMARY KEY (uid, id)
);

CREATE INDEX IF NOT EXISTS cf_chat_sessions_uid_app_updated_idx
  ON cf_chat_sessions(uid, app_id, updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS cf_chat_sessions_uid_starred_updated_idx
  ON cf_chat_sessions(uid, starred, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS cf_chat_quota_events (
  uid TEXT NOT NULL,
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 300),
  source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 100),
  message_id TEXT,
  chat_session_id TEXT,
  platform TEXT,
  occurred_at INTEGER NOT NULL,
  PRIMARY KEY (uid, idempotency_key)
);

CREATE INDEX IF NOT EXISTS cf_chat_quota_events_uid_occurred_idx
  ON cf_chat_quota_events(uid, occurred_at);

-- Earlier Cloudflare chat writes already carried both session identifiers in
-- message_json. Materialize those sessions before this table becomes the
-- authority so existing staging history is not orphaned.
INSERT OR IGNORE INTO cf_chat_sessions (
  uid, id, title, preview, created_at, updated_at, app_id, message_count, starred
)
SELECT
  uid,
  session_id,
  'New Chat',
  NULL,
  COALESCE(MIN(CAST(strftime('%s', json_extract(message_json, '$.created_at')) AS INTEGER)), unixepoch()),
  COALESCE(MAX(CAST(strftime('%s', json_extract(message_json, '$.created_at')) AS INTEGER)), unixepoch()),
  MAX(app_id),
  COUNT(*),
  0
FROM (
  SELECT
    uid,
    app_id,
    message_json,
    COALESCE(
      NULLIF(json_extract(message_json, '$.chat_session_id'), ''),
      NULLIF(json_extract(message_json, '$.session_id'), '')
    ) AS session_id
  FROM cf_chat_messages
)
WHERE session_id IS NOT NULL
GROUP BY uid, session_id;

-- The persistence-only desktop route historically records one quota question
-- for each accepted human desktop-chat message. Preserve any messages already
-- projected in staging before the Worker assumes ownership.
INSERT OR IGNORE INTO cf_chat_quota_events (
  uid, idempotency_key, source, message_id, chat_session_id, platform, occurred_at
)
SELECT
  uid,
  'desktop_messages:' || id,
  'desktop_messages',
  id,
  COALESCE(
    NULLIF(json_extract(message_json, '$.chat_session_id'), ''),
    NULLIF(json_extract(message_json, '$.session_id'), '')
  ),
  NULL,
  COALESCE(CAST(strftime('%s', json_extract(message_json, '$.created_at')) AS INTEGER), unixepoch())
FROM cf_chat_messages
WHERE json_extract(message_json, '$.sender') = 'human'
  AND json_extract(message_json, '$.message_source') = 'desktop_chat';

CREATE TRIGGER IF NOT EXISTS adf_i_chat_sessions
BEFORE INSERT ON cf_chat_sessions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_sessions
BEFORE UPDATE ON cf_chat_sessions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_chat_quota_events
BEFORE INSERT ON cf_chat_quota_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_quota_events
BEFORE UPDATE ON cf_chat_quota_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
