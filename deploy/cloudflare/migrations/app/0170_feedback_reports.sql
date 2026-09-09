-- Aggregates contain no user identifiers. Entry rows remain independently erasable.
CREATE TABLE cf_feedback_reports (
  date TEXT PRIMARY KEY NOT NULL,
  metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
  lease_token TEXT,
  lease_expires_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE cf_feedback_report_entries (
  report_date TEXT NOT NULL REFERENCES cf_feedback_reports(date) ON DELETE CASCADE,
  position INTEGER NOT NULL CHECK (position >= 0 AND position < 500),
  event_id TEXT NOT NULL REFERENCES cf_feedback_events(id) ON DELETE CASCADE,
  uid TEXT NOT NULL,
  entry_json TEXT NOT NULL CHECK (json_valid(entry_json))
    CHECK (json_extract(entry_json, '$.event.id') IS event_id)
    CHECK (json_extract(entry_json, '$.event.uid') IS uid)
    CHECK (json_extract(entry_json, '$.context.event_id') IS event_id)
    CHECK (json_extract(entry_json, '$.context.uid') IS uid),
  PRIMARY KEY (report_date, position),
  UNIQUE (report_date, event_id)
);
CREATE INDEX cf_feedback_report_entries_uid_idx ON cf_feedback_report_entries(uid);

CREATE TRIGGER IF NOT EXISTS adf_i_feedback_report_entries BEFORE INSERT ON cf_feedback_report_entries
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_feedback_report_entries BEFORE UPDATE ON cf_feedback_report_entries
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER feedback_report_entry_owner BEFORE INSERT ON cf_feedback_report_entries
WHEN NOT EXISTS (SELECT 1 FROM cf_feedback_events e WHERE e.id = NEW.event_id AND e.uid = NEW.uid
  AND e.target_id IS json_extract(NEW.entry_json, '$.context.target_id')
  AND e.target_kind IS json_extract(NEW.entry_json, '$.context.target_kind'))
BEGIN
  SELECT RAISE(ABORT, 'feedback entry owner mismatch');
END;

CREATE TRIGGER feedback_report_entry_immutable BEFORE UPDATE ON cf_feedback_report_entries
BEGIN
  SELECT RAISE(ABORT, 'feedback report entries are immutable');
END;
