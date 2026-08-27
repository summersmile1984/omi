CREATE TABLE IF NOT EXISTS cf_goal_mutations (
  uid TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  account_generation INTEGER NOT NULL,
  request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (uid, operation, idempotency_key)
);
