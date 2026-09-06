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
