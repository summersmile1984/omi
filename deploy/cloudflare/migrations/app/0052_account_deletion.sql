CREATE TABLE IF NOT EXISTS cf_account_deletion_intents (
  uid TEXT PRIMARY KEY NOT NULL,
  job_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'failed')),
  phase TEXT NOT NULL CHECK (phase IN ('quiescing', 'purging', 'identity')),
  reason TEXT,
  reason_details TEXT,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_token TEXT,
  lease_until INTEGER,
  next_attempt_at INTEGER NOT NULL,
  settled_at INTEGER,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  CHECK (reason IS NULL OR length(reason) <= 64),
  CHECK (reason_details IS NULL OR length(reason_details) <= 2000),
  CHECK (lease_token IS NULL OR length(lease_token) <= 64),
  CHECK (last_error IS NULL OR length(last_error) <= 128)
);

CREATE INDEX IF NOT EXISTS cf_account_deletion_dispatch_idx
  ON cf_account_deletion_intents(status, next_attempt_at, lease_until);

-- A completed deletion keeps only the opaque uid for one Better Auth JWT TTL
-- plus safety margin. That short tombstone prevents an already-issued 24-hour
-- JWT from recreating an isolated-staging cutover row after Auth is gone.
CREATE TABLE IF NOT EXISTS cf_account_deletion_tombstones (
  uid TEXT PRIMARY KEY NOT NULL,
  completed_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL CHECK (expires_at > completed_at)
);

CREATE INDEX IF NOT EXISTS cf_account_deletion_tombstone_expiry_idx
  ON cf_account_deletion_tombstones(expires_at);

