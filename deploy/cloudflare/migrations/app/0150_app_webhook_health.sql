-- Graduated per-app webhook endpoint health (port of the legacy
-- webhook_health.py contract): a continuous-failure window opens at the first
-- failure after a success, warns the app owner after one and two days, and
-- auto-disables delivery after three days of unbroken failures. A success (or
-- an owner webhook-config change) resets the window. App deletion cascades
-- the row away with the catalog entry; the table is app-scoped and carries no
-- uid column.
CREATE TABLE IF NOT EXISTS cf_app_webhook_health (
  app_id TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 256),
  endpoint TEXT NOT NULL CHECK (endpoint IN ('integration')),
  first_failure_at INTEGER NOT NULL CHECK (first_failure_at >= 0),
  last_failure_at INTEGER NOT NULL CHECK (last_failure_at >= first_failure_at),
  last_success_at INTEGER,
  failure_count INTEGER NOT NULL CHECK (failure_count >= 1),
  last_status INTEGER NOT NULL,
  last_error TEXT NOT NULL CHECK (length(last_error) <= 200),
  notified_day1 INTEGER NOT NULL DEFAULT 0 CHECK (notified_day1 IN (0, 1)),
  notified_day2 INTEGER NOT NULL DEFAULT 0 CHECK (notified_day2 IN (0, 1)),
  disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (app_id, endpoint),
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);
