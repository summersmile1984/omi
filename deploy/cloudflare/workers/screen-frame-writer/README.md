# Screenshot storage Worker

This Worker is the only execution identity bound to `SCREEN_FRAMES` R2. API
Core canonicalizes and adjudicates images with the upstream privacy contract;
it receives a service binding to this writer. Jobs also receives a service
binding for account erasure. The writer has App D1 and this bucket only: no AI
binding, other buckets, service dependencies or direct public deployment route.
The resource renderer enforces that separation. There are now nine Worker
roles and six R2 resource roles; existing Eddy resources need a new allocation
and fresh candidate before this topology can be deployed.

`POST /internal/screen-frames/write` accepts a short-lived HMAC approval plus
the exact canonical JPEG and thumbnail. The dedicated
`SCREEN_FRAME_SIGNING_SECRET` belongs to Core and this writer, independently of
the common request assertion key. The approval binds issuer, audience, uid,
conversation, attempt, epoch, model/policy/prompt identity, decision, both byte
digests and metadata. It is never a client credential. The writer verifies the
claims and bytes and consumes the approval ID in D1 before R2 work.
Readiness and image admission also reject equal actual signing/assertion keys,
so distinct secret reference names alone cannot mask a reused credential.

Migration 0166 owns settings (legacy default enabled), survivor sets, attempt
identity and write receipts. A receipt enters `writing`, then `ready` after
both uploads finish. D1 publication changes survivors to `committed` and
evicted images to `cleanup`. A changed epoch or account/subject deletion
cancels in-flight writes. Receipts cannot be deleted before acknowledged
storage erasure. Account setting disable hides retained images and cancels
pending uploads, preserving the upstream behavior of retained committed images.

Each R2 multipart upload ID is stored in the receipt before its first image
part. Cleanup fences the receipt, aborts both upload handles, deletes completed
objects, then marks the receipt `deleted`. A late initiation cannot register
or upload bytes after that fence. Ordinary deletion retains a consumed approval
until expiry; account erasure can remove it immediately because its account
fence remains authoritative. The five-minute cron processes bounded cleanup
batches. Storage errors retain the receipt for retry.
The same scheduler removes at most 128 expired adjudication attempts per pass,
preserving their 24-hour replay window and any live or other-owner attempts in
an owner-scoped cleanup call.

Cloudflare documents `NoSuchUpload` as Workers error code 10024. An abort that
resolves or reports that exact code has no active upload; other failures are
retried. Aborting a completed upload does not erase its final object, so R2
object deletion is still mandatory. The contract test executes those operations
through HTTP inside the pinned local workerd. A second native test applies all
App D1 migrations and exercises the actual writer's admission, publication,
content read, revocation and erasure against local D1/R2. This is local runtime
evidence; hosted R2 concurrency remains part of production qualification.

`GET /v1/screen-frame-content` uses a separate one-hour capability. Every read
checks current D1 membership, account/subject existence, global setting and
shared visibility, both before and after the R2 read. The response disables
caching. Core issues these capabilities and streams the writer's response through
its public-shaped content proxy; the writer remains the sole owner of image IO.

Jobs calls `/internal/users/{uid}/screen-frames/cleanup` or `residual` with the
common internal assertion, bound to method, path, uid and the writer audience.
The account deletion owner waits for the writer to confirm zero image/receipt
residuals before D1 purge and Auth deletion. Generic D1 purging cannot remove
writer receipts. The same writer check participates in subsequent zero scans.

Tests run in the existing `deploy/cloudflare/ci/routes.sh` lane:
`screen-frame-writer.test.ts`, `screen-frame-r2.test.mjs`, account deletion and
resource-plan tests, plus Core's user export test. The eight upstream public
screenshot routes remain classified as blocked. Core now also has native local
PNG → controlled judge → signed approval → writer → D1/R2 → content-proxy evidence.
Hosted model access, supported-input memory limits, Edge routing and production
business qualification remain outstanding.

Protocol sources:
[R2 Workers multipart API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/),
[R2 error codes](https://developers.cloudflare.com/r2/api/error-codes/).
