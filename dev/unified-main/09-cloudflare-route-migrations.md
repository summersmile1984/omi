# Cloudflare route migration ledger

Base: `b9776fac12f6098ae5eed0474e42843ff507a693` (main source `d238a85af9`). This fork-owned ledger tracks CF-4 work discovered by CF-1. It is not a list of permanently unsupported product features. All entries below remain required work for the unified target objective.

CF3 additionally identifies the deployment-prefix contract as CF-4 work: API,
MCP, share and object bases with mount paths are rejected by resource qualification
until Edge has one proven prefix-routing contract. Owner: CF adapter, coordinated
with CLIENT-1 URL consumers and AUTH-1 MCP OAuth issuer/redirect handling. Required
acceptance includes prefixed HTTP/WS routes, protected-resource discovery, OAuth
redirects and share/object URLs. This is independent of the route-count ledger;
it does not retire routes or reduce the dual-target objective.

CF-1 now compares the actual FastAPI HTTP/WebSocket registry to the reviewed inventory. After the desktop/admin slots, daily-write, CSAT, calendar capture-gap lifecycle opt-out and referral implementations, the inventory has 619 unique method/path/protocol slots: 602 have Worker owners and 17 remain blocked pending the contracts below. Duplicate upstream registrations of one slot are collapsed; this guard does not change upstream first-match routing policy. The stale upstream inventory entry `GET /v1/crisp/unread` is removed; the separate CF route manifest can still inventory explicitly registered CF-only extensions.

`GET /v2/desktop/prompts` is implemented in API Core using `cf_desktop_prompts` and the upstream audience/spec contract, with an authenticated Edge route. The remaining families were compared with the source references below; no complete CF implementation exists. A prefix proxy or same-named storage projection is not proof of availability.

`migration_state=blocked` with `target_runtime=blocked` is required until the named owner passes behavioral coverage; `migration_note` and `follow_up` make each debt explicit. This status is not a deployment profile capability and must not be used to claim the full CF target is ready. CF-4 must wire declared capabilities and client behavior before release. Do not route a D1-owned family to PostgreSQL just because `ORIGIN_BACKEND_URL` exists: choose and prove one state owner for the whole family.

## Completion gate

For every family, port the production success and main failure path, preserve the upstream wire/security contract, prove storage/queue/privacy ownership through a controllable runtime seam, add runtime migration/rollback evidence where needed, and only then change its inventory classification and route manifest together. Run `npm run validate:backend-routes`, `npm run validate:manifest`, TypeScript/Python tests and the two-target contract suite. AUTH-1, CLIENT-1 and the privacy/lineage owner must review their respective boundaries.

<a id="cf4-capture-privacy"></a>

## CF-4: capture-privacy

Owner: `api-core`. Upstream authority: `backend/routers/screen_frames.py`.

Privacy adjudication, immutable screenshot receipts, per-conversation sharing and owner/delete policy must move together.

The [2026-09-06 Worker prerequisite verification](implementation-2026-09-05/screen-frame-worker-prerequisite-2026-09-06.md)
stages the unchanged upstream image processing, wire types and privacy prompt.
It proves local Pyodide image execution. The subsequent
[storage boundary verification](implementation-2026-09-05/screen-frame-storage-2026-09-06.md)
adds the independent R2 writer, one-use approval verification, revocable content
reads and account erasure. The subsequent
[Core screenshot views verification](implementation-2026-09-05/screen-frame-views-2026-09-06.md)
adds settings, owner/public reads, sharing revocation, concurrent-safe deletion
and the streaming content proxy in Core. The
[Core adjudication verification](implementation-2026-09-05/screen-frame-adjudication-2026-09-06.md)
then implements original-prompt adjudication, approval minting and atomic survivor
publication, with actual local PNG/D1/R2 execution and controlled inference.
These eight route slots remain blocked pending hosted model access, supported-input
memory qualification, Edge routing and full hosted business qualification.

| Method | Path                                                         |
| ------ | ------------------------------------------------------------ |
| DELETE | `/v1/conversations/{conversation_id}/screenshots`            |
| DELETE | `/v1/conversations/{conversation_id}/screenshots/{frame_id}` |
| GET    | `/v1/conversations/{conversation_id}/screenshots`            |
| GET    | `/v1/conversations/{conversation_id}/shared/screenshots`     |
| GET    | `/v1/screen-frame-egress/settings`                           |
| PATCH  | `/v1/conversations/{conversation_id}/screenshot-sharing`     |
| PATCH  | `/v1/screen-frame-egress/settings`                           |
| POST   | `/v1/screen-frame-egress/adjudications`                      |

