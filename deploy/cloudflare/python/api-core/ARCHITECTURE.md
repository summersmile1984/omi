# API Core Worker architecture

`src/entry.py` is the ASGI composition root for the Python Worker. Domain route
modules use the injected Cloudflare bindings directly: D1 for uid-scoped
projections and mutation receipts, R2 for asset bytes, and external provider
APIs through the Worker fetch bridge. The route modules must stay async and
must not import Firestore, Redis, thread pools, local persistent files, or
process-lifetime network clients.

The feedback source projector stages the upstream wire models, desktop rating
contract and pure daily-report policy into both the ordinary build and tests.
`feedback_store.py` appends events in the same D1 batch as each rating projection;
`feedback_reports.py` owns leased, bounded and atomic daily publication. Report
entries remain separate uid-scoped rows with cascading event deletion, while
aggregate headers contain no user identity. Events participate in export and
both per-user tables participate in account erasure.

`feedback_context.py` reads metadata-only chat windows and hydrates text only
through `feedback_admin_routes.py`. The latter requires a request-bound internal
feedback actor minted by Jobs after its existing ADMIN_KEY gate. User assertions
cannot invoke the private service. Native message wire timestamps retain exact
microseconds; history ordering slots are not treated as Unix time. Current
deletion fences apply to context reads and report publication, including cached
snapshots. Full API and scheduling details live in
`docs/doc/developer/ForkCloudflareFeedback.mdx`.

The build stages eight `screen_frames_*` modules directly from the upstream
screenshot contract owners. Pillow 11.3.0 supplies the unchanged canonicalizer
and palette implementation in Pyodide. The staged privacy prompt is identical
to the upstream prompt; there is no Cloudflare-specific prompt rewrite. These
modules are projected into both the ordinary build and the CPython test runner. The
independent `screen-frame-writer` Worker now owns `SCREEN_FRAMES` R2 and one-use
write receipts. Core receives only its service binding and the dedicated
`SCREEN_FRAME_SIGNING_SECRET`. The export includes screenshot settings and
visible sets; upload journals remain internal.

`screen_frame_views.py` registers the seven settings/read/sharing/delete routes
in Core. The staged upstream types and selection function own their wire shape,
banner threshold and strip order. Deletes increment revision and cancellation
epoch in one D1 mutation; per-frame deletion compares both counters before
publishing, so a concurrent removal cannot be restored. The database transitions
removed receipts to cleanup; the isolated writer retries physical R2 erasure.
Setting disable hides retained frames and cancels pending writes. Sharing changes
do not bump revision. Public reads resolve the established unique share index
and return an empty set for unavailable/private/disabled subjects.

`screen_frame_content.py` issues only scoped read capabilities using the admitted
API origin. Its public content proxy forwards only the token to the writer and
streams the response without buffering the image. Reader cancellation propagates
on disconnect. The writer checks live D1 state after R2 fetch, revoking old URLs
when the subject/frame is removed, sharing is withdrawn or screenshots are disabled.
All successful responses use no-store. Malformed stored frames are omitted with
the shared sanitized fallback event; missing legacy palette uses the upstream
neutral colors. Invalid signing configuration fails the entire read with 503.

`screen_frame_adjudication.py` now registers candidate admission and processing.
The upstream transport digest checks, capture window and request fingerprint are
staged without rewriting their behavior. Every candidate digest is checked before
the first model call; binary bytes are decoded again per candidate instead of
retaining a second whole batch. Codec and judge failures reject that candidate;
writer failures return 503 without a completed attempt. Global egress remains
default off. It requires the explicit flag, isolated signer, AI binding and
successful writer readiness before a candidate can leave Core.

`screen_frame_judge.py` sends the unchanged upstream privacy prompt and judgement
schema through `AI.run('google/gemini-2.5-flash-lite', ...)`. Contradictory or
malformed output never authorizes storage. Only the judged canonical JPEG and
its derived thumbnail/metadata enter a signed write approval. Usage uses the
existing `screen_frame_judge` feature in the D1 LLM ledger.

`screen_frame_adjudication_store.py` owns the 24-hour attempt reservation and
replay response. A D1 batch acquires the attempt response against the current
revision/epoch/admission snapshot, publishes the selected survivors, and marks
newly written evictions for cleanup. Any frame-publication trigger failure rolls
back the response too. Concurrent new attempts merge against a fresh snapshot;
privacy epoch changes cancel the whole pending publication. An all-rejected pass
sets `adjudicated_at` without incrementing revision, matching the upstream rule.
The storage worker expires attempt rows in bounded batches.

