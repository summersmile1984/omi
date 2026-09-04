-- Existing sessions keep their identity and history; only explicit clear rotates
-- this authority. A late model result cannot repopulate a cleared session.
ALTER TABLE cf_chat_sessions ADD COLUMN clear_epoch TEXT NOT NULL DEFAULT '';
