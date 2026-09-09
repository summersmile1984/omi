-- JIT flag evaluation and frame metadata share the App D1 authority.
-- An empty uid owns deployment defaults; nonempty uids own explicit overrides.
CREATE TABLE cf_jit_flags (
  uid TEXT PRIMARY KEY,
  rollout INTEGER CHECK (rollout IN (0, 1)),
  kill_switch INTEGER CHECK (kill_switch IN (0, 1)),
  updated_at INTEGER NOT NULL
);

CREATE TABLE cf_frame_requests (
  uid TEXT NOT NULL,
  request_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  dedupe_key TEXT NOT NULL CHECK (length(dedupe_key) = 64),
  dedupe_window INTEGER NOT NULL CHECK (dedupe_window >= 0),
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 0),
  conversation_id TEXT,
  screenshot_id TEXT,
  state TEXT NOT NULL CHECK (state IN ('requested','claimed','uploaded','attached','offline','pruned','failed','expired','cancelled')),
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL CHECK (expires_at >= created_at),
  claimed_at REAL,
  uploaded_at REAL,
  attached_at REAL,
  terminal_reason TEXT CHECK (length(terminal_reason) <= 240),
  byte_count INTEGER NOT NULL DEFAULT 0 CHECK (byte_count BETWEEN 0 AND 10485760),
  content_type TEXT,
  storage_id TEXT,
  cleanup_state TEXT NOT NULL DEFAULT 'not_required'
    CHECK (cleanup_state IN ('not_required','pending','failed','deleted','permanent')),
  cleanup_attempts INTEGER NOT NULL DEFAULT 0 CHECK (cleanup_attempts BETWEEN 0 AND 1000),
  cleanup_next_attempt_at REAL,
  PRIMARY KEY(uid, request_id),
  CHECK (state != 'uploaded' OR storage_id IS NOT NULL),
  CHECK (state != 'attached' OR (conversation_id IS NOT NULL AND expires_at = created_at AND terminal_reason IS NULL)),
  CHECK (state NOT IN ('offline','pruned','failed','expired','cancelled') OR (terminal_reason IS NOT NULL AND length(terminal_reason) > 0))
);
CREATE INDEX cf_frame_request_delivery ON cf_frame_requests(uid,device_id,account_generation,state,created_at);
CREATE INDEX cf_frame_request_dedupe ON cf_frame_requests(uid,device_id,account_generation,dedupe_key,attempt_number DESC);
CREATE INDEX cf_frame_request_conversation ON cf_frame_requests(uid,conversation_id,state);
CREATE INDEX cf_frame_request_expiry ON cf_frame_requests(state,expires_at);
CREATE UNIQUE INDEX cf_frame_request_one_conversation ON cf_frame_requests(uid,conversation_id)
  WHERE conversation_id IS NOT NULL AND state IN ('requested','claimed','uploaded','attached');

CREATE TRIGGER IF NOT EXISTS adf_i_frame_requests BEFORE INSERT ON cf_frame_requests
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
  OR NEW.account_generation != COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid = NEW.uid),0)
BEGIN SELECT RAISE(ABORT,'frame request account authority changed'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_frame_requests BEFORE UPDATE ON cf_frame_requests
WHEN NEW.uid != OLD.uid OR (NEW.state NOT IN ('expired','cancelled','failed','pruned','offline') AND (
  EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
  OR NEW.account_generation != COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid = NEW.uid),0)))
BEGIN SELECT RAISE(ABORT,'frame request account authority changed'); END;

CREATE TRIGGER cf_frame_request_identity BEFORE UPDATE ON cf_frame_requests
WHEN NEW.uid != OLD.uid OR NEW.request_id != OLD.request_id OR NEW.device_id != OLD.device_id
  OR NEW.account_generation != OLD.account_generation OR NEW.dedupe_key != OLD.dedupe_key
  OR NEW.dedupe_window != OLD.dedupe_window OR NEW.attempt_number != OLD.attempt_number
  OR NEW.created_at != OLD.created_at OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.screenshot_id IS NOT OLD.screenshot_id
BEGIN SELECT RAISE(ABORT,'frame request identity is immutable'); END;

CREATE TRIGGER cf_frame_request_pending_quota BEFORE INSERT ON cf_frame_requests
WHEN (SELECT count(*) FROM cf_frame_requests WHERE uid = NEW.uid AND device_id = NEW.device_id
  AND account_generation = NEW.account_generation AND state IN ('requested','claimed','uploaded')
  AND expires_at > NEW.created_at) >= 8
BEGIN SELECT RAISE(ABORT,'frame request quota exceeded: pending_count'); END;

CREATE TRIGGER cf_frame_request_conversation_delete AFTER DELETE ON cf_conversations
BEGIN DELETE FROM cf_frame_requests WHERE uid = OLD.uid AND conversation_id = OLD.id; END;

CREATE TRIGGER IF NOT EXISTS adf_i_jit_flags BEFORE INSERT ON cf_jit_flags
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT,'account deletion in progress'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_jit_flags BEFORE UPDATE ON cf_jit_flags
WHEN NEW.uid != OLD.uid OR EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT,'JIT flag owner unavailable'); END;