-- Product-table mutation fences are declared below. DELETE remains permitted
-- so the Jobs deletion owner can purge an account while every late INSERT or
-- UPDATE admitted before the Edge fence fails atomically in D1.
CREATE TRIGGER IF NOT EXISTS adf_i_account_cutover
BEFORE INSERT ON cf_account_cutover
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_account_cutover
BEFORE UPDATE ON cf_account_cutover
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_action_items
BEFORE INSERT ON cf_action_items
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_action_items
BEFORE UPDATE ON cf_action_items
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_announcement_dismissals
BEFORE INSERT ON cf_announcement_dismissals
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_announcement_dismissals
BEFORE UPDATE ON cf_announcement_dismissals
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_app_catalog
BEFORE INSERT ON cf_app_catalog
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.owner_uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.owner_uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_catalog
BEFORE UPDATE ON cf_app_catalog
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_app_reviews
BEFORE INSERT ON cf_app_reviews
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.reviewer_uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.reviewer_uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_reviews
BEFORE UPDATE ON cf_app_reviews
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.reviewer_uid, NEW.reviewer_uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.reviewer_uid, NEW.reviewer_uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_asset_cleanup_tasks
BEFORE INSERT ON cf_asset_cleanup_tasks
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_asset_cleanup_tasks
BEFORE UPDATE ON cf_asset_cleanup_tasks
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_asset_objects
BEFORE INSERT ON cf_asset_objects
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_asset_objects
BEFORE UPDATE ON cf_asset_objects
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_calendar_meetings
BEFORE INSERT ON cf_calendar_meetings
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_calendar_meetings
BEFORE UPDATE ON cf_calendar_meetings
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_chat_messages
BEFORE INSERT ON cf_chat_messages
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_messages
BEFORE UPDATE ON cf_chat_messages
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_chat_shares
BEFORE INSERT ON cf_chat_shares
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.sender_uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.sender_uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_shares
BEFORE UPDATE ON cf_chat_shares
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.sender_uid, NEW.sender_uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.sender_uid, NEW.sender_uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_conversations
BEFORE INSERT ON cf_conversations
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_conversations
BEFORE UPDATE ON cf_conversations
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_daily_summaries
BEFORE INSERT ON cf_daily_summaries
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_daily_summaries
BEFORE UPDATE ON cf_daily_summaries
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_fair_use_events
BEFORE INSERT ON cf_fair_use_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_fair_use_events
BEFORE UPDATE ON cf_fair_use_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_fair_use_notification_outbox
BEFORE INSERT ON cf_fair_use_notification_outbox
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_fair_use_notification_outbox
BEFORE UPDATE ON cf_fair_use_notification_outbox
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_fair_use_states
BEFORE INSERT ON cf_fair_use_states
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_fair_use_states
BEFORE UPDATE ON cf_fair_use_states
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_fair_use_usage_sources
BEFORE INSERT ON cf_fair_use_usage_sources
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_fair_use_usage_sources
BEFORE UPDATE ON cf_fair_use_usage_sources
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_focus_sessions
BEFORE INSERT ON cf_focus_sessions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_focus_sessions
BEFORE UPDATE ON cf_focus_sessions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_folders
BEFORE INSERT ON cf_folders
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_folders
BEFORE UPDATE ON cf_folders
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_goal_mutations
BEFORE INSERT ON cf_goal_mutations
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_goal_mutations
BEFORE UPDATE ON cf_goal_mutations
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_goal_progress_events
BEFORE INSERT ON cf_goal_progress_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_goal_progress_events
BEFORE UPDATE ON cf_goal_progress_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_goal_progress_history
BEFORE INSERT ON cf_goal_progress_history
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_goal_progress_history
BEFORE UPDATE ON cf_goal_progress_history
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_goals
BEFORE INSERT ON cf_goals
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_goals
BEFORE UPDATE ON cf_goals
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_jobs
BEFORE INSERT ON cf_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_jobs
BEFORE UPDATE ON cf_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_memories
BEFORE INSERT ON cf_memories
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_memories
BEFORE UPDATE ON cf_memories
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_people
BEFORE INSERT ON cf_people
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_people
BEFORE UPDATE ON cf_people
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_realtime_sessions
BEFORE INSERT ON cf_realtime_sessions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_realtime_sessions
BEFORE UPDATE ON cf_realtime_sessions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_realtime_usage
BEFORE INSERT ON cf_realtime_usage
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_realtime_usage
BEFORE UPDATE ON cf_realtime_usage
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_screen_activity
BEFORE INSERT ON cf_screen_activity
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_screen_activity
BEFORE UPDATE ON cf_screen_activity
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_sync_capture_claims
BEFORE INSERT ON cf_sync_capture_claims
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_sync_capture_claims
BEFORE UPDATE ON cf_sync_capture_claims
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_sync_content_ledger
BEFORE INSERT ON cf_sync_content_ledger
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_sync_content_ledger
BEFORE UPDATE ON cf_sync_content_ledger
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_sync_job_files
BEFORE INSERT ON cf_sync_job_files
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_sync_job_files
BEFORE UPDATE ON cf_sync_job_files
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_sync_jobs
BEFORE INSERT ON cf_sync_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_sync_jobs
BEFORE UPDATE ON cf_sync_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_sync_playback_objects
BEFORE INSERT ON cf_sync_playback_objects
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_sync_playback_objects
BEFORE UPDATE ON cf_sync_playback_objects
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_share_acceptances
BEFORE INSERT ON cf_task_share_acceptances
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.recipient_uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.recipient_uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_share_acceptances
BEFORE UPDATE ON cf_task_share_acceptances
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.recipient_uid, NEW.recipient_uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.recipient_uid, NEW.recipient_uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_task_shares
BEFORE INSERT ON cf_task_shares
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.sender_uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.sender_uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_shares
BEFORE UPDATE ON cf_task_shares
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.sender_uid, NEW.sender_uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.sender_uid, NEW.sender_uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_usage_sources
BEFORE INSERT ON cf_usage_sources
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_usage_sources
BEFORE UPDATE ON cf_usage_sources
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_ai_profiles
BEFORE INSERT ON cf_user_ai_profiles
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_ai_profiles
BEFORE UPDATE ON cf_user_ai_profiles
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_assistant_settings
BEFORE INSERT ON cf_user_assistant_settings
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_assistant_settings
BEFORE UPDATE ON cf_user_assistant_settings
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_calendar_onboarding
BEFORE INSERT ON cf_user_calendar_onboarding
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_calendar_onboarding
BEFORE UPDATE ON cf_user_calendar_onboarding
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_developer_webhooks
BEFORE INSERT ON cf_user_developer_webhooks
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_developer_webhooks
BEFORE UPDATE ON cf_user_developer_webhooks
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_enabled_apps
BEFORE INSERT ON cf_user_enabled_apps
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_enabled_apps
BEFORE UPDATE ON cf_user_enabled_apps
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_fcm_tokens
BEFORE INSERT ON cf_user_fcm_tokens
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_fcm_tokens
BEFORE UPDATE ON cf_user_fcm_tokens
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_geolocation
BEFORE INSERT ON cf_user_geolocation
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_geolocation
BEFORE UPDATE ON cf_user_geolocation
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_location_context_consent
BEFORE INSERT ON cf_user_location_context_consent
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_location_context_consent
BEFORE UPDATE ON cf_user_location_context_consent
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_notification_preferences
BEFORE INSERT ON cf_user_notification_preferences
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_notification_preferences
BEFORE UPDATE ON cf_user_notification_preferences
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_notification_settings
BEFORE INSERT ON cf_user_notification_settings
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_notification_settings
BEFORE UPDATE ON cf_user_notification_settings
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_onboarding
BEFORE INSERT ON cf_user_onboarding
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_onboarding
BEFORE UPDATE ON cf_user_onboarding
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_privacy_settings
BEFORE INSERT ON cf_user_privacy_settings
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_privacy_settings
BEFORE UPDATE ON cf_user_privacy_settings
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_subscriptions
BEFORE INSERT ON cf_user_subscriptions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_subscriptions
BEFORE UPDATE ON cf_user_subscriptions
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_training_data_opt_in
BEFORE INSERT ON cf_user_training_data_opt_in
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_training_data_opt_in
BEFORE UPDATE ON cf_user_training_data_opt_in
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_user_transcription_preferences
BEFORE INSERT ON cf_user_transcription_preferences
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_user_transcription_preferences
BEFORE UPDATE ON cf_user_transcription_preferences
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_worker_probe
BEFORE INSERT ON cf_worker_probe
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_worker_probe
BEFORE UPDATE ON cf_worker_probe
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_workstream_artifacts
BEFORE INSERT ON cf_workstream_artifacts
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_workstream_artifacts
BEFORE UPDATE ON cf_workstream_artifacts
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_workstream_checkpoints
BEFORE INSERT ON cf_workstream_checkpoints
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_workstream_checkpoints
BEFORE UPDATE ON cf_workstream_checkpoints
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_workstream_events
BEFORE INSERT ON cf_workstream_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_workstream_events
BEFORE UPDATE ON cf_workstream_events
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_workstream_mutations
BEFORE INSERT ON cf_workstream_mutations
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_workstream_mutations
BEFORE UPDATE ON cf_workstream_mutations
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_workstreams
BEFORE INSERT ON cf_workstreams
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_workstreams
BEFORE UPDATE ON cf_workstreams
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

