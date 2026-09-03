ALTER TABLE cf_user_subscriptions ADD COLUMN stripe_schedule_id TEXT
  CHECK (stripe_schedule_id IS NULL OR length(stripe_schedule_id) BETWEEN 12 AND 160);
ALTER TABLE cf_user_subscriptions ADD COLUMN scheduled_price_id TEXT
  CHECK (scheduled_price_id IS NULL OR length(scheduled_price_id) BETWEEN 12 AND 160);
ALTER TABLE cf_user_subscriptions ADD COLUMN stripe_schedule_status TEXT
  CHECK (
    stripe_schedule_status IS NULL
    OR stripe_schedule_status IN ('active', 'not_started', 'completed', 'canceled', 'released')
  );
ALTER TABLE cf_user_subscriptions ADD COLUMN schedule_effective_at INTEGER
  CHECK (schedule_effective_at IS NULL OR schedule_effective_at >= 0);
ALTER TABLE cf_user_subscriptions ADD COLUMN cancellation_reason TEXT
  CHECK (cancellation_reason IS NULL OR length(cancellation_reason) <= 256);
ALTER TABLE cf_user_subscriptions ADD COLUMN cancellation_reason_details TEXT
  CHECK (cancellation_reason_details IS NULL OR length(cancellation_reason_details) <= 4096);
ALTER TABLE cf_user_subscriptions ADD COLUMN cancellation_feedback_at INTEGER
  CHECK (cancellation_feedback_at IS NULL OR cancellation_feedback_at >= 0);
