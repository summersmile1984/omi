-- Staging source contract for the data-protection preparation executor.
--
-- These columns make the projected source explicit without claiming that a
-- protection migration has happened.  Existing rows remain standard and
-- plaintext until a reviewed source revision is prepared and all readers can
-- consume the resulting envelope.
ALTER TABLE cf_memories ADD COLUMN data_protection_source_revision TEXT;

ALTER TABLE cf_conversations ADD COLUMN data_protection_level TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE cf_conversations ADD COLUMN data_protection_source_revision TEXT;

ALTER TABLE cf_chat_messages ADD COLUMN data_protection_level TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE cf_chat_messages ADD COLUMN data_protection_source_revision TEXT;

-- The source revision is content-bound and is populated by the preparation
-- worker only after it has read the complete source field set.  A revision is
-- deliberately nullable so an imported row cannot be mistaken for verified
-- encryption evidence merely because this schema is present.
CREATE INDEX IF NOT EXISTS cf_memories_data_protection_idx
  ON cf_memories(uid, data_protection_level, data_protection_source_revision, id);

CREATE INDEX IF NOT EXISTS cf_conversations_data_protection_idx
  ON cf_conversations(uid, data_protection_level, data_protection_source_revision, id);

CREATE INDEX IF NOT EXISTS cf_chat_messages_data_protection_idx
  ON cf_chat_messages(uid, data_protection_level, data_protection_source_revision, id);
