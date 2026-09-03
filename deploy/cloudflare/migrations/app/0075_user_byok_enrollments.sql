CREATE TABLE IF NOT EXISTS cf_user_byok_enrollments (
  uid TEXT PRIMARY KEY NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
  openai_fingerprint TEXT CHECK (
    openai_fingerprint IS NULL OR
    (length(openai_fingerprint) = 64 AND openai_fingerprint NOT GLOB '*[^0-9a-f]*')
  ),
  anthropic_fingerprint TEXT CHECK (
    anthropic_fingerprint IS NULL OR
    (length(anthropic_fingerprint) = 64 AND anthropic_fingerprint NOT GLOB '*[^0-9a-f]*')
  ),
  gemini_fingerprint TEXT CHECK (
    gemini_fingerprint IS NULL OR
    (length(gemini_fingerprint) = 64 AND gemini_fingerprint NOT GLOB '*[^0-9a-f]*')
  ),
  deepgram_fingerprint TEXT CHECK (
    deepgram_fingerprint IS NULL OR
    (length(deepgram_fingerprint) = 64 AND deepgram_fingerprint NOT GLOB '*[^0-9a-f]*')
  ),
  last_seen_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  CHECK (
    active = 0 OR (
      openai_fingerprint IS NOT NULL AND
      anthropic_fingerprint IS NOT NULL AND
      gemini_fingerprint IS NOT NULL AND
      deepgram_fingerprint IS NOT NULL
    )
  )
);

CREATE INDEX IF NOT EXISTS cf_user_byok_enrollments_active_idx
  ON cf_user_byok_enrollments(active, last_seen_at, uid);

CREATE TRIGGER IF NOT EXISTS adf_i_user_byok_enrollments
BEFORE INSERT ON cf_user_byok_enrollments
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_byok_enrollments
BEFORE UPDATE ON cf_user_byok_enrollments
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
