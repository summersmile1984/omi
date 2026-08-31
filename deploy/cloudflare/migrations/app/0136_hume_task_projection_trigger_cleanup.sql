-- D1-compatible trigger cleanup for the Hume identity projection.
--
-- 0135 initially used a tautological WHEN arm so the immutable trigger also
-- carried the account-deletion inventory references.  Keep the same behavior
-- but express the fence as a conditional RAISE action, which is accepted by
-- both local SQLite and remote D1 without relying on a tautological predicate.
DROP TRIGGER IF EXISTS adf_u_hume_task_projections;

CREATE TRIGGER IF NOT EXISTS adf_u_hume_task_projections
BEFORE UPDATE ON cf_hume_task_projections
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence')
    WHERE EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = OLD.uid OR uid = NEW.uid)
       OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = OLD.uid OR uid = NEW.uid);
  SELECT RAISE(ABORT, 'hume task projection immutable');
END;