Core's entire screenshot pipeline has now run in local workerd with real PNG
canonicalization, actual writer approval verification and real D1/R2. Inference
was controlled and checked the original prompt hash; this is not model-quality
or hosted-provider evidence. Edge routing and the eight inventory slots remain
blocked until hosted model access, supported-input memory bounds and complete
business qualification are resolved. The current upstream 64-megapixel codec
limit must not be mistaken for proof that those images fit a hosted Worker.

`frame_request_routes.py` registers frame creation, status, pending delivery,
state transitions and the JIT decision envelope in Core. The ordinary builder
stages the exact upstream frame wire model, its pure lifecycle policy (one
module import redirected), and the original decorated JIT decision dataclasses
and policy. Those source owners participate in release identity and the existing
route test lane. There is no prompt change.

`jit_authority.py` is the Cloudflare provider: App D1 stores deployment defaults
at `cf_jit_flags.uid = ''` and optional account overrides. A deployment kill
cannot be cleared by a user override. Missing rollout retains the upstream
negative decision; the upstream allowlist and kill precedence remain in the
staged pure policy. This provider does not use PostHog or an isolate cache.
Provider failure emits the bounded fallback event and cannot admit frame work,
even if the original allowlist decision is enabled, because no current account
generation can be established. Missing legacy cutover rows retain generation 0.
The same JSON provider snapshot and account generation are compared inside the
D1 read or write that returns metadata, admits work or changes state.

`frame_request_store.py` owns the immutable request identity, active-intent
replay across minute buckets, terminal retry numbering and bounded delivery.
Migration 0167 enforces eight pending requests per device/generation, one active
or attached request per conversation, immutable owner/identity and account
fences. State writes compare the previous state and current authority together;
a concurrent cancellation cannot be replaced by a stale claim. Polling expires
at most 32 old active rows and returns only the requesting device's requested
rows. Frame metadata participates in the existing user export, conversation
deletion and account-deletion residual/purge owners.

`frame_request_pixels.py` now owns image upload, promotion and private reads.
The upstream policy constants and Server reference canonicalizer are staged from
the original router. `frame_image_transform.py` uses the required `IMAGES`
binding for full-resolution decoding, EXIF transforms, resizing and JPEG encoding.
Core reads container metadata without allocating the full pixel plane, removes
metadata before transformation, applies orientation explicitly and strips output
metadata again. This matters because the native Images service preserved JPEG
EXIF and did not apply the tested WebP EXIF orientation automatically.
JPEG/PNG validation uses Pillow headers and bounded PNG metadata parsing; WebP
uses RIFF metadata plus `Images.info`, since Pillow's WebP decoder allocates full
canvases during open. The output retains the upstream 1920-long-side/2.5M-pixel
bounds, white alpha background and JPEG quality 85. Codec byte identity across
Server/Pyodide/Images is not assumed. The exact returned JPEG is stored and read.
An Images outage leaves the claim retryable and writes no R2 object.

The upload endpoint keeps multipart `file` and query
`device_id`/`account_generation`. `frame_upload_form.py` bounds this route's total
multipart body to 10 MiB plus 64 KiB overhead, one file and eight small fields.
Its Starlette parser stays in memory, avoiding the hosted cold-start temporary
file `Bad file descriptor` error. Files above 10 MiB remain 413; a missing `file`
retains FastAPI's 422 shape. The parser closes partially admitted files on source
failure or cancellation. Other multipart routes keep their own parser ownership.
`python-multipart` 0.0.31 and Pillow 11.3.0 remain pinned; screenshot adjudication
still uses its separate upstream canonicalizer and has its own qualification.
Only upload/promotion can publish pixel states; JSON-only state updates cannot
fabricate storage and return 409 for those transitions.

