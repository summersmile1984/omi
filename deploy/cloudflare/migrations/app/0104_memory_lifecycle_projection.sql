-- Canonical lifecycle fields for rows in cf_memories.
--
-- 0037_memories predates the product-memory lifecycle contract and only kept
-- valid_at/updated_at.  These columns make the fields used by the legacy
-- short-term policy explicit in D1.  Existing rows are conservatively
-- projected from valid_at (the canonical capture timestamp for the current
-- D1 intake) and receive the live 48-hour policy expiry.  The defaults and
-- insert trigger keep older D1 writers correct until every writer supplies
-- the fields explicitly.
ALTER TABLE cf_memories ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0);
ALTER TABLE cf_memories ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
  CHECK (status IN ('active', 'superseded', 'hidden', 'tombstoned'));
ALTER TABLE cf_memories ADD COLUMN processing_state TEXT NOT NULL DEFAULT 'processed'
  CHECK (processing_state IN ('pending', 'processed', 'blocked'));
ALTER TABLE cf_memories ADD COLUMN source_state TEXT NOT NULL DEFAULT 'active'
  CHECK (source_state IN ('active', 'tombstoned', 'purged'));
ALTER TABLE cf_memories ADD COLUMN sensitivity_labels_json TEXT NOT NULL DEFAULT '[]'
  CHECK (length(sensitivity_labels_json) <= 4096 AND json_valid(sensitivity_labels_json));
ALTER TABLE cf_memories ADD COLUMN user_asserted INTEGER NOT NULL DEFAULT 0
  CHECK (user_asserted IN (0, 1));
ALTER TABLE cf_memories ADD COLUMN captured_at INTEGER NOT NULL DEFAULT 0
  CHECK (captured_at >= 0);
ALTER TABLE cf_memories ADD COLUMN expires_at INTEGER
  CHECK (expires_at IS NULL OR expires_at >= 0);
ALTER TABLE cf_memories ADD COLUMN item_revision INTEGER NOT NULL DEFAULT 1
  CHECK (item_revision > 0);
ALTER TABLE cf_memories ADD COLUMN account_generation INTEGER NOT NULL DEFAULT 0
  CHECK (account_generation >= 0);
ALTER TABLE cf_memories ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]'
  CHECK (length(evidence_json) <= 65536 AND json_valid(evidence_json));

UPDATE cf_memories
SET captured_at = CASE
      WHEN valid_at >= 0 THEN valid_at
      WHEN created_at >= 0 THEN created_at
      ELSE 0
    END
WHERE captured_at = 0;

UPDATE cf_memories
SET expires_at = captured_at + 172800
WHERE expires_at IS NULL AND memory_tier = 'short_term';

UPDATE cf_memories
SET account_generation = COALESCE(
      (SELECT account_generation FROM cf_account_cutover
       WHERE cf_account_cutover.uid = cf_memories.uid),
      0
    )
WHERE account_generation = 0;

CREATE INDEX IF NOT EXISTS cf_memories_short_term_lifecycle_idx
  ON cf_memories(
    uid, account_generation, memory_tier, status, processing_state,
    deleted_at, invalid_at, captured_at, expires_at, id
  );

-- Older writers omit the new fields.  Derive them from the same server-side
-- creation timestamp and current account generation; never accept a caller's
-- lifecycle timestamp as authority by rewriting only the legacy default.
CREATE TRIGGER IF NOT EXISTS cf_memories_lifecycle_defaults
AFTER INSERT ON cf_memories
WHEN NEW.captured_at = 0 OR (NEW.expires_at IS NULL AND NEW.memory_tier = 'short_term')
BEGIN
  UPDATE cf_memories
  SET captured_at = CASE
        WHEN NEW.captured_at = 0 THEN NEW.valid_at
        ELSE NEW.captured_at
      END,
      expires_at = CASE
        WHEN NEW.expires_at IS NULL AND NEW.memory_tier = 'short_term'
          THEN (CASE WHEN NEW.captured_at = 0 THEN NEW.valid_at ELSE NEW.captured_at END) + 172800
        ELSE NEW.expires_at
      END,
      account_generation = CASE
        WHEN NEW.account_generation = 0 THEN COALESCE(
          (SELECT account_generation FROM cf_account_cutover
           WHERE cf_account_cutover.uid = NEW.uid),
          0
        )
        ELSE NEW.account_generation
      END
  WHERE uid = NEW.uid AND id = NEW.id;
END;
