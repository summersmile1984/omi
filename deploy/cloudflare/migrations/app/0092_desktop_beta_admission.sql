-- Server-owned macOS Beta admission fence.  The control row is mutable only
-- through the API Core CAS helpers; release manifests and channel pointers
-- remain the immutable/roll-forward authorities in 0089/0090.
CREATE TABLE IF NOT EXISTS cf_desktop_beta_admission (
  id TEXT PRIMARY KEY CHECK (id = 'control'),
  schema_version INTEGER NOT NULL CHECK (schema_version = 1),
  promotion_enabled INTEGER NOT NULL CHECK (promotion_enabled IN (0, 1)),
  latest_reserved_tag TEXT,
  latest_reserved_build_number INTEGER,
  control_generation INTEGER NOT NULL CHECK (control_generation >= 1),
  latest_reserved_at INTEGER,
  admission_updated_at INTEGER NOT NULL,
  CHECK ((latest_reserved_tag IS NULL AND latest_reserved_build_number IS NULL AND latest_reserved_at IS NULL)
      OR (latest_reserved_tag IS NOT NULL AND latest_reserved_build_number IS NOT NULL AND latest_reserved_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS cf_desktop_beta_breakglass_audits (
  audit_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL CHECK (schema_version = 1),
  operation TEXT NOT NULL CHECK (operation IN ('rollback', 'rollout')),
  platform TEXT NOT NULL CHECK (platform = 'macos'),
  channel TEXT NOT NULL CHECK (channel = 'beta'),
  current_release_id TEXT NOT NULL,
  target_release_id TEXT NOT NULL,
  expected_generation INTEGER NOT NULL CHECK (expected_generation >= 0),
  actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
  reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
  incident_url TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  normal_path_unavailable TEXT,
  target_manifest_sha256 TEXT NOT NULL CHECK (length(target_manifest_sha256) = 64),
  resulting_generation INTEGER NOT NULL CHECK (resulting_generation >= 1),
  created_at INTEGER NOT NULL CHECK (created_at > 0)
);

CREATE INDEX IF NOT EXISTS cf_desktop_beta_breakglass_created_idx
  ON cf_desktop_beta_breakglass_audits(created_at DESC, audit_id);