Migration 0168 records every R2 multipart handle before uploading the first part.
Temporary and permanent bytes use separate `FRAME_REQUESTS_TEMPORARY` and
`FRAME_REQUESTS` bindings. Each copy has an immutable object ID; the deterministic
permanent storage identifier resolves to exactly one live copy. One D1 statement
publishes the object, changes request state, appends the conversation photo and
sets the content/photo markers. Trigger failure rolls the whole publication back.
CASE expressions in these triggers stay parenthesized because the remote D1
query parser otherwise mistakes their END for the trigger terminator. Local
SQLite success alone does not qualify migration transport; fresh remote Wrangler
migrations passed after this [repair](../../../../dev/unified-main/implementation-2026-09-05/frame-d1-migration-2026-09-06.md).
An ambiguous response re-reads the authoritative state instead of deleting live
bytes; competing promotion copies return the winning request and queue their own
unreferenced copies for erasure.

`workers/jobs/frame-request-storage.ts` runs from the existing scheduler and
account-deletion owner. Expiry and conversation/photo removal atomically turn
object receipts into cleanup work. Cleanup aborts the durable upload handle
before deleting the object, preventing a late completion after erasure. Failed
erasure retains the receipt, retry time and original frame cleanup status.
Account deletion waits for both bucket residuals and the journal to become empty.
Permanent photos follow conversation lifetime and remain readable when JIT is
stopped; temporary reads recheck rollout, generation, expiry and live ownership
after the R2 fetch. Both read paths stream authorized bytes with no-store.

A real local workerd run exercised PNG upload, temporary reads, concurrent
promotion, conversation photo reads, deletion and the actual Jobs cleanup against
two local R2 buckets. The supported-input envelope subsequently passed hosted
native Images qualification. All eight public Edge routes now use the existing
Better Auth/account admission proxy and upstream hourly per-account limits
(read 120, write 120, upload 30). The authenticated hosted flow passed 35 HTTP
requests for creation, upload, private reads, promotion, revocation and actual
cleanup against isolated D1/R2. Its client read the public account control and
carried the current generation, rather than assuming the legacy default 0.
[The evidence and fixture boundaries](../../../../dev/unified-main/implementation-2026-09-05/frame-public-hosted-2026-09-06.md)
distinguish this storage family from full deployment, desktop capture, scheduled
cleanup and JIT memory/trigger qualification. JPEG encoding is runtime-specific;
stored and served bytes are checked against that runtime's canonical output.

`referral_routes.py` preserves the desktop `ref1` HMAC wire format using a
dedicated `REFERRAL_SIGNING_SECRET`. Link and login destinations come from the
rendered API/Web origins. The secure HttpOnly referral cookie is presentation
state; only the signed code and Auth-issued account creation time admit a
claim. Migration 0165 commits one immutable claim and its 30-day Operator
subscription together. Existing/missing-age, self-referred and paid accounts
receive `claimed: false`; retries cannot extend or replace the grant.

All subscription entitlement readers use `cf_effective_user_subscriptions`.
This D1 view expires the exact non-Stripe referral grant at database time, so
no scheduler delay can extend it. Billing mutations retain the physical
`cf_user_subscriptions` owner; a later Stripe subscription is independent.
Inviter attribution lives separately from the recipient's one-time receipt.
Both participate in export/deletion; erasing an inviter removes the relation
without making the recipient eligible again. D1 fences reject late claims.

`desktop_daily_usage_routes.py` owns running counters keyed by uid/local date/
client device. A single D1 upsert takes the maximum of each counter, preserving
retries and out-of-order delivery without an in-memory lock. The request applies
strict integer limits, exact date syntax, an IANA timezone and a two-day local
window; Edge applies the existing 600/hour user policy. Export reads the same
table, and deletion triggers plus the Jobs residual registry fence and purge it.
The pinned `pytz` wheel supplies IANA data inside Python Workers without an OS
timezone database. This route does not generate daily summaries.

`daily_summary_routes.py` exposes the daily recap projection. Its create,
regenerate and settings-test entries delegate to `daily_summary_generation.py`:
quota admission, timezone resolution, one D1 lease per uid/date, bounded Workers
AI inference, actual feature-usage recording, and conditional D1 publication.
Create reuses stored records and arms its 30-second cooldown only after creating
a recap; regenerate preserves id/creation/sharing and reserves a separate
cooldown before calling the model. Deleting a recap revokes the pending lease.

