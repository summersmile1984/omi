CREATE TABLE IF NOT EXISTS cf_user_calendar_onboarding (
  uid TEXT PRIMARY KEY,
  connected INTEGER NOT NULL DEFAULT 0 CHECK (connected IN (0, 1)),
  onboarding_skipped INTEGER NOT NULL DEFAULT 0 CHECK (onboarding_skipped IN (0, 1)),
  reauth_required INTEGER NOT NULL DEFAULT 0 CHECK (reauth_required IN (0, 1)),
  has_access_token INTEGER NOT NULL DEFAULT 0 CHECK (has_access_token IN (0, 1)),
  reauth_reason TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