<a id="cf4-frame-requests"></a>

## CF-4: frame-requests

Owner: `api-core`. Upstream authority: `backend/routers/frame_requests.py`.

Temporary frame request lifecycle, consent/promotion, R2 image bytes and retention cleanup require one authority.

The [2026-09-06 metadata foundation](implementation-2026-09-05/frame-request-metadata-2026-09-06.md)
implements the original wire/pure policy through a D1-owned JIT provider,
request identity, dedupe, bounded delivery and guarded state transitions. Real
local workerd passed 29 HTTP calls; no pixels or model inference participated.
The subsequent [pixel implementation](implementation-2026-09-05/frame-request-pixels-2026-09-06.md)
adds upload, deterministic promotion, private reads and durable multipart cleanup
with separate temporary/permanent buckets. A real local Core/Jobs/R2 flow now
passes. Both production buckets and retention policies have since been
provisioned. The [hosted image repair](implementation-2026-09-05/frame-image-hosted-runtime-2026-09-06.md)
proves the supported file/pixel envelope through the production multipart and
native Images helpers. The [authenticated hosted flow](implementation-2026-09-05/frame-public-hosted-2026-09-06.md)
now verifies actual Edge/Auth/Core/D1/R2 creation, upload, private reads, promotion,
revocation and cleanup with synthetic accounts. These eight slots are staging-owned
with the upstream read/write/upload rate limits. Complete product deployment and
the separate JIT trigger/memory families remain required.

| Method | Path                                                          |
| ------ | ------------------------------------------------------------- |
| GET    | `/v1/conversations/{conversation_id}/photos/{photo_id}/image` |
| GET    | `/v1/frame-requests/pending`                                  |
| GET    | `/v1/frame-requests/status/{request_id}`                      |
| GET    | `/v1/frame-requests/temporary/{request_id}/image`             |
| POST   | `/v1/frame-requests`                                          |
| POST   | `/v1/frame-requests/{request_id}/promote`                     |
| POST   | `/v1/frame-requests/{request_id}/state`                       |
| POST   | `/v1/frame-requests/{request_id}/upload`                      |

<a id="cf4-share-email"></a>

## CF-4: share-email

Owners: `api-core` for recipient suggestions; `jobs` for the remaining send transaction. Upstream authority: `backend/routers/conversations.py`.

Recipient suggestions now use the original calendar-source, named-attendee, owner-exclusion, deduplication and meeting-size rules in API Core. Auth supplies the current owner email; D1 supplies only owned calendar metadata. The read rechecks locks, deletion and calendar state after Auth enrichment. Missing owner email suppresses the proposal with shared telemetry. No email is sent by this read. [The verification record](implementation-2026-09-05/share-recipients-2026-09-06.md) covers the 37 successful hosted HTTP assertions and the remaining send/release obligations.

The POST remains blocked pending the verified Eddy native sender, provider delivery and public Edge qualification. Its internal Jobs/Core transaction now implements D1 recipient claims, daily quota, revision-owned publication/rollback, one-use dispatch, and confirmed/ambiguous delivery receipts. [Unit and hosted transaction evidence](implementation-2026-09-05/share-email-transactions-2026-09-06.md) is recorded separately from actual outbound delivery.

| Method | Path                                                   |
| ------ | ------------------------------------------------------ |
| GET    | `/v1/conversations/{conversation_id}/share-recipients` |
| POST   | `/v1/conversations/{conversation_id}/share-email`      |

<a id="cf4-email-preferences"></a>

## CF-4: email-preferences

Owner: `api-core`. Upstream authority: `backend/routers/email_preferences.py`.

Implemented with the upstream canonical lifecycle HMAC, scanner-safe GET, branded confirmation and idempotent POST. Core owns the consent row and verifies account existence through Auth; Jobs uses its existing deletion registry to purge it. An independent issuer secret is required in the resource inventory. Provider/identity/store failures and deleted accounts have neutral responses. Real local HTTP/export/queue evidence is recorded in [the verification journal](implementation-2026-09-05/eddy-email-preferences-2026-09-06.md). Lifecycle mail sending remains a separate operation; these endpoint tests do not claim delivery.

