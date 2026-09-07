# Jobs Worker

`index.ts` composes HTTP routes, Queue consumers and scheduled reconcilers.
`env.ts` declares their service, storage and provider bindings. Domain modules
own their D1 transactions and immutable work receipts; Queue delivery schedules
that work and does not replace its durable identity. Full route and provider
contracts are indexed in the [Cloudflare guide](../../README.md).

`account-deletion.ts` is the account erasure coordinator. It retains an App D1
account fence, cleans configured external providers, purges derived vectors and
object families, deletes product rows, performs two zero scans, then requests
Auth identity removal. Failures retain the fence and enqueue a retry.
`account-deletion-residual.ts` explicitly registers identity-bearing D1 tables
and object families; its schema-coverage test catches omitted new owners.

Memory creation shares Core's dedicated `MEMORY_PRIVACY_SECRET` and upstream
HMAC domain through `shared/memory-privacy-receipts.ts`. The X extractor writes
the key with each memory; D1 checks the receipt within that same transaction.
`memory-privacy.ts` expires 30-day receipts from the existing scheduled lane.
Account erasure includes the receipt table in its ordinary residual inventory.

`memory-privacy-cleanup.ts` continues public memory deletion through the existing
Jobs Queue and scheduled lane. It obtains immutable targets and renews legal-hold
admission from Core using method/path/audience-bound internal assertions, then
uses the shared vector artifact owner. Accepted asynchronous Vectorize deletion
does not acknowledge the task: the artifact owner must prove erasure before Core
can physically finalize memory/history rows. Pending tasks retry after ten
seconds. Durable all/default scope requests continue across 100-item batches;
cron revisits children and parent scopes in oldest-attempt order. No Core-to-Jobs
service binding or new credential is introduced. Public clients receive 503
while cleanup is pending; only completed finalization returns 200.

Screenshot storage has a separate execution identity. `screen-frame-storage.ts`
signs uid/method/path/audience-bound requests to `SCREEN_FRAME_WRITER` and
validates its residual acknowledgement. Jobs never receives `SCREEN_FRAMES` R2
or the screenshot approval key. The writer must abort uploads and delete images
before its receipts disappear. Generic D1 purging excludes those receipts, and
both the cleanup step and later zero scans require the writer to respond.
See the [writer contract](../screen-frame-writer/README.md).

JIT frame evidence uses its own two R2 bindings, `FRAME_REQUESTS_TEMPORARY`
and `FRAME_REQUESTS`. Core publishes the request/photo reference atomically in
App D1; `frame-request-storage.ts` owns physical erasure. It expires temporary
requests and abandoned writes, aborts each durable multipart handle before
deleting its object, and retains the journal with retry backoff on failure.
Conversation/photo deletion marks permanent objects for cleanup. Account erasure
requires the journal and both bucket prefixes to be empty before generic D1
purging can finish. The scheduled Jobs reconciler and account erasure coordinator
invoke the same cleanup owner.

Recording, sync, app, connector and import modules retain their respective
state transitions and queue dispatchers. Their storage boundaries are declared
in the Worker configuration and resource renderer; adding a bucket or service
requires updating that ownership graph and the applicable cleanup/export tests.
The existing route and product CI lanes exercise these modules through their
production functions and isolated actual Workers runtime.

`memory-consolidation.ts` connects the existing five-minute cron and JOBS Queue
with Core's signed internal consolidation dispatcher. D1 owns the account scan
and source-attempt state; messages contain only UID and account-generation hints.
Continuation sends a new delayed delivery before acknowledging the current one,
so healthy multi-page scans do not consume the Queue's three-retry budget. Failed
sends retain delivery recovery and are also rediscovered from D1. Generation
changes and account erasure make stale messages ineligible. The ordinary DLQ
capture/replay registry accepts this job kind.

Within the configured ten-message batch, consolidation calls overlap across
accounts; D1 still serializes the same account. Other job kinds retain their
sequential processing. Provider/model failure settlement remains in Core, not
in Queue retry counters. This dispatcher does not complete the remaining
provider-window planner or recurrence-to-workflow integration.