`daily_summary_content.py` reads only uid-scoped, unlocked conversation evidence
and canonical task/memory rows. Model output cannot author task identities,
memory references or statistics. Memory eligibility uses canonical lifecycle,
generation and sensitivity rules, rejecting expired or user-rejected rows.
One SQL publication guard compares all selected source fields plus the lease,
closing the model-await window against privacy mutations. Account deletion
fences and purges both tables; export omits the internal generation token.
The same HTTP product contract runs real recording → Queue enrichment → recap
→ export → queued deletion. Provider IO is controlled there. Notification
scheduling and delivery are still a separate migration/qualification boundary.

The export route also resolves its download filename from `brand_runtime`.
Missing brand presentation metadata emits shared fallback telemetry and uses a
neutral filename, keeping the owner's privacy operation available.

The goal and workstream modules share the validated evidence contract. Each
workflow mutation writes its domain projection and idempotency receipt in one
D1 batch; Edge authentication supplies the signed uid context before the
request reaches this Worker. Legacy workstream search/index refresh and
candidate automation remain outside this package until their own authority and
backfill contracts are migrated.

`email_preference_routes.py` owns the public lifecycle unsubscribe capability.
GET validates the canonical upstream HMAC and renders a branded POST form without
writing. POST ignores the body, verifies the same purpose-bound token and upserts
the lifecycle opt-out in App D1. Both resolve account existence through a signed,
request-bound Auth `/internal/profile` call; missing accounts, malformed tokens,
deletion fences and dependency failures return the same neutral 400 HTML.
Migration 0160 fences late writes against account deletion. Export reads the
same consent row and the existing Jobs deletion registry removes it. The shared
HTTP suite covers invalid-link parity, and the recording/privacy suite proves
actual scanner GET, one-click POST, export, isolation, queued erasure and old-link
denial. This adds the consent surface, not a lifecycle email sender.

`csat_routes.py` owns the product configuration singleton and one immutable
rating per UID/platform. It matches the upstream CSAT validation, normalization
and 201/409 receipt contract; default display copy uses the deployment brand.
Migration 0158 installs the unique key, create-only protection and account
deletion fences. Ratings participate in export and the existing Jobs residual
purge; there is no second feedback writer. Shared Server/CF HTTP cases cover
admission and resubmission, while the recording/privacy runner proves export
and real queue-driven erasure. This does not qualify the separate admin feedback
reporting or referral delivery families.

`advice_routes.py` owns the isolated profile's proactive coaching rows in D1.
Create/list/update/delete and mark-all-read share one uid-scoped authority;
dismissed filtering and category pagination are computed from the same table.
The table participates in account-deletion mutation fences and residual purge,
with no Firestore fallback or dual write.

`app_review_routes.py` owns public-app review rows and rating aggregates in the
same D1 transaction. Catalog rows carry a non-public `owner_uid` column so
self-review and developer-reply authorization fail closed when a legacy
projection has not been backfilled. Catalog readers hydrate bounded review
lists from that table; push delivery remains a separate external-provider
boundary and is not implied by a successful D1 mutation.

`memory_routes.py` is the canonical memory authority only for Better Auth
accounts created inside the isolated Cloudflare staging profile. It provides
uid-scoped list/create/edit/review/delete behavior in D1, persists desktop
read/dismiss state and the baseline flag, and retains deletions as tombstones.
Batch creation preserves the released 100-memory contract, drops per-file
onboarding imports, and atomically writes size-bounded JSON chunks plus usage
sources without per-memory D1 queries.
Native, MCP and Developer single/batch intake always starts in Short-term.
Manual category, explicit-user attribution and caller durability hints do not
admit new Long-term rows. D1 derives the capture/expiry fields and commits the
initial vector work in the same transaction. MCP duplicate intake preserves an
existing row's canonical tier instead of promoting it. Existing historical
Long-term rows are not demoted. This admission correction does not implement
the full consolidation/promotion or append-only knowledge-ledger authority.
Migration 0161 makes the current D1 row the lock authority for interactive
content, visibility, review and desktop-state writes, including MCP, developer
and conflict resolution. The trigger runs inside the write transaction, so a
lock acquired after a route's read still denies with the legacy 402 boundary.
A multi-row resolution or vector-outbox batch rolls back on denial; the shared
error translator preserves dependency failures as 503. Privacy tombstoning
and account erasure remain available for locked rows. Full-migration ASGI
regressions cover every writer and the late-lock window, while the shared HTTP
suite exercises normal manual-memory mutation and deletion on both targets.
This does not supply ledger lineage, history/revert, or complete index revision
authority. It has no Firestore fallback or dual write. Production account promotion remains
forbidden until the account-cutover importer, manifest verification, and
destination binding described by `INV-CUTOVER-1` exist.