| Method | Path                 |
| ------ | -------------------- |
| GET    | `/email/unsubscribe` |
| POST   | `/email/unsubscribe` |

<a id="cf4-referrals"></a>

## CF-4: referrals

Owner: `api-core`. Upstream authority: `backend/routers/referrals.py`.

Implemented with the upstream HMAC/cookie contract, signed Auth account age and an immutable D1 claim receipt that grants the subscription atomically. Every entitlement reader sees trial expiry through the shared effective-subscription view; attribution is separately erasable, and export/deletion use their existing owners. Both Server and CF passed the shared public HTTP claim/reread contract. The branded Web signup was exercised in the browser, including visible success when the local download artifact is absent. Independent signing-secret provisioning and hosted/download validation remain release work. See [verification evidence](implementation-2026-09-05/eddy-referrals-2026-09-06.md).

| Method | Path                          |
| ------ | ----------------------------- |
| GET    | `/r/{code}`                   |
| GET    | `/v1/users/me/referral`       |
| POST   | `/v1/users/me/referral/claim` |

<a id="cf4-calendar-capture-gaps"></a>

## CF-4: calendar-capture-gaps

Owner: `jobs`. Upstream authority: `backend/routers/google_calendar.py`.

Implemented by the existing Jobs Google Calendar grant/refresh owner and a uid-scoped, indexed D1 recording range. The route keeps upstream's 31-day window, 250-event / 500-conversation ceilings, 24-hour lookback, ten-second overlap floor and discarded/declined/cancelled/tentative/all-day exclusions. It never creates or mutates conversations. Worker HTTP query/disconnected checks pass alongside Server OS; positive joins and refresh/failure cases execute the actual Jobs route with real SQLite and controlled provider responses. Hosted Calendar-account execution remains unverified. See [evidence](implementation-2026-09-05/eddy-calendar-capture-gaps-2026-09-06.md).

| Method | Path                        |
| ------ | --------------------------- |
| GET    | `/v1/calendar/capture-gaps` |

<a id="cf4-csat"></a>

## CF-4: csat

Owner: `api-core`. Upstream authority: `backend/routers/csat.py`.

Implemented in `csat_routes.py` and migration 0158: normalized product config,
branded default copy, one atomic create-only rating per UID/platform, server-owned
comment policy, export and the existing account-deletion fence/purge. Both routes
are staging-owned; this is not production publication or full CF-4 qualification.

| Method | Path               |
| ------ | ------------------ |
| GET    | `/v1/csat/config`  |
| POST   | `/v1/csat/ratings` |

<a id="cf4-jit"></a>

## CF-4: jit

Owner: `api-core`. Upstream authority: `backend/routers/jit_rollout.py; backend/routers/jit_ledger_snapshot.py`.

The rollout decision is now exposed through authenticated Edge/Core using the existing D1 control owner. It retains the upstream tri-state/allowlist policy, current owner/default flags, dominant global kill switch and no-store response. The shared HTTP contract passes on Server and local Cloudflare; actual hosted Auth/Edge/Core/D1 flag transitions, isolation and logout revocation also pass. See [verification](implementation-2026-09-05/jit-rollout-2026-09-06.md).

The other five trigger/ledger snapshot, feedback and atomic proactivity reservation routes still require canonical CF business state. This decision read does not qualify those routes or enable them through empty responses.

| Method | Path                                       |
| ------ | ------------------------------------------ |
| GET    | `/v1/jit/knowledge-ledger/mirror-snapshot` |
| GET    | `/v1/jit/knowledge-ledger/prompt-snapshot` |
| GET    | `/v1/jit/rollout-decision`                 |
| GET    | `/v1/jit/trigger-snapshot`                 |
| POST   | `/v1/jit/proactivity/reservations`         |
| POST   | `/v1/jit/trigger-feedback`                 |

<a id="cf4-memory-ledger"></a>

## CF-4: memory-ledger

Owner: `api-core`. Upstream authority: `backend/routers/memories.py`.

Ledger history/revert require canonical memory lineage, revision/privacy authority and outbox; flat D1 projection is insufficient.

