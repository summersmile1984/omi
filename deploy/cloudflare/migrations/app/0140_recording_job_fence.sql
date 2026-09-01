-- Audio URL admission uses the shared cf_jobs queue authority.  Recording
-- deletion already fences source/output tables; fence this shared job row as
-- well so a GET racing the deletion intent cannot publish new audio work.
CREATE TRIGGER IF NOT EXISTS rdf_i_jobs
BEFORE INSERT ON cf_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid = NEW.uid
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS rdf_u_jobs
BEFORE UPDATE ON cf_jobs
WHEN EXISTS (
  SELECT 1 FROM cf_recording_deletion_intents WHERE uid IN (OLD.uid, NEW.uid)
)
BEGIN
  SELECT RAISE(ABORT, 'recording deletion fence');
END;