Migration 0162 advances `item_revision` and the memory vector outbox inside the
same D1 write. Native, MCP, developer, X and conversation-cascade writers all
use that authority; application callers no longer author timestamp versions.
Creation, changes and hard deletion persist projection work, while the account
erasure fence prevents recreating work during purge. Existing timestamp
projections are rebased above their stored and pending versions. The
`cf_memory_projection_sources` view supplies the same eligibility decision to
the outbox and Jobs: lifecycle, lock, generation, expiry and restricted labels.
Jobs reads and acknowledges the actual revision, preserving a later same-second
edit while an earlier embedding is in flight. The native path relies on durable
scheduled reconciliation; MCP/developer notifications remain post-commit hints.
Migration 0163 adds immutable memory-vector publication IDs and a durable
artifact owner. `memory_vector_hydration.py` reads each candidate's canonical
content, revision, publication/model metadata and current access in one D1
snapshot. Native, MCP, developer and chat-tool vector searches consume that
snapshot without a second source read. Invalid candidates are classified before
the result limit; duplicate chunks are not missing items. Repairs retain owned
IDs in the existing artifact journal, re-evaluate canonical intent inside a D1
transaction and wake due projection work through post-commit Queue hints.
Unknown provider IDs never authorize deleting another account's objects.
Native diagnostics report actual rejections and pending D1 artifact records.
Hosted Vectorize qualification, other projection families and ledger lineage
remain required before full memory product qualification.

The same module owns the staging-only `GET /memory/archive/search` read
boundary. Archive rows live in the separate D1 `cf_memory_archive_items`
projection, and the request must find both the operator global read gate and a
uid-bound `cf_memory_control` row emitted by the cutover projection. The query
requires an explicit Archive flag, the current account generation, active and
processed source rows, valid evidence/label arrays, and no lock, tombstone, or
restricted sensitivity label. Missing or malformed control state denies the
read; the request never creates capability rows or falls back to Firestore.
The projection and control tables share the account-deletion mutation fence
and residual purge surface. No production backfill or control-writer endpoint
is implied by this read route.

`memory_admin_routes.py` owns the staging-only
`GET /memory/admin/users/{uid}/non-active-route-report` read boundary. It
reads the generation-bound `cf_memory_non_active_routes` projection only after
validating the server-owned `ADMIN_KEY`, completed destination-bound cutover,
and account-deletion fence. The route preserves the legacy six-outcome audit
shape and red flags for duplicate, missing, or default-visible outcomes, but
it never reads Firestore or creates capability rows. The paired
`POST /memory/admin/users/{uid}/short-term-lifecycle/run` is now a staging-only
Jobs owner backed by `0102_memory_short_term_lifecycle.sql` and
`0104_memory_lifecycle_projection.sql`: it requires a completed,
generation-bound D1 control row, records an idempotent run, and executes the
bounded expiry/source-tombstone policy with a Queue lease and retry consumer.
The transition audit is D1-only and guarded by the account-deletion fence; no
Firestore read, local executor, or legacy fallback participates. The route is
not a historical backfill or production-cutover claim, and the Edge/backend
manifests keep it staging-owned until those separately scoped operations are
approved.

`memory_review_routes.py` owns the D1 `cf_memory_review_queue` projection for
canonical memory conflicts. The `/v3/memories/review-queue*` endpoints are
uid-scoped behind Edge Better Auth, and canonical memory create/batch writes
append conflict rows in the same D1 batch. Reads verify the source
`updated_at` revision and SHA-256 content hash; changed or missing sources are
redacted and tombstoned. Resolution applies candidate/conflict mutations and
the queue state atomically, with deterministic commit IDs for idempotent
retries. MCP/developer memories that are already marked reviewed do not enter
this queue.

`knowledge_graph_routes.py` derives both released graph read shapes directly
from eligible long-term `cf_memories` rows; D1 remains the only authority and
there is no independently mutable graph store. Canonical pages use a signed,
uid-bound cursor plus a memory-revision fence, expose one catalog node per
visible memory, and fail closed if the source changes during a read. Delete and
rebuild preserve canonical state with the legacy conflict response; only a
positively identified legacy cutover principal receives the compatible no-op
response. Return-only extraction uses the native Workers AI binding, validates
referential closure and bounded node/edge counts, and never writes D1.

