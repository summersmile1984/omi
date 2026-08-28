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

`app_review_routes.py` owns public-app review rows and rating aggregates in the
same D1 transaction. Catalog rows carry a non-public `owner_uid` column so
self-review and developer-reply authorization fail closed when a legacy
projection has not been backfilled. Catalog readers hydrate bounded review
lists from that table; push delivery remains a separate external-provider
boundary and is not implied by a successful D1 mutation.

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
`legacy`, and malformed or incomplete `new` rows fail closed. A durable account
deletion intent or live deletion tombstone takes precedence over the cutover
row and projects the existing client-compatible `migrating` /
`migration_maintenance` wire fence, with product and legacy writes disabled.
This keeps already-shipped clients fail closed while the Jobs Worker purges the
account and prevents a deleted cutover row from reopening writes.

The asset API owns logical metadata in D1 and immutable object versions in R2.
Every upload creates a durable cleanup task before its R2 write; one D1 batch
then switches the logical pointer, schedules the superseded version, and clears
the new-object intent. Deletes commit the metadata removal and cleanup task in
the same batch. Request-time cleanup is best-effort, while the Jobs Worker
reconciles due tasks every 15 minutes and never deletes an object still named by
an active pointer.

Conversation list/detail/search and default deletion share the
`cf_conversations` authority. D1 FTS5 triggers project only bounded IDs,
structured metadata, and transcript text into a uid-token-partitioned search
index. The SQL uid predicate remains authoritative after FTS candidate lookup.
Default deletion matches the legacy `cascade=false` boundary and updates folder
counts in the same D1 batch; the Worker rejects cascade deletion until memory
retraction and audio cleanup have moved to the same authority.

`chat_routes.py` and `chat_session_routes.py` share the uid-scoped
`cf_chat_messages`, `cf_chat_sessions`, and `cf_chat_quota_events` authority.
Main-chat clear removes the current session atomically, while desktop scoped
deletes retain the session and decrement its message count. Client message IDs
are idempotency keys, desktop journal revisions advance monotonically, and an
accepted human desktop-chat write records its quota event in the message batch.
`chat_quota.py` projects UTC-month question and provider-cost usage from those
events and powers both the desktop quota read and mobile subscription fields.
Free-plan reservation is enforced atomically by API AI before provider work;
Workers AI token usage settles the event cost with the persisted exchange.
Unsettled provider costs make Architect projections unavailable instead of
silently undercounting. App/persona generation and attachments remain explicit
downstream cutover boundaries.

The Auth Worker places Better Auth account creation time in the signed internal
identity context. API Core uses that immutable projection for the optional
three-day desktop trial in quota, paywall, and trial reads; it never receives
direct access to Auth D1. Missing timestamps and entitlement lookup failures
preserve the legacy fail-open behavior.
