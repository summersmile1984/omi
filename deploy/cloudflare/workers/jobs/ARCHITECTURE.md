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
in Queue retry counters. The Core planner keeps whole source items within the
provider message budget; this does not prove hosted model acceptance.

`task-recurrence.ts` shares the existing JOBS queue and five-minute reconciliation
lane. It reads the pending, current-generation receipt from cf_task_recurrence_inbox,
then signs a method/path/audience-bound internal Core request. Core owns original
qualification, Candidate identity and receipt settlement. A completed response
acks delivery; service failure retries. Cron scans at most 50 pending receipts,
continues other owners after a send failure and rediscovers lost initial hints.
Account erasure and generation changes exclude stale receipts. The existing DLQ
registry includes task_recurrence, and deletion residual/purge inventories include
the inbox. Tests cover the real Jobs queue, schema, signed boundary and recovery;
no hosted recurrence delivery has yet been accepted.


`candidate-integrations.ts` connects the accepted Candidate outbox to the existing
task integration and Firebase owners. Core owns all leases and receipt/task
mutations. Jobs reuses encrypted credentials, refresh and API mapping for Todoist,
Asana, Google Tasks and ClickUp. The provider function accepts normalized tasks;
the manual form retains its 500-character limit while canonical tasks preserve
their original 4,096-character description. Provider requests have a 10-second
abort deadline, including body consumption. Apple Reminders uses data-only FCM
with the original items/legacy fields and collapse tag, background APNs priority
5 and content-available. Missing credentials, no registered device, oversized
payload or failed push never count as successful Apple delivery. Firebase's
existing service-account setting remains its credential owner.

The existing Queue/DLQ and five-minute Cron include candidate_integration. Cron
scans at most 50 eligible receipts across owners and continues after one owner's
service failure. Generation changes and erasure suppress stale work. A settled
application failure acknowledges transport and waits for D1 backoff; Core service
failure retains Queue recovery. External APIs cannot share D1 atomicity: provider
success followed by a crash before settlement can be retried after lease expiry.
The upstream provider APIs have no universal idempotency contract; this work does
not claim globally exactly-once external creation. Local provider/FCM transports
are controlled in tests; hosted credentials and native device acceptance remain
release qualification work.