`synthesis_routes.py` owns the return-only desktop memory-log extraction,
conversation topic, calendar/Gmail/notes synthesis, and two-stage AI profile
contracts. It accepts
only the signed Better Auth identity from Edge, preserves the desktop trial
paywall and route-specific Edge limits, bounds untrusted prompt inputs, and
validates structured Workers AI output before returning it. These routes do
not write D1, call the legacy backend, or require a local model process.

`trend_routes.py` owns the public global `/v1/trends` read. It joins the
allowlisted category/topic projection in D1, sorts topics by bounded memory
counts, strips memory identifiers, and returns `503` when the projection is
unavailable instead of silently falling back to Firestore.

`chat_first_routes.py` owns the isolated staging capability check for
`POST /v1/chat-first/blocks/validate`. It validates the released block union,
checks every referenced task, goal, capture, meeting, or memory against the
uid-scoped D1 projections, and returns retry-stable opaque block IDs. It never
materializes prompts or writes chat state; cold-start blocks and incomplete
cutover rows fail closed.

The same module owns `POST /v1/chat/deferrals`, the generation-bound kernel
outbox receiver. Deferrals are stored as bounded question JSON with a stable
uid/generation/continuity identity and a 24-hour due time; retries return the
original receipt, while continuity conflicts and account fences fail closed.
Intent materialization and re-raise scheduling remain separate until their
durable D1 projections are migrated.

`goal_ai_routes.py` reads the canonical goal, memory, conversation, chat, and
Vectorize projections to produce goal suggestions and weekly advice through
Workers AI. Progress extraction evaluates all active goals in one structured
model call, accepts only known goal IDs and finite absolute totals, and commits
the goal metric, progress event, and daily history rows in one D1 batch. The
free-plan chat quota remains the cost gate for user-initiated extraction;
suggestion and advice retain their independent Edge limits and safe defaults.

`account_cutover_routes.py` is the routing authority consumed by Edge. Only
`ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED=true` plus an exact
`ACCOUNT_CUTOVER_MANIFEST_ID` may initialize a missing Better Auth principal
directly as `new`; staging uses `isolated-staging-v1` and the independent
Cloudflare production deployment uses `isolated-production-v1`. The initializer writes a completed,
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

`speech_profile_routes.py` owns the isolated staging account's biometric audio
in the dedicated `SPEECH_PROFILES` R2 bucket. The uploaded 16 kHz PCM WAV and
its duration metadata land atomically only after Workers AI detects speech;
there is no filesystem, local VAD/ASR, Redis, Firestore, or dual write. Profile
and sample keys are uid-scoped, playback URLs are short-lived HMAC assertions
bound to the exact object, and downloads support one byte range. Account
deletion purges and residual-scans this bucket. The legacy best-effort hosted
speaker-embedding write remains a downstream realtime-identification cutover
boundary and is not treated as part of the upload success response.
People rows retain the ordered object-key/transcript pairing in D1. People
responses replace those keys with the same 60-second playback assertions;
single-sample deletion removes the exact R2 key before atomically removing its
aligned D1 entries, and person deletion purges the entire R2 person prefix
before deleting the row.

Conversation list/detail/search and default deletion share the
`cf_conversations` authority. D1 FTS5 triggers project only bounded IDs,
structured metadata, and transcript text into a uid-token-partitioned search
index. The SQL uid predicate remains authoritative after FTS candidate lookup.
Default deletion matches the legacy `cascade=false` boundary and updates folder
counts in the same D1 batch. `cascade=true` retracts derived data in the same
atomic batch — conversation-derived memories are soft-deleted, derived action
items removed, and vector retraction outbox entries written for every derived
row — then enqueues an idempotent per-conversation R2 audio purge job
(`conversation_audio_purge` in Jobs); the route refuses with 503 when the
queue binding is absent rather than silently dropping audio cleanup.
Visibility writes maintain `cf_shared_conversation_index` in the same D1 batch.
The index rejects cross-account conversation-id collisions, and public reads
join back through both uid and id before stripping location, external metadata,
and encryption-tier fields. Single-index and diarization-speaker assignment
writes use updated-at compare-and-set; the legacy-disabled speech-training path
is not revived, while bulk assignment remains legacy-owned because it still
extracts person speech samples asynchronously.

