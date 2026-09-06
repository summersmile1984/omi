-- A permanent one-time receipt owns the non-Stripe 30-day Operator grant.
-- Attribution is separately erasable when either account is deleted; removing
-- the inviter never removes the recipient's one-time-claim receipt.
CREATE TABLE cf_referral_claims (
  uid TEXT PRIMARY KEY NOT NULL,
  claim_id TEXT NOT NULL UNIQUE,
  program TEXT NOT NULL CHECK (program = 'desktop_operator_month_v1'),
  claimed_at INTEGER NOT NULL,
  trial_ends_at INTEGER NOT NULL CHECK (trial_ends_at = claimed_at + 2592000)
);
CREATE TABLE cf_referral_attributions (
  uid TEXT PRIMARY KEY NOT NULL REFERENCES cf_referral_claims(uid) ON DELETE CASCADE,
  sender_uid TEXT NOT NULL CHECK (uid != sender_uid)
);
CREATE INDEX cf_referral_attributions_sender ON cf_referral_attributions(sender_uid);

CREATE TRIGGER IF NOT EXISTS adf_i_referral_claims
BEFORE INSERT ON cf_referral_claims
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_referral_claims
BEFORE UPDATE ON cf_referral_claims
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_i_referral_attributions
BEFORE INSERT ON cf_referral_attributions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (NEW.uid, NEW.sender_uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (NEW.uid, NEW.sender_uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER IF NOT EXISTS adf_u_referral_attributions
BEFORE UPDATE ON cf_referral_attributions
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, OLD.sender_uid, NEW.uid, NEW.sender_uid))
  OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, OLD.sender_uid, NEW.uid, NEW.sender_uid))
BEGIN SELECT RAISE(ABORT, 'account deletion fence'); END;

CREATE TRIGGER referral_claim_immutable BEFORE UPDATE ON cf_referral_claims
BEGIN SELECT RAISE(ABORT, 'referral claim is immutable'); END;

CREATE TRIGGER referral_claim_entitlement AFTER INSERT ON cf_referral_claims
BEGIN
  INSERT INTO cf_user_subscriptions (
    uid, plan, status, current_period_start, current_period_end, cancel_at_period_end, updated_at
  ) VALUES (NEW.uid, 'operator', 'active', NEW.claimed_at, NEW.trial_ends_at, 1, NEW.claimed_at)
  ON CONFLICT(uid) DO UPDATE SET plan = 'operator', status = 'active',
    current_period_start = excluded.current_period_start, current_period_end = excluded.current_period_end,
    cancel_at_period_end = 1, updated_at = excluded.updated_at;
END;

-- Every entitlement reader sees expiry immediately; no cron lag can extend a
-- free trial. The physical subscription remains the billing mutation owner.
-- A later Stripe subscription is independent and never expires with this grant.
CREATE VIEW cf_effective_user_subscriptions AS
SELECT
  s.uid,
  CASE WHEN s.plan = 'operator' AND s.stripe_subscription_id IS NULL AND s.current_period_start = r.claimed_at AND s.current_period_end = r.trial_ends_at AND r.trial_ends_at <= unixepoch() THEN 'basic' ELSE s.plan END AS plan,
  CASE WHEN s.plan = 'operator' AND s.stripe_subscription_id IS NULL AND s.current_period_start = r.claimed_at AND s.current_period_end = r.trial_ends_at AND r.trial_ends_at <= unixepoch() THEN 'inactive' ELSE s.status END AS status,
  s.current_period_start,
  s.current_period_end,
  s.stripe_subscription_id,
  s.current_price_id,
  s.features_json,
  s.cancel_at_period_end,
  s.show_subscription_ui,
  s.updated_at,
  s.stripe_status,
  s.stripe_event_id,
  s.stripe_schedule_id,
  s.scheduled_price_id,
  s.stripe_schedule_status,
  s.schedule_effective_at,
  s.cancellation_reason,
  s.cancellation_reason_details,
  s.cancellation_feedback_at
FROM cf_user_subscriptions s LEFT JOIN cf_referral_claims r ON r.uid = s.uid;
