# API Core Worker architecture

`src/entry.py` is the ASGI composition root for the Python Worker. Domain route
modules use the injected Cloudflare bindings directly: D1 for uid-scoped
projections and mutation receipts, R2 for asset bytes, and external provider
APIs through the Worker fetch bridge. The route modules must stay async and
must not import Firestore, Redis, thread pools, local persistent files, or
process-lifetime network clients.

The goal and workstream modules share the validated evidence contract. Each
workflow mutation writes its domain projection and idempotency receipt in one
D1 batch; Edge authentication supplies the signed uid context before the
request reaches this Worker. Legacy workstream search/index refresh and
candidate automation remain outside this package until their own authority and
backfill contracts are migrated.

`memory_routes.py` is the canonical memory authority only for Better Auth
accounts created inside the isolated Cloudflare staging profile. It provides
uid-scoped list/create/edit/review/delete behavior in D1 and retains deletions
as tombstones. It has no Firestore fallback or dual write. Production account
promotion remains forbidden until the account-cutover importer, manifest
verification, and destination binding described by `INV-CUTOVER-1` exist.

`account_cutover_routes.py` is the routing authority consumed by Edge. Only
`ACCOUNT_CUTOVER_PROFILE=isolated-staging` may initialize a missing Better Auth
principal directly as `new`; the initializer writes a completed,
destination-bound row before returning. Every other missing principal stays
`legacy`, and malformed or incomplete `new` rows fail closed.

The asset API owns logical metadata in D1 and immutable object versions in R2.
Every upload creates a durable cleanup task before its R2 write; one D1 batch
then switches the logical pointer, schedules the superseded version, and clears
the new-object intent. Deletes commit the metadata removal and cleanup task in
the same batch. Request-time cleanup is best-effort, while the Jobs Worker
reconciles due tasks every 15 minutes and never deletes an object still named by
an active pointer.
