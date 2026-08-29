CREATE TABLE IF NOT EXISTS cf_app_payment_links (
  app_id TEXT PRIMARY KEY NOT NULL,
  owner_uid TEXT NOT NULL,
  stripe_account_id TEXT NOT NULL
    CHECK (stripe_account_id LIKE 'acct_%' AND length(stripe_account_id) BETWEEN 12 AND 160),
  stripe_product_id TEXT NOT NULL UNIQUE
    CHECK (stripe_product_id LIKE 'prod_%' AND length(stripe_product_id) BETWEEN 12 AND 160),
  stripe_price_id TEXT NOT NULL UNIQUE
    CHECK (stripe_price_id LIKE 'price_%' AND length(stripe_price_id) BETWEEN 12 AND 160),
  stripe_payment_link_id TEXT NOT NULL UNIQUE
    CHECK (stripe_payment_link_id LIKE 'plink_%' AND length(stripe_payment_link_id) BETWEEN 12 AND 160),
  payment_link_url TEXT NOT NULL
    CHECK (payment_link_url LIKE 'https://%' AND length(payment_link_url) BETWEEN 12 AND 2048),
  unit_amount INTEGER NOT NULL CHECK (unit_amount > 0),
  currency TEXT NOT NULL DEFAULT 'usd' CHECK (currency = 'usd'),
  interval TEXT NOT NULL DEFAULT 'month' CHECK (interval = 'month'),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (app_id) REFERENCES cf_app_catalog(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cf_app_payment_links_owner_idx
  ON cf_app_payment_links(owner_uid, active, app_id);

-- This provider-only tombstone has no user identity. It remains after the
-- catalog row is purged so a delayed Checkout webhook can never resurrect an
-- entitlement for a Payment Link retired by app/account deletion.
CREATE TABLE IF NOT EXISTS cf_retired_paid_apps (
  app_id TEXT PRIMARY KEY NOT NULL,
  stripe_payment_link_id TEXT NOT NULL
    CHECK (stripe_payment_link_id LIKE 'plink_%' AND length(stripe_payment_link_id) BETWEEN 12 AND 160),
  retired_at INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS cf_app_payment_links_owner_insert
BEFORE INSERT ON cf_app_payment_links
WHEN NOT EXISTS (
  SELECT 1 FROM cf_app_catalog
  WHERE id = NEW.app_id AND owner_uid = NEW.owner_uid
)
BEGIN
  SELECT RAISE(ABORT, 'app payment owner mismatch');
END;

CREATE TRIGGER IF NOT EXISTS cf_app_payment_links_owner_update
BEFORE UPDATE ON cf_app_payment_links
WHEN NOT EXISTS (
  SELECT 1 FROM cf_app_catalog
  WHERE id = NEW.app_id AND owner_uid = NEW.owner_uid
)
BEGIN
  SELECT RAISE(ABORT, 'app payment owner mismatch');
END;

CREATE TRIGGER IF NOT EXISTS cf_retired_paid_app_reuse
BEFORE INSERT ON cf_app_catalog
WHEN EXISTS (
  SELECT 1 FROM cf_retired_paid_apps WHERE app_id = NEW.id
)
BEGIN
  SELECT RAISE(ABORT, 'retired paid app id');
END;

CREATE TRIGGER IF NOT EXISTS adf_i_app_payment_links
BEFORE INSERT ON cf_app_payment_links
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid = NEW.owner_uid
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = NEW.owner_uid
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;

CREATE TRIGGER IF NOT EXISTS adf_u_app_payment_links
BEFORE UPDATE ON cf_app_payment_links
WHEN EXISTS (
  SELECT 1 FROM cf_account_deletion_intents WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
)
OR EXISTS (
  SELECT 1 FROM cf_account_deletion_tombstones WHERE uid IN (OLD.owner_uid, NEW.owner_uid)
)
BEGIN
  SELECT RAISE(ABORT, 'account deletion fence');
END;
