-- The immutable rating history is separate from cf_user_feedback's latest value.
-- No transcript or memory body is copied into the event envelope.
CREATE TABLE cf_feedback_events (
  id TEXT PRIMARY KEY NOT NULL,
  uid TEXT NOT NULL,
  surface TEXT NOT NULL CHECK (surface IN ('chat_text', 'chat_voice', 'chat_notification', 'conversation_summary', 'memory')),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('chat_message', 'conversation', 'memory')),
  target_id TEXT NOT NULL,
  value INTEGER NOT NULL CHECK (value IN (-1, 0, 1)),
  created_at REAL NOT NULL,
  event_json TEXT NOT NULL CHECK (json_valid(event_json))
    CHECK (json_extract(event_json, '$.id') IS id)
    CHECK (json_extract(event_json, '$.uid') IS uid)
    CHECK (json_extract(event_json, '$.surface') IS surface)
    CHECK (json_extract(event_json, '$.target_kind') IS target_kind)
    CHECK (json_extract(event_json, '$.target_id') IS target_id)
    CHECK (json_extract(event_json, '$.value') IS value),
  CHECK ((target_kind = 'chat_message' AND surface IN ('chat_text', 'chat_voice', 'chat_notification'))
    OR (target_kind = 'conversation' AND surface = 'conversation_summary')
    OR (target_kind = 'memory' AND surface = 'memory'))
);

CREATE INDEX cf_feedback_events_negative_day_idx
  ON cf_feedback_events(created_at, id) WHERE value = -1;
CREATE INDEX cf_feedback_events_uid_idx ON cf_feedback_events(uid, created_at, id);

CREATE TRIGGER IF NOT EXISTS adf_i_feedback_events BEFORE INSERT ON cf_feedback_events
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_feedback_events BEFORE UPDATE ON cf_feedback_events
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER feedback_events_immutable BEFORE UPDATE ON cf_feedback_events
BEGIN
  SELECT RAISE(ABORT, 'feedback events are immutable');
END;
