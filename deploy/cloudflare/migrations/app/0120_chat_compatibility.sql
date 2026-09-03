-- Cloudflare-owned Chat-first materialization receipts.
--
-- The legacy endpoint used a Firestore intent journal.  The Cloudflare
-- boundary keeps the same important properties (uid/generation ownership,
-- one-shot receipts, and account deletion fencing) while deriving new daily
-- openers from the canonical D1 goal/task tables.  Blocks are immutable JSON;
-- clients never get to supply or rewrite prompt content through this route.
CREATE TABLE IF NOT EXISTS cf_chat_first_intents (
  uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
  intent_id TEXT NOT NULL CHECK (length(intent_id) BETWEEN 1 AND 128),
  continuity_key TEXT NOT NULL CHECK (length(continuity_key) BETWEEN 1 AND 300),
  account_generation INTEGER NOT NULL CHECK (account_generation >= 0),
  source TEXT NOT NULL CHECK (source IN (
    'daily_opener', 'capture_arrival', 'deferral_reraise',
    'agent_judgment', 'cold_start_rich', 'cold_start_sparse'
  )),
  subject_kind TEXT CHECK (subject_kind IS NULL OR subject_kind IN ('task', 'goal', 'capture', 'cold_start')),
  subject_id TEXT CHECK (subject_id IS NULL OR length(subject_id) BETWEEN 1 AND 128),
  blocks_json TEXT NOT NULL CHECK (length(blocks_json) BETWEEN 2 AND 32000),
  delivery_state TEXT NOT NULL CHECK (delivery_state IN ('ready', 'pending_kernel_receipt', 'delivered')),
  created_at INTEGER NOT NULL,
  delivered_at INTEGER,
  materialization_receipt_id TEXT CHECK (materialization_receipt_id IS NULL OR length(materialization_receipt_id) BETWEEN 1 AND 128),
  cold_start_sequence_terminal_state TEXT CHECK (
    cold_start_sequence_terminal_state IS NULL OR cold_start_sequence_terminal_state IN ('completed', 'abandoned')
  ),
  cold_start_sequence_terminal_receipt_id TEXT CHECK (
    cold_start_sequence_terminal_receipt_id IS NULL OR length(cold_start_sequence_terminal_receipt_id) BETWEEN 1 AND 128
  ),
  PRIMARY KEY (uid, intent_id),
  UNIQUE (uid, account_generation, continuity_key)
);

CREATE INDEX IF NOT EXISTS cf_chat_first_intents_ready_idx
  ON cf_chat_first_intents(uid, account_generation, delivery_state, created_at, intent_id);

CREATE TRIGGER IF NOT EXISTS adf_i_chat_first_intents
BEFORE INSERT ON cf_chat_first_intents
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.uid)
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.uid)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_chat_first_intents
BEFORE UPDATE ON cf_chat_first_intents
WHEN EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.uid, NEW.uid))
   OR EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.uid, NEW.uid))
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
