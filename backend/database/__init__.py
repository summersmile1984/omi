"""Cloud-neutral storage shim hook.

When ``FIRESTORE_PG_DSN`` is set, install the firestore_pg facade into
sys.modules BEFORE any database module imports the real google-cloud-firestore
SDK, so the 88 business modules in this package transparently use PostgreSQL.
Without the env var this file is a no-op (production path unchanged).
"""

import importlib
import os

if os.environ.get("FIRESTORE_PG_DSN"):
    importlib.import_module("firestore_pg.compat").install()
