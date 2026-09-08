# API Core Worker architecture

Request-bound assertions use the shared `assertion_path.raw_request_path`
extractor at every Python verifier. Edge signs the encoded URL pathname;
[ASGI](https://asgi.readthedocs.io/en/latest/specs/www.html#http-connection-scope)
decodes the routing path while preserving the original bytes in `raw_path`.
Missing or malformed raw bytes fail closed. Equivalent decoded paths do not
make differently encoded signatures interchangeable. Plain-path clients retain
the same contract. The existing component entry tests exercise this boundary.

`src/entry.py` is the ASGI composition root for the Python Worker. Domain route
modules use the injected Cloudflare bindings directly: D1 for uid-scoped
projections and mutation receipts, R2 for asset bytes, and external provider
APIs through the Worker fetch bridge. The route modules must stay async and
must not import Firestore, Redis, thread pools, local persistent files, or
process-lifetime network clients.

The ordinary builder also stages `memory_kernel_*` modules from the upstream
canonical apply models and pure Short-term lifecycle rules. Only import module
names change; source text, validators, receipt hashes and decision rules retain
their upstream owner. The same projector runs in Core's test setup, and each
upstream source participates in frozen release identity. The staged kernel is
persistence-free: a committed `ApplyResult` describes the complete item/graph/
operation/head/outbox bundle; it does not mean a D1 transaction committed.
Native Pyodide execution is qualified separately from D1 persistence, writer
convergence, lineage history and trigger/ledger APIs.

`memory_apply_intake.py` persists the kernel result for native single and batch
POSTs in one App D1 batch. Migration 0172 adds operation receipts, commits,
pending kernel outbox events and `cf_memory_apply_control`; the older
`cf_memory_control` remains the archive-capability projection. The transaction
checks the captured control JSON, actual account generation (including an
unmigrated account's generation zero), deletion fences and fresh item/operation
identities before inserting anything. Usage work shares that batch. Intake
does not infer consolidation review decisions from structural field conflicts.
An exact whole internal retry is a no-op; mixed retries and stale authority are
rejected. Public POSTs still allocate their IDs on the server.

Native single and batch intake now use the unchanged upstream
`required_processing_payload` and `_product_metadata_from_payload` helpers.
They enter pending Short-term with an owned processor/submission identity and
conserved source attribution; client-supplied processing or promotion fields
cannot grant admission. The route supplies `v3_manual`, `v3_api` or `v3_batch`
and the already-captured acceptance timestamp keeps preparation/retry identity
stable. Raw content remains readable through the native list, while vector
publication stays delete-only until processing is actually receipted. No model
call occurs in the POST and no default prompt changes. Existing processed rows
retain their state; this is not a bulk backfill. See the
[normalization verification record](../../../../dev/unified-main/implementation-2026-09-05/native-memory-normalization-2026-09-07.md).

`memory_default_read.py` supplies the D1 lifecycle predicate shared by the
native list and ordinary product search. Filtering runs before SQL counts and
pagination. Archive, hidden/superseded, source-removed and user-rejected rows
are absent by default; processed rows also obey the original sensitivity policy.
The native list retains the upstream required-pending exception so owners can
see new submissions immediately. Search requires processed state and reports
stored status/state. Time alone does not hide an active Short-term item.
Historical processed rows with no control/receipt remain readable without writes.
Behavioral tests execute the original canonical visibility bodies as an oracle.
This fixes lifecycle eligibility, not complete read convergence: active-alias
lineage selection, device/cursor parity, explicit native archive reads and other
read families still need alignment. See the [read verification record](../../../../dev/unified-main/implementation-2026-09-05/memory-default-read-2026-09-07.md).

`memory_history_routes.py` now owns `GET /v3/memories/ledger-history`. Its staged
upstream schema, canonical wire projector, history admission and page algorithm
retain the original owner-facing semantics. `memory_history_store.py` supplies
D1 keyset pages, physical privacy/lock admission and account/head/row rechecks.
It reads at most 32 rows / 1,000,000 document bytes per transfer, charges larger
single rows separately, and caps the scan at 4,000,000 bytes. Consumed budgets
return `X-Omi-List-Truncated: true`; authority/storage failures return 503.
The original 500-result / 501-provider-row / 5000-pagination-window limits remain.
It creates no control, migration or receipt and performs no provider call. The
unchanged head query/fingerprint also serves trigger snapshots through
`memory_read_authority.py`. Revert, lineage producers and full release acceptance
remain unfinished. See the [history verification record](../../../../dev/unified-main/implementation-2026-09-05/memory-history-2026-09-08.md).

`memory_apply_mutation.py` is the ordinary native user-mutation transaction
owner. Content correction, visibility, review votes, read/dismiss and baseline
all persist their item, operation, commit, control head and outbox together.
`memory_apply_item.py` owns the historical item projection; all former callers
use it directly. Existing physical product fields are imported into upstream
promotion metadata before a mutation, so updating one flag retains the others.
`memory_apply_edit.py` and `memory_product_mutation.py` supply the corresponding
upstream policies. Positive review does not grant promotion or processor
admission. Belief-model policy is the upstream default-off behavior of this
target; opt-in belief processing remains part of ledger/JIT convergence.

Migration 0177 stores the original `MemoryGraphAssertion` produced when an
ordinary mutation refreshes a graph-backed Long-term item. Only a transaction
guard that admits that exact uid/item/generation may insert it; the assertion
must match the new revision, content hash, graph plan and commit. Item changes
revoke the preceding assertion, and the new one shares the item transaction.
Privacy preparation and physical/account deletion remove it; owner export
includes it in `memory_ledger_data`. The consolidation transaction below now
uses this assertion owner too; shared graph aggregation remains unfinished. This table does not introduce
a public promotion shortcut.

`memory_consolidation_apply.py` accepts a complete upstream L2 batch and rehydrates
every pending source, candidate and negative example from the owner's D1 rows.
The unchanged upstream partition/reference policy runs before any write.
`memory_consolidation_policy.py` builds the same normalization and route applies
as the upstream owners; tests execute those original owners to compare the
complete patches and operation identities. The ordinary kernel projector also
stages the original decision schemas, required-processing receipts, provenance
conservation and review-record builder. No default prompt is changed.

Up to 20 decisions commit in one D1 batch under the account/head and item guards.
Required normalization and its receipt precede the route; promotion precedes
dependent duplicate routes. Items, superseded peers, graph assertions, review
records, operations, commits, control and outbox records all roll back together.
An old context cannot issue a second route. Returned items use the actual stored
JSON and integer-second timestamp representation. Provider publication is a
post-commit hint backed by the existing durable projection outbox.

Migration 0182 separates the consolidation transaction's read dependencies from
its write targets. Only items actually changed by the canonical apply plan enter
`expected_items_json`; candidate/owner-feedback snapshots enter
`observed_items_json`. The read guard compares owner, generation, source state,
status, lock, revision and captured metadata inside the same D1 transaction.
Eligible hidden or locked rejection examples can inform a decision without
granting mutation authority. Existing interactive writers keep the unchanged
active/unlocked write gate; concurrent dependency changes abort the whole batch.
See the [read-dependency regression record](../../../../dev/unified-main/implementation-2026-09-05/memory-consolidation-read-set-2026-09-07.md).

`memory_consolidation_llm.py` now supplies the Workers AI invocation owner.
It stages the original prompt, bounded context formatter and message constructor;
its schema-format text matches the backend's pinned LangChain 1.3.3. The model
receives separate system/user text messages with the full original schema.
The original backend JSON Schema is frozen in
`contracts/consolidation-output-schema.json` and staged as data; runtime Pydantic
versions cannot rewrite the default prompt. The component suite compares it to
the current upstream model and tests the observed older-runtime difference.
No tokenizer, vocabulary download or LangChain runtime is added to Core.
`WORKERS_AI_MEMORY_CONSOLIDATION_MODEL` defaults to
`@cf/qwen/qwen3.8-27b`. Overrides must support the same Chat Completions
contract: ordinary messages, `max_completion_tokens`, and one completed
`choices[].message.content` response. As in upstream, the schema is part of the
original prompt and the reply is validated afterwards; no `response_format`
is added by the adapter. Calls use medium reasoning effort, temperature zero,
at most 8,192 completion tokens and a 180-second timeout. Truncated completions,
refusals, multiple choices and reasoning-only output cannot reach apply. Input/output bridge
budgets are 110,000 / 256,000 UTF-8 bytes; these are not a token-window guarantee.
The source/owner/control is validated before provider disclosure and rehydrated
again for apply. Provider usage is recorded in `cf_llm_usage_daily` under
`memory_consolidation`; invalid/missing usage or output prevents apply. Errors
retain pending work and do not synthesize a route or switch providers. The
leased runner passes UID/source IDs through `memory_consolidation_planning.py`;
`memory_consolidation_context.py` queries BGE-M3/Vectorize and uses the existing
canonical D1 vector hydration owner. `consolidate_with_llm` accepts the selected,
verified context. No public or unleased background trigger is registered. See the [model invocation record](../../../../dev/unified-main/implementation-2026-09-05/memory-consolidation-llm-2026-09-07.md).

The context owner searches complete sources in 3,500-character overlapping
embedding windows, ranks results before the existing 100-ID hydration budget,
and preserves real scores for up to eight candidates per source. Account/source
state is fenced before disclosure and rechecked after retrieval. Owner-rejected
examples use the original 24-row scan, eight-example/180-character bounds,
sensitivity exclusion and active/hidden eligibility. Rehydration compares the
same bounded feedback text, while hidden rows remain ineligible as ordinary
candidates or pending sources. A feedback read failure retains pending work.

`memory_vector_readiness.py` now admits retrieval only after all eligible existing
sources outside the pending batch have complete, current publications and each
published vector has been observed in a real ANN query. The query uses the hashed
tenant namespace and an opaque `publication_id` filter (the immutable vector ID),
so identical embeddings cannot evict the target at the top-K limit. The metadata
index is a required release policy and must exist before publication. Migration
0179 records the expected vector count and per-vector proof in the existing
artifact journal. Legacy/partial/missing projections use the existing outbox and
Jobs reconciler; they are never assumed ready. One call checks at most 20 vectors,
retaining completed proofs for continuation. No vector values or source content
are returned by this check. A separate `projection_sequence` on the existing apply
control detects mapping changes between admission and candidate hydration; it does
not change the business ledger JSON. Account deletion can still retract mappings.

`ConsolidationIndexPending` is a resumable dependency wait, with a five-second retry
hint; it must not spend a model failure attempt or select a business route. Proofs
are checked again after retrieval without advancing them. Native intake creates
the canonical control row; an unmaterialized control is not maintenance-ready.
`memory_consolidation_runner.py` owns one bounded leased batch. `memory_consolidation_leases.py` and migration 0180 store the original non-content retry state
per exact source revision/hash.
Attempts retain the upstream three-attempt budget and 600-second lease; retry
sources are isolated and remaining input is returned to the dispatcher. Index
waits refund the attempt and persist a five-second due time. A generation fence
invalidates lease ownership without resetting that exact source's retry budget.

The planner measures the actual unchanged system/user message bytes against
the existing 110,000-byte limit. An oversized batch is reduced to a fitting
whole-source prefix; every reduction regathers candidate/feedback context and
rechecks index dependencies. No additional content truncation, tokenizer,
candidate-limit reduction, model call or prompt edit is used to fit a batch.
Only the selected prefix enters the single inference/apply invocation; ordered
remaining IDs resume through the existing durable dispatcher. Unused fresh
reservations are deleted under their exact, unexpired lease and generation CAS,
so batch sizing does not create zero-attempt retries that isolate every source.
Existing retry reservations retain their previous failure budget. A single
source still exceeding the message limit follows the original three-failure
terminal-review lifecycle without invoking Qwen, rather than deferring forever.
This is a byte-admission limit, not an exact model-token-window guarantee.
See the [batch-planning regression record](../../../../dev/unified-main/implementation-2026-09-05/memory-consolidation-planning-2026-09-07.md).

The last failure uses the original terminal-review decision, then its quarantine
route if review persistence fails. A recovered third attempt only leases terminal
settlement and cannot call the model again. Both terminal writes failing leaves
three-attempt work retryable for settlement. Apply validates ownership before
inference and in the D1 transaction, and clears successful attempts or stores
terminal state atomically with the canonical write. Source deletion and account
erasure purge operational state. Malformed source/retry rows retain their data
and block the cycle watermark without starving healthy sources; storage outages
propagate without acknowledging progress.

Migration 0181 records an account's dirty sequence in the same transaction as
source insertion, update or deletion. `memory_consolidation_dispatch.py` claims
one 15-minute account lease, selects at most 20 source IDs plus one lookahead,
and persists the original scan cursor between invocations. A cycle captures its
starting sequence: backdated intake arriving behind the cursor remains dirty
for the next cycle. Only a complete unblocked cycle acknowledges that sequence;
an applied unblocked cycle advances the original maintenance watermark. Cursor
and watermark share an account/head/generation/lease-guarded D1 transaction.
The migration seeds existing principals; absent controls use the existing genesis
constructor without fabricated business commits or backfill receipts.

`memory_consolidation_routes.py` admits only the signed, method/path/audience-bound
internal Jobs request at `POST /internal/memory/consolidation`. UID comes from the
assertion; the body supplies only account generation. The existing Jobs cron
finds due D1 work and Queue messages resume it, including recovery after a lost
send. The recurrence handoff below now persists with the memory batch; this
local wiring alone is not full production qualification.
Batch retry evidence: [hosted recovery trial](../../../../dev/unified-main/implementation-2026-09-05/memory-consolidation-retry-2026-09-07.md).
See the [index admission record](../../../../dev/unified-main/implementation-2026-09-05/memory-index-readiness-2026-09-07.md).
`recurrence_inbox.py` now adds the original RecurrenceInboxReceipt to the same
D1 batch as memory results and lease settlement. CandidateTransaction exposes
its prepared guarded statements so this composition does not create another
transaction implementation. Failed handoff rolls back memory; a concurrent
first receipt defers the batch and preserves the first proposal. Migration 0188
keeps that signal immutable and prevents completed receipts from reopening.

`recurrence_sources.py` projects the original consumption function, threshold
constants, proposal constructor and stable identities. Only its control read
and Candidate creation call become async storage calls. Qualification remains
unresolved, at least two occurrences on two distinct days and confidence >= 0.7;
proposal ownership confidence stays 0.5. Consumption creates a pending canonical
Candidate, never an automatically accepted task/workstream. The ordinary
idempotency owner recovers if Candidate creation commits before receipt ack.

Jobs receives only UID/receipt hints after commit. The existing five-minute Cron
rediscovers pending inbox entries if sending fails. Its internal signed
`POST /internal/task-intelligence/recurrence` supplies the receipt ID and account
generation; user assertions cannot invoke it. Completed, foreign, stale and
erased receipts cannot create fresh work. Queue failures preserve the inbox;
the existing DLQ can capture/replay the new task_recurrence job kind. Export and
account deletion include owned receipt data. See the [recurrence verification](../../../../dev/unified-main/implementation-2026-09-05/canonical-recurrence-2026-09-08.md). Native new intake, content
correction and review acceptance now carry the required-normalization marker.
Other intake families and the complete default-read policy must be qualified before enabling automatic
consolidation. See the [verification record](../../../../dev/unified-main/implementation-2026-09-05/memory-consolidation-apply-2026-09-07.md).

Review feedback immediately follows the guarded item UPDATE in the same batch.
A target deleted concurrently returns 503 with no new feedback or journal
entry. After commit, the existing Queue hint wakes vector projection; Queue
failure retains durable outbox work for scheduled reconciliation.

`memory_privacy_receipts.py` computes the original uid/item HMAC with a dedicated
`MEMORY_PRIVACY_SECRET`, shared only with Jobs. All Core creators persist this
storage-only key. Migration 0174 admits null content only in a deleted tombstone,
preserves the preceding row/schema dependencies, and rejects writers against
unexpired deletion receipts at transaction time. Sealed identities cannot be
renamed or have their key replaced. Unrelated legacy rows need no backfill;
fresh creation always supplies a key. Export excludes it. Canonical privacy
apply still owns lineage scrubbing, receipt sealing and finalization; the
receipt infrastructure alone is not public deletion authority.

`memory_privacy_apply.py` now prepares that canonical deletion in D1. Migration
0175 adds the complete-lineage SQL view, a transaction-only admission guard and
content-free retry inventory. It checks the authoritative account/head, item
revision/metadata and the entire lineage again at commit, including incoming
aliases from creators that have not yet joined the canonical journal. The
original scrubbers clear item semantics, the transaction rotates the privacy
head, and HMAC receipts seal only after row/journal/outbox writes. A late failure
rolls the whole preparation back. No provider deletion or success acknowledgement
is implied by returning the inventory.

The new server-owned legal-hold and destructive-operation gate tables follow
`backend/database/legal_holds.py`: absent legacy hold state permits acquisition;
an active trusted hold blocks it, and another live owner excludes acquisition
for six hours. Hold placement and acquisition contend in the same D1 authority.
Payment locks and writer-transition pauses do not block an admitted privacy
scrub. These tables are currently integrated only into canonical memory
deletion, not every Cloudflare destructive workflow.

`memory_privacy_delete.py` connects native single/batch/all/default, MCP and
Developer DELETE routes to this owner. A pending provider returns 503 with
`memory_cleanup_pending` and `Retry-After: 2`; 200 acknowledges completed erasure.
The durable inventory and existing Jobs Queue/cron resume cleanup without
requiring another client request. Delete-all/default has a durable parent scope
across batches of at most 100 items; default retains Archive even when linked.
The original Developer paid-lock admission remains 402. An owned unexpired
receipt makes a retry after successful finalization idempotent; unknown IDs
still return 404.

`memory_privacy_routes.py` exposes only request-bound internal Jobs continuations
over the existing `API_CORE` service binding. Clients cannot choose target
revisions, operation tokens or legal-hold authority. Jobs renews the gate through
Core before provider IO, retracts vectors through their observed revision, then
calls `memory_privacy_finalize.py`. Migration 0176 checks tombstone identity,
gate/hold state and absence of every artifact/serving mapping inside the final
D1 batch. Referencing operations, commits, outbox, reviews, Archive/lifecycle
history and memory usage-source identities are removed with the physical rows.
Original source conversations/import artifacts remain. Removing usage-source
rows also removes those memories from the derived historical usage counts.
Pending inventory fences late writers even beyond receipt expiry. Only the
30-day opaque receipts remain after finalization. These checks do not establish
complete canonical writer convergence or production deployment qualification.

`cf_memories` remains the item authority. Its existing physical columns hold
content, evidence and lifecycle fields; `canonical_metadata_json` holds only
the remaining upstream model fields. Bounded JSON inserts preserve the existing
100-item limit without one D1 query per item. Cloudflare batch intake is capped
at 1,000,000 UTF-8 request bytes. Edge bounds the body before forwarding it to
the Python ASGI bridge; Core independently enforces the same limit. Oversized
requests return 413 with `max_bytes` and `max_memories`, before any write. The native
parser releases the raw body before apply, and operation transport omits its
duplicate text. The operation INSERT restores that exact text from the item
inserted earlier in the same guarded batch; a missing owned item aborts instead
of persisting an incomplete receipt. Upstream receipt ID/digest validation
checks the stored result. Native
intake retains Short-term admission and stages the original TTL policy.
Preparation keeps only operation IDs/digests for replay lookup, then materializes
and serializes one item at a time into bounded D1 bindings. It does not retain
the entire batch of typed operations and materialized item dictionaries.
Operations and commits are included in the owner's `memory_ledger_data` export;
all five added tables participate in account erasure and deletion write fences.

This is the native intake adapter, not complete ledger authority. Other intake
families, non-native edits, source deletion, consolidation and graph projections
and projection consumers must still converge before history/revert or JIT can
expose a complete canonical head. The existing vector publication outbox still
drives Jobs; a pending kernel outbox event is not a delivered index watermark.

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
Edge registers all eight screenshot API slots and the separate image-content
capability route. Owner endpoints use the existing verified-session, cutover
admission and signed Core context. Adjudication uses the upstream
`screenshots:adjudicate` limit of 30 requests per account per hour before body
forwarding. Shared sets and image reads preserve the URL while dropping caller
credentials, claimed identity and cache validators; Core/the writer recheck
current share state or the image capability. Responses stream through Edge with
the Core privacy cache headers. The boundary tests run in the normal Workers suite.
The upstream transport digest checks, capture window and request fingerprint are
staged without rewriting their behavior. Every candidate digest is checked before
the first model call; binary bytes are decoded again per candidate instead of
retaining a second whole batch. Codec and judge failures reject that candidate;
writer failures return 503 without a completed attempt. Global egress remains
default off. It requires the explicit flag, isolated signer, AI/Images bindings and
successful writer readiness before a candidate can leave Core.

`screen_frame_image.py` delegates source JPEG/PNG decoding, EXIF orientation and
bounded resizing to the native Images binding. Core reads headers without loading
the source pixel plane. `screen_frame_png.py` removes metadata and alpha before
that call, preserving the upstream discard-alpha RGB behavior, rather than the
white compositing used by frame-request uploads. It streams filtered PNG rows,
including 8/16-bit gray/RGB alpha and Adam7 passes, without unfiltering or allocating
a source-sized canvas. The bounded PNG result is checked and stripped again, then
the unchanged upstream canonicalizer creates the 1600-pixel quality-82 JPEG,
480-pixel quality-75 thumbnail and exact-byte digest. No transform occurs on reads.

`screen_frame_judge.py` sends the unchanged upstream privacy prompt and judgement
schema through `AI.run('@cf/qwen/qwen3.8-27b', ...)`. The Cloudflare target selects
this vision model; the upstream purpose rules and default prompt retain their
original source. The request uses a text part plus a JPEG data URL in `messages`
and the original model's JSON schema in `response_format`, following the
[Cloudflare Qwen protocol](https://developers.cloudflare.com/workers-ai/models/qwen3.8-27b/).
Only a single completed assistant choice supplies the verdict. Refusals, tool
calls, reasoning-only, contradictory or malformed output never authorize storage.
Only the judged canonical JPEG and its derived thumbnail/metadata enter a signed
write approval. Core and the isolated writer bind that approval to the exact Qwen
model identity. Usage uses the existing `screen_frame_judge` feature in the D1
LLM ledger; completion tokens already include reasoning tokens and are counted once.

`screen_frame_adjudication_store.py` owns the 24-hour attempt reservation and
replay response. A D1 batch acquires the attempt response against the current
revision/epoch/admission snapshot, publishes the selected survivors, and marks
newly written evictions for cleanup. Any frame-publication trigger failure rolls
back the response too. Concurrent new attempts merge against a fresh snapshot;
privacy epoch changes cancel the whole pending publication. An all-rejected pass
sets `adjudicated_at` without incrementing revision, matching the upstream rule.
The storage worker expires attempt rows in bounded batches.

The [hosted Qwen verification](../../../../dev/unified-main/implementation-2026-09-05/screen-frame-qwen-2026-09-08.md)
runs the normal Core and isolated writer with real Images/Qwen/D1/R2. A two-image
submission stores the meeting presentation and omits the synthetic credential
image. It proves replay without repeated inference, exact receipt-to-content
digests, separate owner views, sharing/settings revocation and deletion of read
access. The subsequent [public screenshot run](../../../../dev/unified-main/implementation-2026-09-05/screen-frame-public-2026-09-08.md)
uses actual Auth signup/session/JWT through Edge, verifies the original quota,
logout and the five-minute writer's automatic R2 erasure. Completed conversations
and images are synthetic. Native capture, queued account erasure, the complete
multi-candidate request envelope and two-target qualification remain outstanding;
the eight inventory slots remain blocked. Earlier native hosted tests prove single
64-megapixel RGB/RGBA images, a 20 MiB file (including its JSON transport), dense
RGB/RGBA inputs and eight 64-megapixel RGBA candidates. They do not prove eight simultaneous
20 MiB candidates or the complete adjudication/D1/R2/model workflow. See the
[codec verification](../../../../dev/unified-main/implementation-2026-09-05/screen-image-hosted-2026-09-06.md).

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

`memory_review_routes.py` projects `cf_memory_review_queue` against the original
canonical source contract: current commit, item revision, content hash and
`promotion.route = review`. Timestamp-based historical rows cannot authorize a
mutation and become redacted stale reviews when read. Native create/batch no
longer infers review authority from a structural conflict. Canonical
consolidation supplies the review decision; its D1 adapter now creates the exact
source-bound queue record in the route transaction. The scheduled model producer
remains part of the unfinished consolidation convergence.

`memory_review_store.py` supplies a typed transaction participant to ordinary
apply and privacy preparation. Migration 0178 rechecks the exact source and
pending queue state inside the appropriate account/item transaction, then
requires a redacted decision with the committed control head before completion.
Accept/correct follow `resolve_canonical_memory_review`: return the candidate
to pending Short-term, retain conflict memories and audit `target_fact_id`
without changing that other item. Correction merges arguments and uses the
original promotion-reset policy. No acceptance grants graph/promotion admission.

Reject and low-veracity timeout/drop use canonical privacy deletion, including
lineages, legal holds, provider observation and history erasure. A valid source
may be rejected while payment-locked. A lock or other edit that changed its
source revision makes an older review stale. Pending provider cleanup returns
503 `memory_cleanup_pending` and `Retry-After: 2`; retry and Jobs use the same
durable inventory. No completed response precedes final erasure. Privacy
finalization removes the derived review row, so later GET/resolve returns 404,
as upstream purge does. Accepted/corrected retries return `already_resolved`
without another mutation. Resolutions retain no candidate text, source hash or
correction payload in the queue; fixed empty storage sentinels support its
existing NOT NULL schema and project as null on the wire.

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


`developer_ask_routes.py` owns the read-only Developer question/answer contract.
`developer_ask_context.py` combines tenant-namespaced summary/transcript
Vectorize candidates with current D1 conversation and memory admission. Key,
scope and source snapshots are checked before model disclosure and before
returning the answer. Its dedicated `WORKERS_AI_DEVELOPER_ASK_MODEL` defaults
to the native Llama 3.3 70B FP8 fast model; unresolved or missing numeric
citations withhold the output without a second inference call. Workers AI
usage reaches the existing chat feature
counter; questions, answers and new business records are not persisted.
The ordinary source projector imports the upstream RAG prompt unchanged and
uses a request-local fact adapter. `worker_timezone.py` loads the pinned pytz
TZif data for the original validator because hosted Python lacks OS zoneinfo.
The [public contract](../../../../docs/doc/developer/ForkCloudflareDeveloperAsk.mdx)
and existing route CI lane describe the full boundary and verification.

`share_recipient_routes.py` owns the read-only calendar recipient proposal. Its
staged upstream contract retains all five attributed calendar sources, owner
exclusion, named-recipient requirements, five-recipient cap and ten-attendee gate.
The route uses a request-bound Edge identity and an Auth-signed profile lookup.
Only D1 calendar fields are hydrated, and current locks, account deletion and
calendar identity are checked again after Auth yields. Missing owner email uses
the original suppression policy and shared fallback telemetry. This read creates
no data or email side effects; the separate share-email send is still blocked.

`share_email_routes.py` exposes only signed internal preparation, claim and finish
operations to Jobs. `share_email_store.py` owns the atomic D1 transaction and
original-day quota refund. The 0171 migration maintains a conversation write
revision so failure rollback cannot overwrite another actor's same-value share.
The recipient ledger distinguishes active claims from sent/ambiguous records;
terminal attempts cannot transition back to dispatching. Claim admission rechecks
locks, account deletion, visibility and the prepared conversation revision.
The current public Edge send route remains blocked pending native email provider
qualification. Jobs holds that binding; Core performs no provider send. The
`cf_share_email_receipts` view exports status metadata without the transient
message payload or leases.

Canonical consolidation stages `backend/fork/consolidation_admission.py` as
`memory_kernel_duplicate_admission.py`. After authoritative D1 hydration, it
rejects a model's `promote/create` for a known same-subject, same-owner,
byte-identical full-content long-term candidate before the batch is mutated.
It ignores vector scores and never chooses a replacement route. The Server
uses the identical rule through its original gather/validator/retry boundary.
Unknown legacy candidate attribution is not treated as proof of identity.

## Candidate lifecycle integration in progress

`candidate_kernel_sources.py` stages original wire models, identities, Suggested
projection and staged-task policy. `candidate_db.py` guards exact observed JSON
and physical rows, account generation and deletion state. Creation/coalescing,
terminal resolution and task/workstream acceptance commit atomically. Acceptance
also saves integration work and vector projection work; a workstream includes its
anchor task and original first event. Goal/workstream snapshots prevent stale
links, and task-change storage preserves explicit null versus omitted fields.

`candidate_routes.py` now owns the public list/get/create/accept/reject/expire
handlers. Suggested uses original confidence, evidence, semantic suppression and
five-item limits. `candidate_attention.py` writes original intervention/feedback
records to their existing D1 tables. Feedback plus attention overrides commit in
one batch; retry retains the original expiry. The old physical request-fingerprint
unique index includes record identity so two distinct idempotency keys do not
incorrectly merge identical bodies. Original request hashes remain internal
payload metadata. Subsecond expiry is checked against the retained JSON timestamp.

`staged_candidate_routes.py` replaces the old staged writers. New intake,
compatibility scores and explicit decisions use Candidates. Reads merge current
canonical proposals and active historical `cf_task_candidates` rows without
materializing history. Terminal canonical evidence suppresses historical duplicates
even if cleanup fails. Cleanup follows the canonical decision and is generation
fenced; acceptance retries cannot create a second task. Wire shapes, score order,
0.5 confidence and unknown ownership follow the projected upstream functions.

Goal creation records account generation; canonical creation rechecks it inside
its receipt batch. User export includes Candidates, interventions, feedback and
attention overrides, retaining historical payloads without internal index/retry
metadata. The Jobs deletion owner includes all six new owner/guard tables.

`recommendation_sources.py` also projects the original WMNow evaluation and debug
control flow, eligibility, shortlist balancing, material identity and message
constructor. Only storage/provider calls become async. `recommendation_state.py`
reads canonical Candidates, tasks, goals, workstreams, artifacts and events from
D1. The active attention override set participates in the original material
version, so feedback invalidates both Suggested and WMNow consistently.

`recommendation_store.py` publishes the current head, decision history,
interventions, job completion, model receipt and successful-publication usage in
one guarded D1 batch. Migration 0185 adds the current head and exact job snapshots.
A job identifies an execution against the observed head; returning to earlier
material after suppression expires creates a fresh execution, while retries of
the same execution share its three-attempt budget and 300-second lease. The old
physical request-fingerprint index also uses execution identity. A third expired
lease becomes terminal without another model call. Queue/Cron retries rehydrate
current canonical state; retired fixed-input jobs are terminally rejected.
Deletion fences cover both old and new owners, and Jobs purges the new head.

`recommendation_llm.py` sends unchanged upstream system/user text and the original
JudgmentOutput schema through Workers AI. `WORKERS_AI_TASK_INTELLIGENCE_MODEL`
defaults to `@cf/qwen/qwen3.8-27b`; an override must support Chat Completions with
JSON Schema output. The original StableId model-version field receives a digest;
receipts and usage retain the exact model ID. Input/output limits are 110,000 /
256,000 UTF-8 bytes, with 8,192 completion tokens and a 180-second timeout. Empty
shortlists make no inference call. Failed inference/publication cannot fabricate
an empty recommendation. Usage counts committed successes, not all provider-billed
calls whose later publication failed.

`recommendation_snapshots.py` now owns device snapshot writes, receipts, reads
and expiry. The public handlers parse original NormalizedContextSnapshot and
OpenLoopSnapshot models. Open loops do not require an invented snapshot_id.
The unchanged upstream window validator enforces future expiry, a one-hour TTL
and at most five minutes of future clock skew. Open-loop admission requires the
same owner's open canonical workstream, rechecked in the D1 commit.

Migration 0186 preserves the original physical payloads while replacing the
single-row-per-device uniqueness with generation/device scope for context and
generation/device/runtime/workstream scope for open loops. Request receipts keep
the first response across newer replacements. Old timestamps and changed content
at an existing timestamp/key conflict; snapshots and receipts publish atomically.
Readers retract expired state and its receipt under the same exact snapshot guard,
so concurrent replacement survives cleanup. At most 50 expired receipts are
removed per pass. Export returns the business payload without storage scope/hash
fields; account deletion purges the added receipt table.

The actual workerd migration/R2 test caught an expression-depth failure when
0186 rebuilt a table: schema revalidation rejected the long 0183 task predicate.
Its 33 null-safe comparisons are now grouped with balanced conjunctions. No
comparison is removed and no runtime limit is changed. The existing actual
workerd suite passes the full migration chain and R2 write/read/revocation path.
Snapshot HTTP semantics remain locally verified with controlled AI, not hosted
Python Worker/client acceptance. See the [snapshot evidence](../../../../dev/unified-main/implementation-2026-09-05/canonical-device-snapshots-2026-09-08.md).

`recommendation_outcomes.py` now owns `/v1/task-intelligence/outcomes`. The
ordinary projector copies upstream `_outcome_matches_chain` and replaces only
its four storage reads with async adapters. Candidate results, task/workstream
links and artifact membership determine allowed subjects; outcome codes must
match their original subject kind. This is attribution of a client-reported
result, not a task completion or artifact approval command.

Migration 0187 checks the exact intervention/feedback and relationship snapshots
alongside generation and the outcome receipt in one D1 batch. Concurrent source
changes retry the complete relationship check. Original request-derived identity
and first server timestamp survive retries; identical bodies under distinct keys
remain distinct events despite the legacy physical uniqueness index. Historical
request-only receipts retain their bytes and identity. Owner export now includes
outcomes without internal hashes; existing account deletion purges them.

`candidate_integrations.py` now owns accepted-task integration leases and settlement.
Acceptance commits the original task/outbox first, then claims and sends a JOBS
message. The public drain returns the count actually queued, with the original
generation header and 1–500 limit. Lost hints remain in D1; the existing Cron
reclaims expired 300-second leases. A prepare transaction permits one external
invocation per lease. Concurrent or replayed messages cannot start it twice.
The original queue policy is projected unchanged: five failed attempts with
30-second exponential backoff capped at 1,800 seconds. Cron runs every five
minutes, so readiness is a lower bound, not an exact dispatch time.

Jobs calls the signed `/internal/candidates/integrations` endpoint for scheduling,
preparation and settlement. Assertions bind uid, internal authority, audience,
method and the encoded path. Cloud service success commits task export metadata
and the outbox receipt together under existing Candidate snapshot/deletion guards.
Apple preparation marks sync_requested and uses the original payload/tag builder;
push success completes delivery but device sync-batch confirmation owns exported.
No default prompt, model, schema or external task data is changed by deployment.

`candidate_control_routes.py` projects the unchanged upstream universal rollout
from the same D1 generation/deletion owner used by Candidate writes. The ordinary
projector stages `candidate_kernel_rollout.py` without changing its policy.
Healthy authenticated accounts receive read/Chat-first capability and their
current generation; unmigrated accounts use zero without creating metadata.
Unavailable or erasing accounts retain the original off/zero/false response with
bounded fallback telemetry. Client headers and historical UI flags are not
authority. No additional control table, rollout gate or prompt change is added.

Actual hosted Core/Jobs Candidate/control and no-default integration delivery
have passed through the unmodified ASGI/Queue entrypoints. Remaining legacy
writers/readers, live providers/devices and release qualification are unfinished.
See the [control verification](../../../../dev/unified-main/implementation-2026-09-05/canonical-control-2026-09-08.md).
See the [integration evidence](../../../../dev/unified-main/implementation-2026-09-05/canonical-integrations-2026-09-08.md).
Migrations 0183–0188 are local drafts. See the
[Candidate evidence](../../../../dev/unified-main/implementation-2026-09-05/canonical-candidates-2026-09-08.md)
and [recommendation evidence](../../../../dev/unified-main/implementation-2026-09-05/canonical-recommendations-2026-09-08.md).

Outcome verification is recorded in the [outcome evidence note](../../../../dev/unified-main/implementation-2026-09-05/canonical-outcomes-2026-09-08.md).


Route composition is verified by `tests/test_candidate_entry.py` against the
actual `entry.app`, including its request-bound authentication middleware.
Every router import uses its own domain name. The runtime registry guard rejects
duplicate method/path mounts, and HTTP tests reach both the Candidate integration
processor and original app-integration owner. Module-only FastAPI fixtures remain
useful for domain tests but do not prove production mounting. This guard catches
the c66e9e3 alias collision, which left nine duplicate mounts and an unreachable
internal integration endpoint despite passing module tests. See the
[entry verification](../../../../dev/unified-main/implementation-2026-09-05/canonical-integration-entry-2026-09-08.md).


`jit_proactivity_routes.py` supplies the original content-free paid-work
reservation wire contract. `jit_proactivity_sources.py` stages the unchanged
receipt models and trigger compiler, and projects the upstream reservation
transaction with only storage reads/writes relocated to async D1 adapters.
Default prompts and per-day/per-candidate limits are unchanged.

Migration 0189 adds event, daily-budget, budget-window and candidate-turn state
to the existing Candidate transaction owner. Exact record snapshots, account
generation and deletion fences protect all writes. A supplemental guard checks
current rollout/kill flags, memory control, timezone and trigger revision plus
metadata in the same batch. Replayed receipts also commit their read guards;
an old successful receipt cannot authorize work after a concurrent revocation.
Snapshot conflicts retry the entire read and policy evaluation at most five
times. No provider call or notification send occurs in this endpoint.

Timezone authority uses the existing latest FCM device registration for this
Cloudflare target, ordered by updated_at and device_key, as in its daily summary
owner. The original policy rejects missing/invalid zones and changes splitting
an active budget window. ZoneInfo uses tzdata 2024.1, matching the upstream pin,
including local-midnight DST boundaries. Client reservation bodies cannot choose
their own timezone. Edge applies the existing agent:execute_tool rate policy.
User export includes owned JIT business records; account erasure includes all
four state families and the transient guard.

The actual Core entrypoint and migration 0189 passed hosted Python Worker/D1
verification: concurrent daily budgets, replay, parent ownership, trigger
revocation and complete rollback after a failed final write. All disposable
resources were removed. Local Core regression passes 1170 tests; the original
upstream reservation store passes 21. See the
[execution evidence](../../../../dev/unified-main/implementation-2026-09-05/jit-reservations-2026-09-08.md).
This initial fixture uses synthetic signed principals and seeded trigger
authority. The subsequent public snapshot run below also verifies reservations
through actual Auth/Edge. The shared two-target reservation contract, queued
erasure and the native trigger/watchlist workflow remain unqualified; the route
manifest retains its blocked classification until its completion gate is satisfied.

`jit_trigger_snapshot_routes.py` supplies the desktop's exhaustive trigger
watchlist. `jit_snapshot_sources.py` stages the original snapshot reader,
revision hashing, model envelopes and response projection, replacing only
Firestore IO with the D1 store. Account generation comes independently from
`cf_account_cutover`; canonical control and its physical head/sequence must
agree with it. Missing control with existing memory is not certified empty.
The original 500-row maximum, active-row validation, action and snooze policy,
whole-snapshot failure and final uncached rollout check are preserved.

The store rechecks both the canonical head and the exact bounded trigger rows
after scanning. This also catches existing writer families that mutate rows
without advancing the head. No partial, invalid or concurrently revoked
watchlist is released as complete. The route uses existing authentication and
no-store responses; it performs no memory mutation or provider call. Its
manifest classification remains blocked pending the family completion gate.
The [public execution record](../../../../dev/unified-main/implementation-2026-09-05/jit-trigger-snapshot-2026-09-08.md)
proves real signup/session/JWT, authenticated snapshot/reservation, 500/501-row
behavior, privacy revocation and logout through the ordinary four Workers and
hosted D1. It uses seeded trigger fixtures; all test resources were removed.

`jit_trigger_feedback_routes.py` mounts the original content-free user feedback
contract. `jit_feedback_sources.py` stages upstream feedback rules and the
canonical adapter with only its IO calls relocated. `FeedbackStore` reuses
`CandidateTransaction` for the notification and immutable receipt read set;
the typed `CanonicalTriggerFeedback` participant joins the existing memory
mutation owner. Item revision, operation digest, head, graph/outbox and both
receipt links commit atomically. Hidden-target replay and owner export use the
same current authority; rollout does not gate explicit user feedback.

Migration 0190 extends the existing Candidate guard and adds receipt storage.
Deletion ownership is in the ordinary Jobs residual registry. The actual-entry
tests cover all actions, killed/disabled rollout, stale/reused identities,
concurrent replay, final-write rollback and the bounded local feedback window.
The shared migration-backed test database enforces D1's 50-byte LIKE/GLOB
pattern limit. Receipt identity uses exact prefix equality; a full feedback
digest must not be embedded in a LIKE pattern.
This route remains in the required JIT family completion gate until common
two-target, native and queued-erasure qualification is complete. See the
[execution record](../../../../dev/unified-main/implementation-2026-09-05/jit-trigger-feedback-2026-09-08.md).

### Explicit ledger restore

`memory_revert_routes.py` exposes authenticated `POST /v3/memories/{memory_id}/revert`.
Edge retains the upstream `memories:modify` rate (120/hour). The UUID body, append/
close chain policy, exact retry recognition, standalone reopen, provenance, row
identity, slot validation, wire projection and apply kernel come from upstream.
`memory_revert_sources.py` changes their synchronous storage calls to awaited D1
participants. The server process prompt-cache invalidation is omitted because
Core has no such cache; the actual canonical head and existing outbox advance.
No model or default prompt is involved.

`memory_revert_store.py` commits through the existing canonical apply guard,
`cf_memories`, journal, graph, outbox and control. Migration 0191 adds a read-only
closed-lineage participant; it does not relax active-item write admission. Every
observed physical field is checked inside the same batch. A source-keyed
`cf_memory_ledger_reopens` receipt prevents two different operation UUIDs from
reopening one standalone source. Same-operation races reload the original policy;
retired operations are conflicts, not new restores. One record above 1,000,000
encoded bytes or aggregate reads above 4,000,000 bytes returns 503 before writing.
Original 64-link traversal remains intact.

New rows receive the existing keyed privacy identity. Reopen receipts join owned
export, canonical privacy finalization and account-deletion residual cleanup;
the transient read participant is always removed before commit. Deleting a source
or replacement purges its reopen receipt as upstream does, without inventing a
new supersession link on the standalone closed source. Existing writer convergence,
common two-target acceptance and production/native qualification are separate work.
