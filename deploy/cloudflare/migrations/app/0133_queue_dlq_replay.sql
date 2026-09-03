-- Durable index for Cloudflare Queue dead-letter deliveries.
--
-- Queue bindings do not expose a list/read API to a Worker.  The DLQ consumer
-- therefore records a bounded, hash-bound copy of each delivery in D1.  The
-- operator replay route only republishes a recorded envelope; it never accepts
-- a caller-supplied JobMessage payload.
CREATE TABLE IF NOT EXISTS cf_queue_dlq_messages (
  queue_name TEXT NOT NULL CHECK (length(queue_name) BETWEEN 1 AND 128),
  message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
  body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64 AND body_sha256 NOT GLOB '*[^0-9a-f]*'),
  job_id TEXT,
  uid TEXT,
  kind TEXT,
  payload_json TEXT,
  delivery_attempts INTEGER NOT NULL CHECK (delivery_attempts >= 0),
  status TEXT NOT NULL CHECK (status IN ('captured', 'invalid', 'replay_queued', 'replayed', 'replay_failed')),
  invalid_reason TEXT CHECK (invalid_reason IS NULL OR length(invalid_reason) <= 256),
  replay_id TEXT,
  replay_count INTEGER NOT NULL DEFAULT 0 CHECK (replay_count >= 0),
  captured_at INTEGER NOT NULL CHECK (captured_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  replayed_at INTEGER,
  PRIMARY KEY (queue_name, message_id),
  CHECK (
    (status = 'invalid' AND payload_json IS NULL) OR
    (status <> 'invalid' AND payload_json IS NOT NULL)
  ),
  CHECK (payload_json IS NULL OR length(payload_json) <= 16000)
);

CREATE INDEX IF NOT EXISTS cf_queue_dlq_messages_status_idx
  ON cf_queue_dlq_messages(status, updated_at, queue_name);

CREATE TABLE IF NOT EXISTS cf_queue_dlq_replay_requests (
  replay_id TEXT PRIMARY KEY CHECK (length(replay_id) = 36),
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
  requested_count INTEGER NOT NULL CHECK (requested_count > 0 AND requested_count <= 50),
  queued_count INTEGER NOT NULL DEFAULT 0 CHECK (queued_count >= 0),
  skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
  failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
  status TEXT NOT NULL CHECK (status IN ('queued', 'completed', 'partial', 'failed')),
  created_at INTEGER NOT NULL CHECK (created_at > 0),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS cf_queue_dlq_replay_requests_idempotency_idx
  ON cf_queue_dlq_replay_requests(idempotency_key);

CREATE TABLE IF NOT EXISTS cf_queue_dlq_replay_items (
  replay_id TEXT NOT NULL,
  queue_name TEXT NOT NULL CHECK (length(queue_name) BETWEEN 1 AND 128),
  message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
  status TEXT NOT NULL CHECK (status IN ('queued', 'skipped', 'failed')),
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 256),
  updated_at INTEGER NOT NULL CHECK (updated_at > 0),
  PRIMARY KEY (replay_id, queue_name, message_id),
  FOREIGN KEY (replay_id) REFERENCES cf_queue_dlq_replay_requests(replay_id)
);

CREATE INDEX IF NOT EXISTS cf_queue_dlq_replay_items_message_idx
  ON cf_queue_dlq_replay_items(queue_name, message_id, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS adf_i_queue_dlq_messages
BEFORE INSERT ON cf_queue_dlq_messages
WHEN NEW.uid IS NOT NULL AND (
  EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid) OR
  EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_queue_dlq_messages
BEFORE UPDATE ON cf_queue_dlq_messages
WHEN (OLD.uid IS NOT NULL AND EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = OLD.uid))
   OR (OLD.uid IS NOT NULL AND EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = OLD.uid))
   OR (NEW.uid IS NOT NULL AND EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid))
   OR (NEW.uid IS NOT NULL AND EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
