-- Per-user provenance for the Firebase -> Better Auth identity projection.
--
-- The import ledger authenticates the complete export.  This optional column
-- records the deterministic source row digest as well, so a projection can be
-- audited without retaining Firebase export data in D1.  Existing dormant
-- rows are deliberately nullable: the importer must backfill and verify them
-- before a bridge admission can use the projection.
ALTER TABLE cf_firebase_identity_projection
  ADD COLUMN sourceRecordSha256 TEXT
  CHECK (
    sourceRecordSha256 IS NULL OR
    (length(sourceRecordSha256) = 64 AND
     sourceRecordSha256 NOT GLOB '*[^0-9a-f]*')
  );

CREATE INDEX IF NOT EXISTS cf_firebase_identity_projection_provenance_idx
  ON cf_firebase_identity_projection(sourceImportId, sourceRecordSha256);
