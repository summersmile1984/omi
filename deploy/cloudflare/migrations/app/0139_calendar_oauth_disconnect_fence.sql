-- Prevent an in-flight Google Calendar OAuth callback from restoring a grant
-- after the user disconnects Calendar.  The generation is advanced by the
-- disconnect mutation and captured by each single-use OAuth state.
ALTER TABLE cf_user_calendar_onboarding
  ADD COLUMN oauth_generation INTEGER NOT NULL DEFAULT 0 CHECK (oauth_generation >= 0);

ALTER TABLE cf_google_calendar_oauth_states
  ADD COLUMN oauth_generation INTEGER NOT NULL DEFAULT 0 CHECK (oauth_generation >= 0);
