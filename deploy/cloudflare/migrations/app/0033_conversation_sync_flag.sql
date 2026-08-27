ALTER TABLE cf_conversations ADD COLUMN private_cloud_sync_enabled INTEGER NOT NULL DEFAULT 0 CHECK (private_cloud_sync_enabled IN (0, 1));