The ordinary native, MCP and Developer single/batch intake paths now start in
Short-term, matching `INV-MEM-4` and the upstream canonical adapter. Caller
category/durability no longer creates Long-term rows; historical Long-term rows
retain their tier. The shared HTTP contract checks both deployment targets.
Full consolidation with one terminal route and atomic promotion/graph receipts
still needs a Cloudflare owner, alongside append-only ledger producers. This
correction does not make either history/revert endpoint owned or release-ready.
The [intake verification record](implementation-2026-09-05/eddy-memory-intake-2026-09-06.md)
contains the five failing baseline paths and the real dual-target HTTP results.

Migration 0161 now enforces the existing row lock at the shared D1 mutation
boundary for native, MCP, developer and review writers, including locks acquired
after a pre-read. This closes a prerequisite write-admission defect; these two
ledger routes remain blocked until append-only lineage, exact retry identity,
current-tail privacy and index revision semantics are implemented and exercised.

Migration 0162 now commits a monotonic memory `item_revision` and its vector
outbox work atomically for every in-tree producer, including X intake and
conversation-cascade deletion. Jobs acknowledges only the observed revision,
so a same-second edit during embedding retains its new work. Migration 0163
adds immutable external memory-vector IDs, canonical publication comparison,
and an artifact journal that survives in-flight writes and asynchronous erasure.
Production projector regressions now cover reverse completion, stale deletion,
late account-deletion writers and uncertain provider responses. The shared
memory hydration owner now reads canonical content, revision, publication/model
metadata and current access in one snapshot for native, MCP, developer and
chat-tool searches. It reports actual rejection/repair diagnostics and sends
due repair work to the existing Queue owner after commit. Hosted Vectorize
cleanup behavior and equivalent ownership for other projection families still
require qualification; this remains a
prerequisite to qualifying append-only ledger history and revert.

The [native canonical apply-kernel verification](implementation-2026-09-05/memory-kernel-2026-09-06.md)
now stages the unchanged upstream pure models and apply rules into Core. Thirteen
hosted Python Worker scenarios preserve retry IDs, head/generation rejection,
source privacy, restricted projections and promotion/graph receipts. This is an
executable prerequisite, not a D1 commit owner or a history implementation. The
existing writers must still converge on one atomic durable apply boundary before
either ledger route or the remaining JIT snapshots/feedback can be qualified.

| Method | Path                              |
| ------ | --------------------------------- |
| GET    | `/v3/memories/ledger-history`     |
| POST   | `/v3/memories/{memory_id}/revert` |

<a id="cf4-developer-ask"></a>

## CF-4: developer-ask

Owner: `api-core`. Upstream authority: `backend/routers/developer.py`.

Implemented in Core with the original request/response types and unchanged upstream
RAG prompt. Edge applies the per-key 25/hour policy; native BGE-M3 and both
Vectorize indexes supply candidates, and D1 scope/privacy/content checks fence
model disclosure and response release. The Worker loads packaged timezone data
instead of relying on an OS database. See the [contract](../../docs/doc/developer/ForkCloudflareDeveloperAsk.mdx)
and [verification journal](implementation-2026-09-05/developer-ask-2026-09-06.md).
This route does not retire the separate memory-ledger or JIT obligations.

| Method | Path               |
| ------ | ------------------ |
| POST   | `/v1/dev/user/ask` |

## CF-4 Live Model Session

`POST /v2/realtime/session` is a real upstream route (`backend/routers/desktop_realtime.py:142`),
with OpenAI/Gemini ephemeral session token and error contracts. CF-2 retires the
fork-only `POST /v1/realtime/web-ticket` and its HMAC ticket, but retains this
registered route. It now returns a bounded, authenticated 409 capability denial
with `reason`, `provider`, `backend_route`, and `retryable` for the two real
providers; invalid provider is 400. It cannot return an STT ticket as a live model
credential. That correction exposed one existing false ownership claim: at that
point the 612-route inventory became 576 staging-owned + 36 blocked, with no
registrations dropped. The current, expanded inventory totals appear above.

Owner: CF AI/realtime adapter, jointly with CLIENT-1 live-model client owner.
The missing contract is interactive live-model transport, model selection,
quota, usage recording, cancellation and provider failure semantics. A pure CF
implementation must prove an equivalent native provider/client transport before
this route can be marked owned. The target profile's
`allow_direct_model_providers: false` must gate both visible Web controls and
the token-request entry point (CLIENT-1); this is a retained delivery obligation,
not a permanent removal of the user's live-model goal.