`followup_routes.py` owns Joan's follow-up-question read contract. The legacy
`DELETE /v1/joan/{memory_id}/followup-question` shape is preserved while the
Worker reads only the uid-scoped D1 transcript projection, returns an empty
result for short transcripts, and generates longer prompts with Workers AI.
Missing, in-progress, and locked conversation boundaries retain the legacy
status responses; malformed transcripts and unavailable model calls fail
closed without a legacy provider fallback.

`persona_routes.py` owns the authenticated Twitter-persona initial-message
read. It resolves the persona prompt from the D1 app catalog and uses the
Workers AI binding for the short greeting; missing persona data returns the
legacy empty message, while malformed catalog data or model failures fail
closed without reading Firestore or contacting the legacy LLM.

`tool_routes.py` owns the first-party conversation, transcript-chunk, memory,
and action-item REST tools. Lists read the canonical uid-scoped D1 rows;
semantic searches embed with the Workers AI binding, use tenant-namespaced
Vectorize candidates, map them through `cf_vector_projection_state`, and
re-hydrate authoritative unlocked D1 rows before returning the typed tool
envelope. Action-item create/update reuses `action_item_routes.py`, whose D1
mutation and vector projection outbox are committed in one batch before a Jobs
Queue hint is published. Edge owns the Better Auth assertion and the
`tools:search` / `tools:mutate` limits. Calendar-event creation remains on the
legacy Google Calendar provider boundary.

`chat_routes.py` and `chat_session_routes.py` share the uid-scoped
`cf_chat_messages`, `cf_chat_sessions`, and `cf_chat_quota_events` authority.
Main-chat clear removes the current session atomically, while desktop scoped
deletes retain the session and decrement its message count. Client message IDs
are idempotency keys, desktop journal revisions advance monotonically, and an
accepted human desktop-chat write records its quota event in the message batch.
`chat_quota.py` projects UTC-month questions from those events and provider
cost from `cf_llm_usage_daily`, powering both the desktop quota read and mobile
subscription fields.
Edge is the sole BYOK enrollment and key-validation authority. API Core treats
BYOK as active only when the request-bound signed context contains
`byokActive: true` and all four provider headers survived Edge validation;
caller-controlled headers alone never bypass trial, paywall, or quota policy.
Free-plan reservation is enforced atomically by API AI before provider work;
Workers AI token usage settles the event cost with the persisted exchange. The
NULL-to-settled D1 trigger increments `llm_usage_routes.py`'s feature/model
ledger exactly once, while desktop reports increment a separate account-aware
`desktop_chat` bucket. Summary reads exclude bucket rows, total-cost reads use
only the primary bucket dimension, and Architect combines managed chat plus
desktop bucket cost. Unsettled API-AI provider costs make Architect projections
unavailable instead of silently undercounting; desktop persistence rows do not
invent an event-local cost. App/persona generation and attachments remain
explicit downstream cutover boundaries.

`overage_routes.py` uses that same monthly authority for the legacy billing
explainer. Neo and Operator use proportional excess-question attribution;
Architect uses exact cost above the configured allowance. Any unsettled API-AI
provider event fails the billing projection closed until settlement completes.

`payment_callback_routes.py` owns the public, stateless checkout success,
checkout cancel, and customer-portal return pages consumed as terminal URLs by
native web views. Subscription state is never inferred from those navigations;
Stripe webhook projection remains the billing authority.

`integration_routes.py` owns app-scoped API-key consumers without accepting a
Better Auth identity from the caller. Edge preserves only the client
Authorization header and strips cookies/internal assertions; API Core hashes
the presented secret and requires a matching D1 key, enabled app, current paid
entitlement, and manifest action for the requested uid. Reads share the
canonical memory/conversation/action-item projections. Writes use the native
Workers AI binding for conversation structure or text-memory extraction and
commit product rows, usage, action items, and durable webhook fanout in one D1
batch. Notification/chat delivery uses the shared Jobs outboxes. No Firestore,
Redis, local model, or legacy fallback participates in this boundary.