-- The FTS uid is derived only by cf_conversations triggers; fencing the
-- authoritative conversation INSERT/UPDATE closes that mutation path.
-- Token-only share children inherit the sender fence through their parent.
CREATE TRIGGER IF NOT EXISTS adf_i_task_share_items
BEFORE INSERT ON cf_task_share_items
WHEN EXISTS (
  SELECT 1
  FROM cf_task_shares AS parent
  JOIN cf_account_deletion_intents AS deletion
    ON deletion.uid = parent.sender_uid
  WHERE parent.token = NEW.token
)
OR EXISTS (
  SELECT 1
  FROM cf_task_shares AS parent
  JOIN cf_account_deletion_tombstones AS deletion
    ON deletion.uid = parent.sender_uid
  WHERE parent.token = NEW.token
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_task_share_items
BEFORE UPDATE ON cf_task_share_items
WHEN EXISTS (
  SELECT 1
  FROM cf_task_shares AS parent
  JOIN cf_account_deletion_intents AS deletion
    ON deletion.uid = parent.sender_uid
  WHERE parent.token IN (OLD.token, NEW.token)
)
OR EXISTS (
  SELECT 1
  FROM cf_task_shares AS parent
  JOIN cf_account_deletion_tombstones AS deletion
    ON deletion.uid = parent.sender_uid
  WHERE parent.token IN (OLD.token, NEW.token)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_chat_share_messages
BEFORE INSERT ON cf_chat_share_messages
WHEN EXISTS (
  SELECT 1
  FROM cf_chat_shares AS parent
  JOIN cf_account_deletion_intents AS deletion
    ON deletion.uid = parent.sender_uid
  WHERE parent.token = NEW.token
)
OR EXISTS (
  SELECT 1
  FROM cf_chat_shares AS parent
  JOIN cf_account_deletion_tombstones AS deletion
    ON deletion.uid = parent.sender_uid
  WHERE parent.token = NEW.token
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_share_messages
BEFORE UPDATE ON cf_chat_share_messages
WHEN EXISTS (
  SELECT 1
  FROM cf_chat_shares AS parent
  JOIN cf_account_deletion_intents AS deletion
    ON deletion.uid = parent.sender_uid
  WHERE parent.token IN (OLD.token, NEW.token)
)
OR EXISTS (
  SELECT 1
  FROM cf_chat_shares AS parent
  JOIN cf_account_deletion_tombstones AS deletion
    ON deletion.uid = parent.sender_uid
  WHERE parent.token IN (OLD.token, NEW.token)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