The reachable `/v2/realtime/usage` accounting path now uses an immutable
UID/turn receipt and one D1 transaction for quota, daily counters and desktop
cost. Retries retain the first report, cached input is priced as a subset,
missing storage fails, and export/account deletion include the new receipts.
This repairs client-reported accounting; it does not prove a native interactive
provider, observed wire usage, cancellation or the session-mint contract above.

<a id="cf4-desktop-daily-writes"></a>

## CF-4: desktop daily writes

Owner: `api-core`. Upstream authority: `backend/routers/users.py` and
`backend/database/daily_summaries.py`. The 2026-09-05 Eddy production preparation
found these actual registrations missing from the inventory. Both now have a
local-runtime-tested Cloudflare owner. Inventory classification is not
production release qualification.

| Method | Path                            | Status                                                                 |
| ------ | ------------------------------- | ---------------------------------------------------------------------- |
| POST   | `/v1/users/desktop-usage/daily` | Core/D1 staging-owned; public local-runtime contract passed            |
| POST   | `/v1/users/daily-summaries`     | Core/D1/Workers AI staging-owned; public local-runtime contract passed |

`desktop_daily_usage_routes.py` owns one D1 row per UID/device/day and atomically
merges each counter by maximum. It validates strict counter bounds, the local
two-day date window, and IANA timezone, with the upstream 600/hour Edge policy.
The row participates in export and the existing deletion fence/purge. The
2026-09-05 public recording contract exercised concurrent out-of-order writes,
export, and actual Queue-driven account erasure; see
[the evidence](implementation-2026-09-05/eddy-desktop-daily-usage.md).

The on-demand summary now resolves IANA local days, admits quota before lookup,
reuses existing dates, and claims one D1 day lease before reading/generating.
Empty/contentless days do not arm create cooldown; concurrent creation is 409,
successful creation arms 30 seconds, and regeneration reserves its own cooldown
before inference. Workers AI supplies validated prose; D1 supplies tasks, usage,
locations and eligible learned-memory identities. An atomic source/lease check
fences late privacy mutations. Actual local HTTP/Queue/D1 proof includes recap,
reuse, regeneration, export and account erasure. See
[the recap evidence](implementation-2026-09-05/eddy-daily-recap.md).

The upstream scheduled recap and settings-test notification delivery contract
remain required: this package does not qualify push, device-token admission,
local scheduled send time, chat-card delivery, or hosted model quality.

<a id="cf4-feedback-reports"></a>

## CF-4: feedback reports

Owner: `jobs` public admin gate and schedule, with `api-core` report storage
and context hydration. Upstream authority: `backend/routers/feedback_admin.py`.
All five registrations are now staging-owned after the real hosted feedback
flow and behavioral tests described below:

| Method | Path                                                |
| ------ | --------------------------------------------------- |
| GET    | `/v1/admin/feedback/reports`                        |
| GET    | `/v1/admin/feedback/reports/{report_date}`          |
| GET    | `/v1/admin/feedback/events/{event_id}/context`      |
| POST   | `/v1/admin/feedback/reports/{report_date}/generate` |
| POST   | `/v1/admin/feedback/reports/generate-yesterday`     |

The public Jobs boundary checks its existing ADMIN_KEY, records hashed reader
attribution and signs a feedback-specific internal Core assertion. End-user
JWTs cannot enter the report service. All five rating surfaces append upstream
feedback envelopes atomically with their current rating. The upstream policy
owns reason-preserving collapse, UTC dates, raw/entry/byte caps and context limits.
D1 leases fence concurrent report publication; events and pointer entries join
export/erasure ownership and current privacy fences prevent stale republication.

The existing five-minute Jobs schedule ensures yesterday's report after 01:30
UTC. Actual hosted Auth/Edge/Jobs/Python Core/D1 requests proved rating writes,
admin rejection/admission, report generation, precise context hydration and
privacy revocation. These five routes do not remove the independent CF-4/CI-1
production qualification gates. See the
[contract](../../docs/doc/developer/ForkCloudflareFeedback.mdx) and
[verification record](implementation-2026-09-05/feedback-reports-2026-09-06.md).