MCP API-key creation, metadata listing, and revocation are owned by the Jobs
Worker and its uid-scoped D1 table. `mcp_routes.py` accepts only exact
`omi_mcp_` bearer keys, hashes the 32-hex secret payload, validates the
persisted scope set, and requires a completed destination-bound Cloudflare
account cutover with no deletion fence. The migrated REST tools read and write
the existing D1 memory, conversation, action-item, goal, chat, people,
screen-activity, daily-summary, and AI-profile projections. Profile contact
metadata is a best-effort signed service-binding call to Auth; API Core never
receives an Auth D1 binding. Edge strips cookies and forged internal identity
headers, and write limits use only an irreversible key digest. Legacy key
metadata can be backfilled without raw secrets and uses the reserved
`omi_mcp_legacy` display prefix. The three semantic-search routes embed queries
with the 1024-dimensional multilingual Workers AI BGE-M3 model, request
candidate IDs from four
tenant-namespaced Vectorize indexes, map those IDs through
`cf_vector_projection_state`, and hydrate uid-scoped D1 rows before returning.
Memory, action-item, and pre-transcribed conversation vector writes use a
durable D1 outbox plus Jobs Queue hints and cron repair. Account deletion removes
recorded vector IDs before D1 state purge. OAuth grants and hosted transport
remain explicit downstream cutover boundaries.

`developer_routes.py` authenticates `omi_dev_` credentials only from their
SHA-256 digests in `cf_developer_api_keys`, then enforces a route-specific
read scope before touching the same memory, action-item, folder, conversation,
goal, and Vectorize-backed projections used by first-party clients. The
account-cutover and deletion fences run on every credential use; raw keys never
enter D1, logs, or signed internal identity headers.
`developer_mutation_routes.py` carries the corresponding memory, action-item,
conversation-metadata, and goal write scopes into the existing D1 owners. It
keeps the public Developer limits and response projections, rejects locked
rows, preserves goal progress history, and publishes Queue projection hints
for vector-backed records without turning a Developer key into an internal
user session. `developer_conversation_create_routes.py` owns both Developer
conversation-creation shapes and the first-party pre-transcribed
`POST /v1/conversations/from-segments` path. The first-party path derives
stable device provenance from `X-App-Platform` plus `X-Device-Id-Hash` before
entering the same client-session-id claim. Workers AI produces the summary, action items,
memories, and discard decision; one D1 batch commits the completed conversation,
derived records, usage, Vectorize outboxes, installed-app fanout, and the legacy
`memory_created` Developer webhook outbox. Stable client session IDs use the
released UUIDv5 namespace and a 15-minute processing claim, so exact retries do
not repeat model or downstream work and failed enrichment removes its claim.
Generated memories cross a deterministic storage boundary first: each entry
must be a complete user-specific statement, have high token support in the
transcript, avoid task/scaffolding vocabulary, and survive overlap
deduplication plus a transcript-sized count cap. Rejected model output never
reaches D1, usage, webhooks, or Vectorize.
Jobs leases and delivers Developer webhooks without exposing response bodies or
allowing private-network targets. No legacy process, Firestore, Redis, or local
model participates in this creation boundary.

`conversation_finalization_routes.py` owns the explicit first-party
`POST /v1/conversations/{conversation_id}/finalize` admission and
`GET .../finalization` status projection. Admission changes only the D1
conversation row and its revision-keyed
`cf_conversation_finalization_jobs` record, then publishes a bounded Jobs
message. The Jobs Worker leases and retries that record and calls the private
API Core processor with a request-bound internal assertion. The processor
reuses the Worker-native enrichment, action-item/memory fan-out, usage, vector
outbox, and webhook batch used by `from-segments`; terminal state is recorded
only after that batch commits. BYOK requests, malformed transcripts, changed
ownership, and exhausted retries fail closed. Reprocess and merge remain
separate legacy boundaries until their canonical authority can share this
contract.

The Auth Worker places Better Auth account creation time in the signed internal
identity context. API Core uses that immutable projection for the optional
three-day desktop trial in quota, paywall, and trial reads; it never receives
direct access to Auth D1. Missing timestamps and entitlement lookup failures
preserve the legacy fail-open behavior.

`desktop_prompt_routes.py` owns the authenticated `/v2/desktop/prompts` read.
Operator-authored global `cf_desktop_prompts` documents retain upstream channel,
minimum-build, deterministic per-user rollout, defaults, and response bounds.
Missing configuration preserves the legacy empty response; unavailable D1 or
malformed documents fail with 503. The table stores no account data and has no
public write surface. Edge supplies the request-bound internal principal.
