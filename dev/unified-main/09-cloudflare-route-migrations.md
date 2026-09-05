# Cloudflare route migration ledger

Base: `b9776fac12f6098ae5eed0474e42843ff507a693` (main source `d238a85af9`). This fork-owned ledger tracks CF-4 work discovered by CF-1. It is not a list of permanently unsupported product features. All entries below remain required work for the unified target objective.

CF3 additionally identifies the deployment-prefix contract as CF-4 work: API,
MCP, share and object bases with mount paths are rejected by resource qualification
until Edge has one proven prefix-routing contract. Owner: CF adapter, coordinated
with CLIENT-1 URL consumers and AUTH-1 MCP OAuth issuer/redirect handling. Required
acceptance includes prefixed HTTP/WS routes, protected-resource discovery, OAuth
redirects and share/object URLs. This is independent of the route-count ledger;
it does not retire routes or reduce the dual-target objective.

CF-1 now compares the actual FastAPI HTTP/WebSocket registry to the reviewed inventory. After the desktop/admin slots, daily-write, CSAT, calendar capture-gap and lifecycle opt-out implementations, the inventory has 619 unique method/path/protocol slots: 583 have Worker owners and 36 remain blocked pending the contracts below. Duplicate upstream registrations of one slot are collapsed; this guard does not change upstream first-match routing policy. The stale upstream inventory entry `GET /v1/crisp/unread` is removed; the separate CF route manifest can still inventory explicitly registered CF-only extensions.

`GET /v2/desktop/prompts` is implemented in API Core using `cf_desktop_prompts` and the upstream audience/spec contract, with an authenticated Edge route. The remaining families were compared with the source references below; no complete CF implementation exists. A prefix proxy or same-named storage projection is not proof of availability.

`migration_state=blocked` with `target_runtime=blocked` is required until the named owner passes behavioral coverage; `migration_note` and `follow_up` make each debt explicit. This status is not a deployment profile capability and must not be used to claim the full CF target is ready. CF-4 must wire declared capabilities and client behavior before release. Do not route a D1-owned family to PostgreSQL just because `ORIGIN_BACKEND_URL` exists: choose and prove one state owner for the whole family.

## Completion gate

For every family, port the production success and main failure path, preserve the upstream wire/security contract, prove storage/queue/privacy ownership through a controllable runtime seam, add runtime migration/rollback evidence where needed, and only then change its inventory classification and route manifest together. Run `npm run validate:backend-routes`, `npm run validate:manifest`, TypeScript/Python tests and the two-target contract suite. AUTH-1, CLIENT-1 and the privacy/lineage owner must review their respective boundaries.

<a id="cf4-capture-privacy"></a>
## CF-4: capture-privacy

Owner: `api-core`. Upstream authority: `backend/routers/screen_frames.py`.

Opt-in adjudication, immutable screenshot receipts, per-conversation sharing and owner/delete policy must move together.

| Method | Path |
|---|---|
| DELETE | `/v1/conversations/{conversation_id}/screenshots` |
| DELETE | `/v1/conversations/{conversation_id}/screenshots/{frame_id}` |
| GET | `/v1/conversations/{conversation_id}/screenshots` |
| GET | `/v1/conversations/{conversation_id}/shared/screenshots` |
| GET | `/v1/screen-frame-egress/settings` |
| PATCH | `/v1/conversations/{conversation_id}/screenshot-sharing` |
| PATCH | `/v1/screen-frame-egress/settings` |
| POST | `/v1/screen-frame-egress/adjudications` |

<a id="cf4-frame-requests"></a>
## CF-4: frame-requests

Owner: `api-core`. Upstream authority: `backend/routers/frame_requests.py`.

Temporary frame request lifecycle, consent/promotion, R2 image bytes and retention cleanup require one authority.

| Method | Path |
|---|---|
| GET | `/v1/conversations/{conversation_id}/photos/{photo_id}/image` |
| GET | `/v1/frame-requests/pending` |
| GET | `/v1/frame-requests/status/{request_id}` |
| GET | `/v1/frame-requests/temporary/{request_id}/image` |
| POST | `/v1/frame-requests` |
| POST | `/v1/frame-requests/{request_id}/promote` |
| POST | `/v1/frame-requests/{request_id}/state` |
| POST | `/v1/frame-requests/{request_id}/upload` |

<a id="cf4-share-email"></a>
## CF-4: share-email

Owner: `jobs`. Upstream authority: `backend/routers/conversations.py`.

Recipient claims, quota, share publication and ambiguous-delivery idempotency are not in the CF conversation projection.

| Method | Path |
|---|---|
| GET | `/v1/conversations/{conversation_id}/share-recipients` |
| POST | `/v1/conversations/{conversation_id}/share-email` |

<a id="cf4-email-preferences"></a>
## CF-4: email-preferences

Owner: `api-core`. Upstream authority: `backend/routers/email_preferences.py`.

Implemented with the upstream canonical lifecycle HMAC, scanner-safe GET, branded confirmation and idempotent POST. Core owns the consent row and verifies account existence through Auth; Jobs uses its existing deletion registry to purge it. An independent issuer secret is required in the resource inventory. Provider/identity/store failures and deleted accounts have neutral responses. Real local HTTP/export/queue evidence is recorded in [the verification journal](implementation-2026-09-05/eddy-email-preferences-2026-09-06.md). Lifecycle mail sending remains a separate operation; these endpoint tests do not claim delivery.

| Method | Path |
|---|---|
| GET | `/email/unsubscribe` |
| POST | `/email/unsubscribe` |

<a id="cf4-referrals"></a>
## CF-4: referrals

Owner: `api-core`. Upstream authority: `backend/routers/referrals.py`.

