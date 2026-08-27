CREATE TABLE IF NOT EXISTS cf_user_training_data_opt_in (
  uid TEXT PRIMARY KEY NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending_review', 'approved', 'rejected')),
  requested_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS cf_user_training_data_opt_in_updated_idx
  ON cf_user_training_data_opt_in(updated_at);
