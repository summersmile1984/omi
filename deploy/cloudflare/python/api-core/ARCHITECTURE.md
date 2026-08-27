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