Referral cookie/codes, account-age admission and exactly-once trial entitlement need shared Auth and billing state.

| Method | Path |
|---|---|
| GET | `/r/{code}` |
| GET | `/v1/users/me/referral` |
| POST | `/v1/users/me/referral/claim` |

<a id="cf4-calendar-capture-gaps"></a>
## CF-4: calendar-capture-gaps

Owner: `jobs`. Upstream authority: `backend/routers/google_calendar.py`.

Implemented by the existing Jobs Google Calendar grant/refresh owner and a uid-scoped, indexed D1 recording range. The route keeps upstream's 31-day window, 250-event / 500-conversation ceilings, 24-hour lookback, ten-second overlap floor and discarded/declined/cancelled/tentative/all-day exclusions. It never creates or mutates conversations. Worker HTTP query/disconnected checks pass alongside Server OS; positive joins and refresh/failure cases execute the actual Jobs route with real SQLite and controlled provider responses. Hosted Calendar-account execution remains unverified. See [evidence](implementation-2026-09-05/eddy-calendar-capture-gaps-2026-09-06.md).

| Method | Path |
|---|---|
| GET | `/v1/calendar/capture-gaps` |

<a id="cf4-csat"></a>
## CF-4: csat

Owner: `api-core`. Upstream authority: `backend/routers/csat.py`.

Implemented in `csat_routes.py` and migration 0158: normalized product config,
branded default copy, one atomic create-only rating per UID/platform, server-owned
comment policy, export and the existing account-deletion fence/purge. Both routes
are staging-owned; this is not production publication or full CF-4 qualification.

| Method | Path |
|---|---|
| GET | `/v1/csat/config` |
| POST | `/v1/csat/ratings` |

<a id="cf4-jit"></a>
## CF-4: jit

Owner: `api-core`. Upstream authority: `backend/routers/jit_rollout.py; backend/routers/jit_ledger_snapshot.py`.

Rollout/trigger and ledger snapshots, feedback receipts and atomic proactivity reservations have no equivalent CF state.

| Method | Path |
|---|---|
| GET | `/v1/jit/knowledge-ledger/mirror-snapshot` |
| GET | `/v1/jit/knowledge-ledger/prompt-snapshot` |
| GET | `/v1/jit/rollout-decision` |
| GET | `/v1/jit/trigger-snapshot` |
| POST | `/v1/jit/proactivity/reservations` |
| POST | `/v1/jit/trigger-feedback` |

<a id="cf4-memory-ledger"></a>
## CF-4: memory-ledger

Owner: `api-core`. Upstream authority: `backend/routers/memories.py`.

Ledger history/revert require canonical memory lineage, revision/privacy authority and outbox; flat D1 projection is insufficient.

| Method | Path |
|---|---|
| GET | `/v3/memories/ledger-history` |
| POST | `/v3/memories/{memory_id}/revert` |

<a id="cf4-developer-ask"></a>
## CF-4: developer-ask

Owner: `api-core`. Upstream authority: `backend/routers/developer.py`.

Developer scope/rate admission plus conversation/transcript retrieval, authoritative lock recheck and cited Workers AI answer must be combined.

| Method | Path |
|---|---|
| POST | `/v1/dev/user/ask` |

## CF-4 Live Model Session

`POST /v2/realtime/session` is a real upstream route (`backend/routers/desktop_realtime.py:142`),
with OpenAI/Gemini ephemeral session token and error contracts. CF-2 retires the
fork-only `POST /v1/realtime/web-ticket` and its HMAC ticket, but retains this
registered route. It now returns a bounded, authenticated 409 capability denial
with `reason`, `provider`, `backend_route`, and `retryable` for the two real
providers; invalid provider is 400. It cannot return an STT ticket as a live model
credential. This correction exposes one existing false ownership claim: the
612-route inventory is now 576 staging-owned + 36 blocked, with no registrations
dropped.

Owner: CF AI/realtime adapter, jointly with CLIENT-1 live-model client owner.
The missing contract is interactive live-model transport, model selection,
quota, usage recording, cancellation and provider failure semantics. A pure CF
implementation must prove an equivalent native provider/client transport before
this route can be marked owned. The target profile's
`allow_direct_model_providers: false` must gate both visible Web controls and
the token-request entry point (CLIENT-1); this is a retained delivery obligation,
not a permanent removal of the user's live-model goal.

<a id="cf4-desktop-daily-writes"></a>
## CF-4: desktop daily writes

Owner: `api-core`. Upstream authority: `backend/routers/users.py` and
`backend/database/daily_summaries.py`. The 2026-09-05 Eddy production preparation
found these actual registrations missing from the inventory. Both now have a
local-runtime-tested Cloudflare owner. Inventory classification is not
production release qualification.

| Method | Path | Status |
|---|---|---|
| POST | `/v1/users/desktop-usage/daily` | Core/D1 staging-owned; public local-runtime contract passed |
| POST | `/v1/users/daily-summaries` | Core/D1/Workers AI staging-owned; public local-runtime contract passed |

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

Owner: `api-core` with `jobs` generation. Upstream authority:
`backend/routers/feedback_admin.py`. The same production preparation discovered
five missing admin registrations:

| Method | Path |
|---|---|
| GET | `/v1/admin/feedback/reports` |
| GET | `/v1/admin/feedback/reports/{report_date}` |
| GET | `/v1/admin/feedback/events/{event_id}/context` |
| POST | `/v1/admin/feedback/reports/{report_date}/generate` |
| POST | `/v1/admin/feedback/reports/generate-yesterday` |

The required boundary joins the admin secret gate and actor attribution with a
feedback event ledger, bounded pointer-only daily reports and authorized
on-demand context hydration. End-user JWTs must not grant access. No CF source
currently implements that complete boundary; all five remain blocked.
