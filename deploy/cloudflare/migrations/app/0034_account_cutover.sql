CREATE TABLE IF NOT EXISTS cf_account_cutover (
  uid TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
  state TEXT NOT NULL DEFAULT 'legacy' CHECK (state IN ('legacy', 'migrating', 'new', 'rolled_back_stranded')),
  account_generation INTEGER NOT NULL DEFAULT 0 CHECK (account_generation >= 0),
  ui_generation INTEGER NOT NULL DEFAULT 0 CHECK (ui_generation >= 0),
  api_generation INTEGER NOT NULL DEFAULT 0 CHECK (api_generation >= 0),
  stranded_new_data INTEGER NOT NULL DEFAULT 0 CHECK (stranded_new_data IN (0, 1)),
  offline_queue_instruction TEXT NOT NULL DEFAULT 'none' CHECK (offline_queue_instruction IN ('none', 'drain', 'quarantine')),
  checkpoint_phase TEXT NOT NULL DEFAULT 'not_started' CHECK (checkpoint_phase IN (
    'not_started', 'inventory', 'offline_queue_fenced', 'exporting', 'importing',
    'verifying', 'cutover_ready', 'completed', 'failed', 'paused'
  )),
  checkpoint_token TEXT,
  manifest_id TEXT,
  destination_backend_bound INTEGER NOT NULL DEFAULT 0 CHECK (destination_backend_bound IN (0, 1)),
  updated_at INTEGER NOT NULL
);
