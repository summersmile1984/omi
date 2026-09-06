-- Screenshot bytes have one independent R2 writer. D1 owns admission,
-- one-use approvals, immutable write receipts and the visible survivor set.
CREATE TABLE cf_screen_frame_settings (
  uid TEXT PRIMARY KEY NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);
CREATE TABLE cf_screen_frame_sets (
  uid TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
  sharing_enabled INTEGER NOT NULL DEFAULT 1 CHECK (sharing_enabled IN (0, 1)),
  adjudicated_at TEXT,
  frames_json TEXT NOT NULL DEFAULT '[]' CHECK (
    json_valid(frames_json) AND json_type(frames_json) = 'array' AND json_array_length(frames_json) <= 7
  ),
  PRIMARY KEY (uid, conversation_id),
  FOREIGN KEY (uid, conversation_id) REFERENCES cf_conversations(uid, id) ON DELETE CASCADE
);
CREATE TABLE cf_screen_frame_attempts (
  uid TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
  epoch INTEGER NOT NULL,
  response_json TEXT CHECK (response_json IS NULL OR json_valid(response_json)),
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (uid, attempt_id),
  FOREIGN KEY (uid, conversation_id) REFERENCES cf_screen_frame_sets(uid, conversation_id) ON DELETE CASCADE
);
CREATE INDEX cf_screen_frame_attempt_expiry ON cf_screen_frame_attempts(expires_at);

-- No parent FK: removal of the subject must retain enough information to
-- abort pending multipart uploads and erase committed objects. Deleted
-- receipts remain until approval expiry, preventing replay after cleanup.
CREATE TABLE cf_screen_frame_writes (
  jti TEXT PRIMARY KEY NOT NULL,
  uid TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('writing', 'ready', 'committed', 'cleanup', 'deleted')),
  main_upload_id TEXT,
  thumbnail_upload_id TEXT,
  canonical_sha256 TEXT NOT NULL CHECK (length(canonical_sha256) = 64),
  thumbnail_sha256 TEXT NOT NULL CHECK (length(thumbnail_sha256) = 64),
  metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
  created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX cf_screen_frame_write_owner ON cf_screen_frame_writes(uid, conversation_id, phase);
CREATE INDEX cf_screen_frame_write_cleanup ON cf_screen_frame_writes(phase, expires_at);

CREATE TRIGGER screen_frame_receipt_delete_requires_erased BEFORE DELETE ON cf_screen_frame_writes
WHEN OLD.phase != 'deleted'
BEGIN SELECT RAISE(ABORT, 'screen frame storage erasure required'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_screen_frame_settings BEFORE INSERT ON cf_screen_frame_settings
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_screen_frame_settings BEFORE UPDATE ON cf_screen_frame_settings
WHEN OLD.uid != NEW.uid
  OR EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_i_screen_frame_sets BEFORE INSERT ON cf_screen_frame_sets
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
  OR NEW.frames_json != '[]'
BEGIN SELECT RAISE(ABORT, 'screen frame admission fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_screen_frame_sets BEFORE UPDATE ON cf_screen_frame_sets
WHEN NEW.uid != OLD.uid OR NEW.conversation_id != OLD.conversation_id
  OR EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'screen frame admission fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_i_screen_frame_attempts BEFORE INSERT ON cf_screen_frame_attempts
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
  OR NOT EXISTS (SELECT 1 FROM cf_screen_frame_sets
    WHERE uid = NEW.uid AND conversation_id = NEW.conversation_id AND epoch = NEW.epoch)
BEGIN SELECT RAISE(ABORT, 'screen frame admission fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_screen_frame_attempts BEFORE UPDATE ON cf_screen_frame_attempts
WHEN NEW.uid != OLD.uid OR NEW.conversation_id != OLD.conversation_id
  OR NEW.attempt_id != OLD.attempt_id OR NEW.fingerprint != OLD.fingerprint OR NEW.epoch != OLD.epoch
  OR EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'screen frame admission fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_screen_frame_writes BEFORE INSERT ON cf_screen_frame_writes
WHEN NEW.phase != 'writing'
  OR NEW.expires_at <= unixepoch()
  OR EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
  OR COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = NEW.uid), 1) != 1
  OR NOT EXISTS (SELECT 1 FROM cf_conversations c JOIN cf_screen_frame_sets s
    ON s.uid = c.uid AND s.conversation_id = c.id
    JOIN cf_screen_frame_attempts a ON a.uid = s.uid AND a.conversation_id = s.conversation_id
    WHERE c.uid = NEW.uid AND c.id = NEW.conversation_id AND c.status = 'completed'
      AND s.epoch = NEW.epoch AND a.epoch = NEW.epoch AND a.attempt_id = NEW.attempt_id
      AND a.response_json IS NULL AND a.expires_at > unixepoch())
BEGIN SELECT RAISE(ABORT, 'screen frame admission fence'); END;
CREATE TRIGGER IF NOT EXISTS adf_u_screen_frame_writes BEFORE UPDATE ON cf_screen_frame_writes
WHEN NEW.jti != OLD.jti OR NEW.uid != OLD.uid OR NEW.conversation_id != OLD.conversation_id
  OR NEW.attempt_id != OLD.attempt_id OR NEW.epoch != OLD.epoch OR NEW.expires_at != OLD.expires_at
  OR NEW.canonical_sha256 != OLD.canonical_sha256 OR NEW.thumbnail_sha256 != OLD.thumbnail_sha256
  OR NEW.metadata_json != OLD.metadata_json
  OR (OLD.phase IN ('cleanup', 'deleted') AND NEW.phase NOT IN ('cleanup', 'deleted'))
  OR (NEW.phase NOT IN ('cleanup', 'deleted') AND (
    EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
    OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))))
