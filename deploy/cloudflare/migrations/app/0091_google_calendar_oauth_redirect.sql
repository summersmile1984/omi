-- Persist the state-bound desktop deep link so Calendar OAuth can return to
-- named macOS bundles (each bundle owns a different URL scheme).
ALTER TABLE cf_google_calendar_oauth_states
  ADD COLUMN success_redirect_url TEXT;

CREATE INDEX IF NOT EXISTS cf_google_calendar_oauth_states_redirect_idx
  ON cf_google_calendar_oauth_states(success_redirect_url);
