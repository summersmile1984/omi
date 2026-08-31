-- Worker-safe thumbnails for the chat-file authority.
--
-- The source object and thumbnail remain private R2 objects.  The thumbnail
-- key is stored explicitly so deletion and reconciliation never have to infer
-- a second object from a caller-supplied URL.
ALTER TABLE cf_chat_files ADD COLUMN thumbnail_key TEXT;

CREATE INDEX IF NOT EXISTS cf_chat_files_thumbnail_idx
  ON cf_chat_files(uid, thumbnail_key);