BEGIN SELECT RAISE(ABORT, 'screen frame receipt is immutable'); END;

CREATE TRIGGER screen_frame_publish_requires_written BEFORE UPDATE OF frames_json ON cf_screen_frame_sets
WHEN EXISTS (SELECT 1 FROM json_each(NEW.frames_json) f WHERE NOT EXISTS (
  SELECT 1 FROM cf_screen_frame_writes w WHERE w.jti = json_extract(f.value, '$.id')
    AND w.uid = NEW.uid AND w.conversation_id = NEW.conversation_id
    AND (w.phase = 'committed' OR (w.phase = 'ready' AND w.epoch = NEW.epoch AND w.expires_at > unixepoch()
      AND COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = NEW.uid), 1) = 1))
))
BEGIN SELECT RAISE(ABORT, 'screen frame bytes are not approved and written'); END;
CREATE TRIGGER screen_frame_publish_receipts AFTER UPDATE OF frames_json ON cf_screen_frame_sets
BEGIN
  UPDATE cf_screen_frame_writes SET phase = 'committed'
    WHERE uid = NEW.uid AND conversation_id = NEW.conversation_id AND phase = 'ready'
      AND jti IN (SELECT json_extract(value, '$.id') FROM json_each(NEW.frames_json));
  UPDATE cf_screen_frame_writes SET phase = 'cleanup'
    WHERE uid = NEW.uid AND conversation_id = NEW.conversation_id AND phase = 'committed'
      AND jti NOT IN (SELECT json_extract(value, '$.id') FROM json_each(NEW.frames_json));
END;
CREATE TRIGGER screen_frame_subject_deleted BEFORE DELETE ON cf_screen_frame_sets
BEGIN
  UPDATE cf_screen_frame_writes SET phase = 'cleanup'
    WHERE uid = OLD.uid AND conversation_id = OLD.conversation_id AND phase != 'deleted';
END;
CREATE TRIGGER screen_frame_account_deleted AFTER INSERT ON cf_account_deletion_intents
BEGIN
  UPDATE cf_screen_frame_writes SET phase = 'cleanup' WHERE uid = NEW.uid AND phase != 'deleted';
END;
CREATE TRIGGER screen_frame_account_tombstoned AFTER INSERT ON cf_account_deletion_tombstones
BEGIN
  UPDATE cf_screen_frame_writes SET phase = 'cleanup' WHERE uid = NEW.uid AND phase != 'deleted';
END;
CREATE TRIGGER screen_frame_epoch_changed AFTER UPDATE OF epoch ON cf_screen_frame_sets
WHEN NEW.epoch != OLD.epoch
BEGIN
  UPDATE cf_screen_frame_writes SET phase = 'cleanup'
    WHERE uid = NEW.uid AND conversation_id = NEW.conversation_id AND epoch != NEW.epoch AND phase IN ('writing', 'ready');
END;
CREATE TRIGGER screen_frame_settings_disabled_insert AFTER INSERT ON cf_screen_frame_settings WHEN NEW.enabled = 0
BEGIN
  UPDATE cf_screen_frame_sets SET epoch = epoch + 1 WHERE uid = NEW.uid;
  UPDATE cf_screen_frame_writes SET phase = 'cleanup' WHERE uid = NEW.uid AND phase IN ('writing', 'ready');
END;
CREATE TRIGGER screen_frame_settings_disabled_update AFTER UPDATE OF enabled ON cf_screen_frame_settings
WHEN NEW.enabled = 0 AND OLD.enabled != 0
BEGIN
  UPDATE cf_screen_frame_sets SET epoch = epoch + 1 WHERE uid = NEW.uid;
  UPDATE cf_screen_frame_writes SET phase = 'cleanup' WHERE uid = NEW.uid AND phase IN ('writing', 'ready');
END;
