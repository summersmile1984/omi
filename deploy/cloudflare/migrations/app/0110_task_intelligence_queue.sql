-- Persist the bounded evaluation input beside its lease.  A Queue message is
-- only a wake-up signal; the D1 row is the recovery authority when a delivery
-- is delayed, duplicated, or the scheduled reconciler has to republish it.
ALTER TABLE cf_task_intelligence_jobs
  ADD COLUMN input_json TEXT NOT NULL DEFAULT '{}';
