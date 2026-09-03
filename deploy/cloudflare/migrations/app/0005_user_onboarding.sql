CREATE TABLE IF NOT EXISTS cf_user_onboarding (
  uid TEXT PRIMARY KEY NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
  acquisition_source TEXT NOT NULL DEFAULT '',
  device_onboarding_completed INTEGER NOT NULL DEFAULT 0 CHECK (device_onboarding_completed IN (0, 1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_onboarding_updated_idx
  ON cf_user_onboarding(updated_at);
