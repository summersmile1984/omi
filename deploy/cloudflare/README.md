# Omi Cloudflare Workers

This directory is the Worker-first deployment surface described in
[`dev/cloudflare-adaptation-plan.md`](../../dev/cloudflare-adaptation-plan.md).
It intentionally does not import the monolithic `backend/main.py`.

The first staging slice contains:

- `edge`: public routing, request IDs, trusted auth context and legacy fallback.
- `auth`: Hono + Better Auth + D1, with request-scoped auth construction.
- `api-core`: a minimal FastAPI/Python Worker composition root with a D1 probe,
  uid-scoped R2 asset API (`/v1/cf/assets/{key}`) with checksum and range
  semantics, uid-scoped transcription
  preferences, onboarding/privacy/notification/location-consent settings, and
  the public firmware stable/latest/version APIs. It also exposes staging-only
  D1-backed action-item and canonical memory CRUD surfaces.
- `api-core`: a public firmware stable-release API backed by the GitHub Releases
  API; it keeps firmware metadata outside the Worker filesystem.
- `api-ai`: a minimal FastAPI/Python Worker composition root for provider APIs.
- `realtime`: the Durable Object/ASR protocol seam; no model is run locally.

## Local setup

```bash
npm ci
uvx uv==0.12.3 run pywrangler init
```

The Python projects have their own `pyproject.toml`; run `uvx uv==0.12.3 run pywrangler dev`
from the project directory after installing the Python Worker dependencies. The
deploy script uses the pinned launcher because older globally installed uv
versions are rejected by `pywrangler`.

## Staging resources

Resource names are deliberately isolated from existing account resources:

- D1: `omi-cf-auth-staging`, `omi-cf-app-staging`
- Workers: `omi-cf-edge-staging`, `omi-cf-auth-staging`, `omi-cf-api-core-staging`, `omi-cf-api-ai-staging`, `omi-cf-realtime-staging`
- Jobs Worker: `omi-cf-jobs-staging`
- Queues: `omi-cf-jobs-staging`, `omi-cf-jobs-dlq-staging`
- R2: `omi-cf-staging`

The deploy script only deploys the named staging environment. It applies D1
migrations before Workers and deploys Edge last. It never creates production
resources and never mutates existing Omi Workers.

## Commands

```bash
npm test
npm run typecheck
npm run deploy:staging
npm run smoke:staging
```

### Web Worker staging

The Next.js 16 app has a separate Cloudflare Worker build through vinext. It
uses service bindings for both authenticated API traffic (`EDGE`) and Better
Auth (`AUTH`), so server-side routes never make public Worker-to-Worker
`workers.dev` fetches. Browser WebSockets connect to the public Edge Worker
directly. Staging is compiled in Better Auth email mode; the existing Firebase
client path remains the default for non-staging builds.

```bash
cd web/app
npm ci
npx vinext check                 # 97% compatible; image optimization is the only partial feature
npx tsc --noEmit
npm test
npm run build:vinext:staging
npm run deploy:vinext:staging
```

The Vinext build sets `VINEXT_BUILD=1` so the Cloudflare bundle keeps the real
`cloudflare:workers` module. The ordinary `npm run build` path aliases that
module to a Node-only stub and remains available for the existing Next.js
workflow.

The staging deployment is `omi-web-app-staging` at
`https://omi-web-app-staging.summersmile1984.workers.dev`. The staging build
script pins `NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_WS_BASE_URL`,
`NEXT_PUBLIC_AUTH_MODE=better-auth`, and the Auth Worker URL; production DNS and
production identity are not changed by this command. The `/login` page exposes
email/password sign-up and sign-in, while OAuth remains on the Firebase path
until its identity-linking contract is migrated and qualified.

Better Auth browser sessions are cookie-only: the same-origin auth proxy
forwards `Set-Cookie` but removes the session token from successful sign-in and
sign-up JSON. The API proxy forwards that httpOnly cookie only over the `EDGE`
service binding; its public local-development fallback accepts bearer tokens
and never receives browser cookies. Web recording exchanges the cookie at
`POST /v1/realtime/web-ticket` for a signed 30-second ticket. The browser sends
that ticket as its first WebSocket message, and the isolated Durable Object
claims it once, so the Realtime Worker never receives a long-lived Better Auth
session token or an Auth service binding.

After Edge verifies a client session, it removes the client cookie/bearer and
mints a fresh HMAC assertion for the exact downstream request. Assertions last
at most 60 seconds and bind the uid to one audience (`api-core`, `api-ai`,
`auth`, `jobs`, or `realtime`), HTTP method, and path. Every TypeScript
downstream verifies those claims directly; both Python Workers enforce the same
contract in ASGI middleware whenever internal assertion headers are present.
This prevents a captured assertion for one service or route from being replayed
against another. The explicit legacy fallback is the only path that preserves a
client bearer, because the legacy backend remains its verifier during cutover.

The isolated staging profile has one server-authoritative account/data-plane
binding. On its first authenticated control read, a Better Auth principal is
atomically registered in D1 as a bound `new` account; this is safe only because
the profile cannot contain a historical Firebase account. Edge checks that
control row before Core, AI, Jobs, or Realtime product traffic and fails closed
unless `state=new`, product traffic is allowed, and the destination is bound.
Auth/profile and the control endpoint remain reachable while product traffic is
fenced. Missing rows outside the exact
`ACCOUNT_CUTOVER_PROFILE=isolated-staging` configuration still project as
`legacy`; no existing-account migration or production cutover is inferred.

`deploy:staging` first runs the TypeScript/Python/Web tests and dry-run builds,
then records the active version of all six backend Workers and the Web Worker.
It applies the isolated migrations, publishes backend Workers in dependency
order, verifies Edge `/ready`, deploys the already-qualified Web bundle, checks
Web `/api/worker-ready`, and runs the staging smoke. Edge readiness calls Auth,
Core, AI, Realtime, and Jobs only through Service Bindings. Core, AI, Realtime,
and Jobs have no public `workers.dev` or preview URL; only Edge, Web, and the
staging Auth compatibility surface remain public.

If any post-deploy check fails, the command restores every Worker version from
the pre-release snapshot and checks the restored Edge/Web entrypoints. Snapshots
are owner-only files under `deploy/cloudflare/.wrangler/releases/`. A prior
snapshot can also be restored explicitly:

```bash
npm run rollback:staging -- .wrangler/releases/staging-before-<timestamp>.json
```

D1 migrations and R2/Queue resources are not versioned by Workers rollback;
staging migrations must therefore remain backward-compatible with the captured
Worker versions. The current migrations are additive.

`smoke:staging` checks Edge health by default. To enable the authenticated
checks, provide a staging Better Auth token through an environment variable or
an explicit JSON token file:

```bash
CLOUDFLARE_SMOKE_TOKEN_FILE=/tmp/cf-auth-signup.json npm run smoke:staging
```

To deliberately exercise billable native TTS as part of that authenticated
smoke, add `CLOUDFLARE_SMOKE_NATIVE_TTS=1`; the check asserts a non-empty
`audio/mpeg` response and is opt-in.

The authenticated smoke verifies unauthenticated rejection, the D1 probe, the
conversation list/search, folder/memory shell dependencies, and the same
conversation read through Web `/api/proxy` so a missing Web→Edge binding fails
the release. It
also verifies the Workers AI raw-audio input boundary with an empty body, so it
does not invoke billable model inference; use a separate explicit audio request
for model quality or latency qualification.

The deployment script requires an already authenticated Wrangler session or a
scoped `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`; it never prints
secret values. It creates/applies only the isolated `omi-cf-*-staging`
resources.

Before exercising authenticated routes, configure secrets explicitly. The
values are read from stdin by Wrangler and are never committed:

```bash
cf_auth_secret="$(openssl rand -base64 48)"
printf '%s' "$cf_auth_secret" | npx wrangler secret put BETTER_AUTH_SECRET --name omi-cf-auth-staging
printf '%s' "$BETTER_AUTH_URL" | npx wrangler secret put BETTER_AUTH_URL --name omi-cf-auth-staging
cf_internal_secret="$(openssl rand -base64 48)"
for worker_name in omi-cf-auth-staging omi-cf-edge-staging omi-cf-api-core-staging omi-cf-api-ai-staging omi-cf-realtime-staging omi-cf-jobs-staging; do
printf '%s' "$cf_internal_secret" | npx wrangler secret put INTERNAL_ASSERTION_SECRET --name "$worker_name"
done
# Optional, staging-only Flutter Better Auth bridge (never use in a release build).
cf_dev_issuer_secret="$(openssl rand -base64 48)"
printf '%s' "$cf_dev_issuer_secret" | npx wrangler secret put AUTH_DEV_ISSUER_SECRET --name omi-cf-auth-staging
```

The `/auth-issue` bridge is hidden (`404`) unless `AUTH_DEV_ISSUER_SECRET` is
configured. It accepts only a matching bearer secret and a bounded `uid`, then
uses Better Auth's JWT plugin to mint the same 24-hour token shape as the local
development bridge. The Flutter client enables this path only when both
`OMI_AUTH_SERVER_URL` and `OMI_AUTH_DEV_ISSUER_SECRET` are supplied to a
non-release build; do not put the issuer secret in a release build or commit it.
Point `OMI_AUTH_SERVER_URL` at the Auth Worker URL and `OMI_API_BASE_URL` at the
Edge Worker URL when exercising the app against staging.

For a local debug run against the deployed staging slice, keep the issuer
secret in your shell only:

```bash
flutter run --flavor prod \
  --dart-define=OMI_APP_PROFILE=localProd \
  --dart-define=OMI_API_BASE_URL=https://omi-cf-edge-staging.summersmile1984.workers.dev/ \
  --dart-define=OMI_AUTH_SERVER_URL=https://omi-cf-auth-staging.summersmile1984.workers.dev \
  --dart-define=OMI_AUTH_DEV_ISSUER_SECRET="$cf_dev_issuer_secret" \
  --dart-define=OMI_AUTH_DEV_UID=mobile-better-auth-staging
```

The AI and realtime paths are API-first. They intentionally return `503` until
their provider is configured; no ASR/model process runs inside a Worker. The
Python Workers use Cloudflare's native `workers.fetch` for outbound calls so
they do not depend on Pyodide socket/DNS support. Add
the provider endpoint/key as Worker secrets when a staging provider is chosen:

```bash
printf '%s' "$ASR_WS_URL" | npx wrangler secret put ASR_WS_URL --name omi-cf-realtime-staging
printf '%s' "$ASR_API_KEY" | npx wrangler secret put ASR_API_KEY --name omi-cf-realtime-staging
printf '%s' "$ASR_API_BASE_URL" | npx wrangler secret put ASR_API_BASE_URL --name omi-cf-api-ai-staging
printf '%s' "$ASR_API_KEY" | npx wrangler secret put ASR_API_KEY --name omi-cf-api-ai-staging
printf '%s' "$EMBEDDING_API_BASE_URL" | npx wrangler secret put EMBEDDING_API_BASE_URL --name omi-cf-api-ai-staging
printf '%s' "$EMBEDDING_API_KEY" | npx wrangler secret put EMBEDDING_API_KEY --name omi-cf-api-ai-staging
printf '%s' "$AI_API_BASE_URL" | npx wrangler secret put AI_API_BASE_URL --name omi-cf-api-ai-staging
printf '%s' "$AI_API_KEY" | npx wrangler secret put AI_API_KEY --name omi-cf-api-ai-staging
printf '%s' "$TTS_API_BASE_URL" | npx wrangler secret put TTS_API_BASE_URL --name omi-cf-api-ai-staging
printf '%s' "$TTS_API_KEY" | npx wrangler secret put TTS_API_KEY --name omi-cf-api-ai-staging
printf '%s' "$ARTIFICIALANALYSIS_API_KEY" | npx wrangler secret put ARTIFICIALANALYSIS_API_KEY --name omi-cf-api-ai-staging
```

`/v1/stt/transcribe-async` is an additive staging route for clients that can
send a raw audio body. It accepts at most 5 MB, stages the bytes under an
uid-scoped temporary R2 key, records an idempotent D1 job, and lets the Queue
consumer run native Workers AI Whisper. The object is deleted after a terminal
result; the poll response contains only the bounded normalized transcription.
This route does not claim the legacy `/v2/sync-local-files` conversation,
memory, or diarization pipeline, so it must not be used as a production
replacement until those authorities have their own migration contract.

Browser WebSockets cannot attach an `Authorization` header during the HTTP
upgrade. Edge therefore signs a random, 30-second, one-use bootstrap for
`/v4/web/listen`; the isolated Durable Object accepts only that bootstrap and
verifies the first `{type: "auth", token: ...}` message through the Auth service
binding before opening the ASR provider socket. Binary audio before successful
authentication is rejected, and no two browser connections share a default DO
session. Header-authenticated native realtime routes retain their existing
upgrade contract.

Do not point these commands at production names from this worktree. The
staging smoke surface is:

```text
GET  /health                  public Edge liveness
GET  /ready                   Edge → all internal dependency readiness
GET  Web /api/worker-ready    Web → Edge service-binding readiness
POST /api/auth/sign-up/email  Better Auth + D1
GET  /v1/cf/probe             Edge → Auth → Python API Core → D1
POST /v1/stt/transcribe      Edge → Python API AI → hosted ASR API
POST /v1/stt/transcribe-workers-ai
                              Edge → Python API AI → Workers AI binding (raw audio)
POST /v2/realtime/session     Edge → Python API AI → OpenAI/Gemini ephemeral token API
POST /v2/realtime/usage       Edge → Python API AI → D1 usage projection
POST /v1/realtime/web-ticket  Edge cookie session → 30-second signed WebSocket ticket
POST /v1/stt/transcribe-async
                              Edge → Jobs → R2 → Queue → Workers AI Whisper
GET  /v1/stt/transcribe-async/{jobId}
                              Edge → Jobs → uid-scoped D1 result
POST /v1/embeddings-workers-ai
                              Edge → Python API AI → Workers AI BGE binding
POST /v1/translate           Edge → Python API AI → Workers AI m2m100 translation
POST /v1/tts/synthesize      Edge → Python API AI → hosted OpenAI-compatible TTS API
POST /v1/tts/synthesize-workers-ai
                              Edge → Python API AI → Workers AI Aura binding
GET  /v1/auto/model-pick    Edge → Python API AI → Artificial Analysis API + D1 cache
GET/POST /v1/ai/*           Edge → Python API AI → fixed OpenAI-compatible AI API
WS   /v4/listen               Edge → Realtime → Durable Object → ASR API seam
WS   /v4/web/listen           Edge bootstrap → first-message ticket → isolated DO → ASR API seam
R2   /v1/cf/assets/{key}      Edge → Python API Core → R2 + D1 metadata/checksum
JOB  /v1/cf/jobs              Edge → Jobs Worker → Queue → idempotent D1 ledger
GET  /v1/cf/jobs/{jobId}      Edge → Jobs Worker → uid-scoped D1 job status
POST /v1/cf/conversations     Edge → Python API Core → D1 idempotent projection upsert
GET  /v1/cf/conversations     Edge → Python API Core → D1 conversation projection
GET  /v1/cf/conversations/count
GET  /v1/cf/conversations/{conversationId}
                              bounded list/count/detail reads with uid isolation
GET  /v1/conversations       Edge → Python API Core → D1 canonical conversation list
GET/POST/DELETE /v3/memories
PATCH/DELETE /v3/memories/{memoryId}
PATCH /v3/memories/{memoryId}/visibility
POST  /v3/memories/{memoryId}/review
DELETE /v3/memories/batch
                              uid-scoped canonical D1 memory CRUD for isolated
                              Better Auth staging accounts; deletes tombstone
                              and there is no Firestore fallback or dual write
GET  /v1/account/cutover/control
                              Edge → Python API Core → D1 account migration control projection
GET  /v1/conversations/count
POST /v1/conversations/search
GET  /v1/conversations/{conversationId}
DELETE /v1/conversations/{conversationId}
                              canonical read/search projection and non-cascade delete;
                              finalization/merge remain legacy
GET  /v1/conversations/{conversationId}/photos
                              bounded photo projection; locked rows fail closed
GET  /v1/conversations/{conversationId}/transcripts
GET  /v1/conversations/{conversationId}/analytics
                              bounded transcript buckets and D1 speaker analytics
GET  /v1/conversations/{conversationId}/recording
                              R2 head check for uid/{conversationId}.wav; no download
GET  /v1/conversations/{conversationId}/action-items
GET  /v1/conversations/{conversationId}/action-items/count
                              standalone D1 action-item projection; locked rows fail closed
PATCH /v1/conversations/{conversationId}/segments/text
                              bounded D1 transcript edit with updated-at CAS
PATCH /v1/conversations/{conversationId}/events
PATCH /v1/conversations/{conversationId}/summary
                              structured event flags and default/app summaries → D1
DELETE /v1/conversations/{conversationId}/calendar-event
                              local calendar link removal only; external calendar remains legacy
PATCH /v1/conversations/{conversationId}/action-items
PATCH /v1/conversations/{conversationId}/action-items/{actionItemIdx}
DELETE /v1/conversations/{conversationId}/action-items
                              action-item state/description/delete projections → D1
PATCH /v1/conversations/{conversationId}/title
PATCH /v1/conversations/{conversationId}/starred
                              canonical metadata mutations → D1
GET  /v2/firmware/stable      Edge → Python API Core → GitHub Releases API
GET  /v2/firmware/latest      Edge → Python API Core → GitHub Releases API
GET  /v2/firmware/version     Edge → Python API Core → GitHub Releases API
GET  /v1/announcements/changelogs
GET  /v1/announcements/features
GET  /v1/announcements/general
                              Edge → Python API Core → D1 announcement projection
GET  /v1/announcements/pending
POST /v1/announcements/{announcementId}/dismiss
                              Edge → Python API Core → D1 + per-user dismissal
GET  /v1/announcements/all
GET  /v1/announcements/{announcementId}
POST /v1/announcements
PUT/DELETE /v1/announcements/{announcementId}
                              Edge → Python API Core → D1 (admin secret)
GET  /v1/app-categories
GET  /v1/app/proactive-notification-scopes
GET  /v1/app-capabilities
GET  /v1/app/payment-plans
                              Edge → Python API Core → static catalog metadata;
                              mutable app records, reviews, and subscriptions remain legacy
GET  /v1/approved-apps         Edge → Python API Core → approved public app D1 projection
GET  /v1/apps/popular          Edge → Python API Core → popular public app D1 projection
GET  /v2/apps                  Edge → Python API Core → paginated/grouped public app D1 projection
GET  /v2/apps/capability/{capability_id}/grouped
                              Edge → Python API Core → capability/category D1 projection
GET  /v2/apps/search
                              Edge → Python API Core → authenticated D1 search/filter projection
GET  /v1/apps/enabled          Edge → Python API Core → uid-scoped D1 install projection
POST /v1/apps/enable           Edge → Python API Core → idempotent free-app D1 install
POST /v1/apps/disable          Edge → Python API Core → uid-scoped D1 uninstall
GET  /v1/config/api-keys      Edge → Python API Core → Worker client-key vars
GET  /v1/users/transcription-preferences
PATCH /v1/users/transcription-preferences
GET  /v1/users/available-languages
GET  /v1/users/language
PATCH /v1/users/language
GET  /v1/users/onboarding
PATCH /v1/users/onboarding
GET  /v1/users/store-recording-permission
POST /v1/users/store-recording-permission
GET  /v1/users/private-cloud-sync
POST /v1/users/private-cloud-sync
GET  /v1/users/training-data-opt-in
POST /v1/users/training-data-opt-in
POST /v1/users/fcm-token
ANY  /v1/users/developer/webhook/*
GET  /v1/users/developer/webhooks/status
GET  /v1/users/notification-settings
PATCH /v1/users/notification-settings
GET  /v1/users/daily-summary-settings
PATCH /v1/users/daily-summary-settings
GET  /v1/users/mentor-notification-settings
PATCH /v1/users/mentor-notification-settings
GET  /v1/users/location-context-consent
PUT  /v1/users/location-context-consent
PATCH /v1/users/geolocation  Edge → Python API Core → D1 TTL row
GET  /v1/users/assistant-settings
PATCH /v1/users/assistant-settings
GET  /v1/users/ai-profile
PATCH /v1/users/ai-profile
                              Edge → Python API Core → D1
GET  /v1/users/profile         Edge → Better Auth → D1
GET/POST /v1/action-items      Edge → Python API Core → D1
GET  /v1/action-items/ids      Edge → Python API Core → D1
PATCH /v1/action-items/batch   Edge → Python API Core → D1
POST  /v1/action-items/batch   Edge → Python API Core → D1
POST /v1/action-items/batch-delete
                              Edge → Python API Core → D1
GET  /v1/action-items/pending-sync
PATCH /v1/action-items/sync-batch
                              Edge → Python API Core → D1 Apple Reminders projection
GET/PATCH/DELETE /v1/action-items/{actionItemId}
                              Edge → Python API Core → D1
PATCH /v1/action-items/{actionItemId}/completed
                              Edge → Python API Core → D1
GET /v1/conversations/{conversationId}/action-items
GET /v1/conversations/{conversationId}/action-items/count
                              Edge → Python API Core → D1 standalone task projection
GET/POST /v1/users/people   Edge → Python API Core → D1
GET/PATCH/DELETE /v1/users/people/{personId}
                              Edge → Python API Core → D1
PATCH /v1/users/people/{personId}/name
                              Edge → Python API Core → D1
GET  /v1/goals             Edge → Python API Core → D1
GET  /v1/goals/all         Edge → Python API Core → D1
POST /v1/goals             Edge → Python API Core → D1
GET  /v1/goals/canonical/list
POST /v1/goals/canonical   Edge → Python API Core → D1 mutation + receipt
GET/PATCH/DELETE /v1/goals/{goalId}
                              Edge → Python API Core → D1
GET  /v1/goals/{goalId}/detail
                              Edge → Python API Core → bounded D1 goal/workstream/task/event projection
PATCH /v1/goals/{goalId}/progress
                              Edge → Python API Core → D1
GET  /v1/goals/{goalId}/history
                              Edge → Python API Core → D1
POST /v1/goals/{goalId}/progress-events
GET  /v1/goals/{goalId}/progress-events
                              Edge → Python API Core → D1 event log + receipt
POST /v1/goals/{goalId}/focus
DELETE /v1/goals/{goalId}/focus
POST /v1/goals/{goalId}/lifecycle
                              Edge → Python API Core → D1 mutation + receipt
POST /v1/work-intents       Edge → Python API Core → D1 workstream + task
GET/PATCH /v1/workstreams/{workstreamId}
                              Edge → Python API Core → D1
GET/POST /v1/workstreams/{workstreamId}/events
                              Edge → Python API Core → D1 journal + receipt
GET/POST /v1/workstreams/{workstreamId}/artifacts
                              Edge → Python API Core → D1 artifact projection
PATCH /v1/workstreams/{workstreamId}/artifacts/{artifactId}/status
                              Edge → Python API Core → D1 journal + receipt
GET /v1/workstreams/{workstreamId}/checkpoints
PUT /v1/workstreams/{workstreamId}/checkpoints/{runtimeId}
                              Edge → Python API Core → D1 checkpoint + receipt
GET/POST /v1/folders        Edge → Python API Core → D1
GET/PATCH/DELETE /v1/folders/{folderId}
                              Edge → Python API Core → D1
POST /v1/folders/reorder    Edge → Python API Core → D1
GET /v1/folders/{folderId}/conversations
                              Edge → Python API Core → D1 conversation projection
PATCH /v1/conversations/{conversationId}/folder
                              Edge → Python API Core → D1 folder move + count refresh
POST /v1/folders/{folderId}/conversations/bulk-move
                              Edge → Python API Core → D1 atomic bulk move + count refresh
DELETE /v1/folders/{folderId}?move_to_folder_id=...
                              Edge → Python API Core → D1 rehome + folder delete
GET  /v1/calendar/onboarding/status
                              Edge → Python API Core → D1 flags
POST /v1/calendar/onboarding/skip
POST /v1/calendar/onboarding/reset
                              Edge → Python API Core → D1 flags
POST /v1/calendar/meetings
GET  /v1/calendar/meetings
GET  /v1/calendar/meetings/{meetingId}
                              Edge → Python API Core → D1 calendar metadata projection
```

The calendar meeting routes use a uid-scoped natural key
(`calendar_source` + `calendar_event_id`) and store bounded metadata in D1.
They are staging-only until the legacy conversation context reader is migrated
to the same authority; the Worker does not pretend to own OAuth tokens, event
discovery, or conversation finalization yet.

Only routes explicitly listed as migrated are sent to the partial Worker
implementations. Authenticated routes that are not yet migrated use
`LEGACY_BACKEND_URL` when configured; staging without that binding returns
`404 route not migrated` instead of silently treating the partial Worker as the
owner.

The destructive `DELETE /v1/users/store-recording-permission` operation remains
on the legacy owner until its R2/GCS recording deletion contract is migrated.

The location-consent route is staging-only while legacy chat still reads its
Firestore consent projection. Do not cut this route over in production until
that downstream consumer has moved to the D1 authority and passed its privacy
regression contract.

`PATCH /v1/users/geolocation` stores only the latest validated coordinates in a
uid-scoped D1 row with a 30-minute expiry and preserves the legacy success-shaped
response for invalid coordinates. It is staging-only because the legacy chat and
pusher consumers still read the Redis geolocation key; those consumers must move
to the D1 authority before production cutover.

The action-item routes provide a D1-backed, uid-scoped CRUD and reconciliation
projection with content-idempotent create, date-range filtering, completion
toggle, ordering batch update, Apple Reminders pending/synced projections, sync
batch updates, and batch deletion. Conversation-scoped list/count reads now use
the same standalone `conversation_id` projection and preserve locked-row
access checks. They intentionally do not
claim vector search, goal/workstream link validation, Apple Reminders sync,
sharing, FCM/reminder delivery, or legacy conversation-item restoration; those
side effects remain on the legacy owner until their separate contracts move.
The route group is staging-only until existing Firestore items are imported and
all downstream readers use the D1 authority.

`GET /v1/daily-score` and `GET /v1/scores` are read-only D1 projections over the
migrated action items. They preserve the legacy UTC day window, seven-day
created-at window, completion rounding, deleted-row exclusion, and default-tab
selection. They do not claim conversation-derived analytics or other legacy
score sources, so they remain staging-only with the action-item projection.

The focus-session routes (`POST/GET/DELETE /v1/focus-sessions` and
`GET /v1/focus-stats`) use a uid-scoped D1 event table. They preserve the legacy
focused/distraction duration defaults and top-five aggregation, but do not claim
screen-activity vector search, focus inference, or notification side effects.

The text-only screen-activity routes (`POST /v1/screen-activity/sync`,
`GET /v1/screen-activity`, and `GET /v1/screen-activity/summary`) use an
idempotent uid/device-scoped D1 upsert and bounded date/app queries. Incoming
embeddings are accepted for client compatibility but are not stored or searched;
vector lifecycle, paid semantic search, and MCP advanced search remain on the
legacy owner until their contracts move.

`npm run backfill:d1 -- --input export.ndjson` generates a transactional SQL
backfill from newline-delimited records. Every record must name one of the
whitelisted D1 tables (including `cf_conversations`) and carries
`{ "table": "cf_action_items", "row": { ... } }`;
the generator validates uid/id, normalizes timestamps/booleans/JSON, escapes SQL,
and uses uid+id upserts. It only writes SQL to stdout. Review the output and
apply it explicitly to the isolated staging database with Wrangler; the command
does not connect to Firestore or production by itself.

`CLOUDFLARE_SMOKE_TOKEN_FILE=/path/to/staging-token.json npm run benchmark:staging`
warms and samples six non-mutating staging endpoints, reporting p50/p95/max and
an optional p95 budget (`CLOUDFLARE_BENCHMARK_P95_MS`, default 4 seconds). It is
report-only by default; set `CLOUDFLARE_BENCHMARK_ENFORCE=1` to make a budget
exceedance fail the command.

The People routes migrate uid-scoped person id/name metadata with idempotent
create, list, rename, and delete operations. The response keeps the existing
Person shape, but speech sample URLs are empty until sample objects and signed
URL generation move from GCS to R2; the route group is therefore staging-only.

The goal routes migrate uid-scoped goal metadata and metric projection with
current/all listing, create/read/update, progress updates, and soft-abandon
delete. The canonical generation-scoped list/create surfaces now share this D1
projection; canonical create uses a deterministic goal id and mutation receipt
for safe retries. Daily progress history is stored in a uid/goal/date D1 projection and
served by the history route. Progress event feeds now append validated evidence
and metric events to a uid/goal sequence in D1; explicit event writes use a D1
mutation receipt, while the existing progress route emits a `metric_update`
event in the same batch as the metric and daily-history projection. Focus-cap and
retain-only lifecycle mutations are stored in a D1 mutation receipt and applied
as one batch. Relationship detach and AI advice/suggestion remain on legacy
until their stronger workflow contracts are migrated. Focus/unfocus/lifecycle
writes require
`Idempotency-Key` and `X-Account-Generation`; the five-slot focus cap and
replacement rule are enforced in a D1 batch, while relationship `detach` fails
closed until the workstream projection also moves. This route group is
staging-only pending goal/event backfill and downstream reader cutover.

The workstream routes migrate canonical workflow metadata, journal events,
artifact descriptors, continuation checkpoints, and task/goal-origin work
intents to D1. Mutating operations use generation-scoped idempotency receipts;
artifact revisions and checkpoints enforce their monotonic version/sequence
rules. Workstream search/index refresh and candidate automation remain legacy
owned. The goal detail reader now composes the bounded
goal/workstream/task/progress-event projections in D1; relationship detach and
AI advice/suggestion remain legacy-owned. This group is staging-only pending
workstream backfill and downstream reader cutover.

The R2 asset route stores a SHA-256 integrity projection in D1 alongside the
uid-scoped object metadata. Uploads can supply `X-Content-SHA256` for fail-closed
verification; downloads support one `bytes` range and `If-None-Match`, while
multi-range and unsatisfiable requests return `416`. Logical asset keys point
to immutable R2 storage keys, so an overwrite cannot destroy the previous
object before its D1 pointer commits. Superseded, deleted, or uncommitted
objects are tracked in D1 and retried by the Jobs Worker's 15-minute cleanup
sweep. R2 reads stream through the Python ASGI response; uploads retain the
bounded 25 MB compatibility surface. Large-object multipart migration and
signed URL issuance remain separate R2 cutover work.

The folder routes migrate system/custom folder metadata and ordering to D1.
Folder conversation listing and single-conversation moves now use the D1
conversation projection and refresh non-discarded folder counts transactionally.
The bulk move route validates every uid-scoped conversation, rejects locked or
missing rows, and updates all selected conversations plus folder counts in one
D1 batch. Folder deletion side effects remain staging-only until the
conversation authority moves in production.

The conversation routes use an explicit D1 projection (indexed metadata plus
bounded JSON transcript/structured fields). The POST `/v1/cf/conversations`
staging ingress accepts a pre-transcribed, bounded conversation and upserts by
the uid/id key; it does not run LLM enrichment. Canonical GET list/count/detail
routes now read this projection in staging. List responses never include
transcript segments, and locked rows redact derived content. Rows can also be
loaded by the reviewed backfill/import workflow. Title and starred metadata
mutations update the canonical D1 projection in staging. Visibility remains
legacy-owned because it also maintains public-share and Redis indexes.
Conversation search uses a D1 FTS5 index maintained by insert/update/delete
triggers over IDs, titles, summaries, categories, and transcript text. Search
remains uid-scoped, excludes locked rows, and supports the Web pagination,
discarded, date, and speaker filters. Default conversation deletion removes the
D1 projection and refreshes folder counts; `cascade=true` fails closed because
memory retraction and audio cleanup are not yet Worker-owned. Conversation
creation/finalization, memory extraction, merge, cascade deletion, audio
deletion, and downstream integration fanout remain legacy-owned; production
reader cutover still requires those write authorities and readers to move
together.

`GET /v1/conversations/{conversation_id}/transcripts` reads the bounded
`transcript_segments_json` projection and returns the same four provider buckets
as the legacy comparison view (`deepgram`, `soniox`, `speechmatics`, and
`whisperx`), sorted by segment start time. The importer must preserve
`stt_provider`; unknown providers are omitted rather than guessed. Locked rows
return `402`, and provider-specific Firestore writes remain legacy-owned until
the transcript write/finalization contract is migrated.

`GET /v1/conversations/{conversation_id}/analytics` computes the legacy
per-speaker talk-time, word-count, WPM, and talk-share response from the same
bounded transcript projection. Person labels are resolved from the uid-scoped
`cf_people` table; speech-profile extraction and other transcript side effects
are not part of this read route.

`PATCH /v1/conversations/{conversation_id}/events` updates the `created` flags
of indexed events in the structured D1 projection. It preserves the legacy
parallel-array and out-of-range-index behavior, rejects malformed input, and
does not claim calendar/integration fanout.

`PATCH /v1/conversations/{conversation_id}/summary` updates either the default
`structured.overview` or a matching `apps_results_json` entry. The bounded route
keeps app-summary identity uid-scoped, rejects missing app entries, and fails
closed for locked conversations; LLM regeneration and downstream enrichment
remain legacy-owned.

`DELETE /v1/conversations/{conversation_id}/calendar-event` clears only the
local `calendar_event_json` link in the D1 projection. It intentionally does not
call Google Calendar or mutate the external event; link creation and OAuth
ownership remain in the legacy integration service.

`PATCH /v1/conversations/{conversation_id}/action-items` updates indexed
completion flags in the structured projection and mirrors matching standalone
`cf_action_items` rows in one D1 batch. The bounded route preserves the legacy
index behavior; reminder delivery, exports, and other external side effects
remain outside this Worker authority.

`PATCH /v1/conversations/{conversation_id}/action-items/{action_item_idx}`
updates the first matching action-item description in both projections using one
D1 batch. The path index remains a compatibility component (the legacy handler
uses the description pair as the identity); deletion and reminder side effects
remain on the legacy owner.

`DELETE /v1/conversations/{conversation_id}/action-items` removes all matching
description entries from the structured projection and standalone D1 action-item
rows in one batch. The legacy `completed` field is accepted for wire
compatibility but does not alter the description identity; reminder/export
cleanup remains a separate downstream contract.

The calendar onboarding routes expose only a uid-scoped D1 projection of the
connected/skipped/re-auth-required flags. Google OAuth tokens, refresh, event
reads, and calendar writes remain on the legacy integration service; tokens are
never returned by these routes. This group is staging-only until existing
integration rows are backfilled and every downstream OAuth reader has cut over
to the same authority.

The announcement routes move public changelogs/features/general reads, the
authenticated pending/dismissal contract, and secret-gated admin CRUD to D1.
Version comparisons preserve the legacy semantic-plus-build behavior (a bare
semantic version matches all builds), while platform, firmware, device,
trigger, time-window, priority, and per-user `show_once` filtering are evaluated
in the Worker. Admin routes require the `ANNOUNCEMENTS_ADMIN_KEY` Worker secret
and remain staging-only until content backfill, key rotation, and rollback
evidence are approved; records can be loaded with the whitelisted backfill tool.

The app catalog metadata routes (`/v1/app-categories`,
`/v1/app/proactive-notification-scopes`, `/v1/app-capabilities`, and
`/v1/app/payment-plans`) are static, public responses and now run in API Core
without D1 or external providers. Mutable app records, reviews, subscriptions,
MCP credentials, and enable/disable side effects remain legacy-owned until
their catalog authority and user-installation state are migrated together. The
three installation routes below are a deliberately smaller projection: they
only accept approved public catalog rows that are free and have no external
setup callback.

`/v1/approved-apps` and the authenticated `/v1/apps/popular` route read only the
approved, non-disabled, non-persona records in `cf_app_catalog`. Records enter
that table through the whitelisted D1 backfill generator, which rejects private
fields such as reviews, payment identifiers, credentials, and prompts. This
projection is the first dynamic catalog slice. `GET /v1/apps/enabled`,
`POST /v1/apps/enable`, and `POST /v1/apps/disable` project only the
uid/app-id relationship into `cf_user_enabled_apps` and maintain the catalog
install counter for idempotent retries. Paid apps, private/persona apps, setup
callbacks, app creation, reviews, subscriptions, and MCP state remain
legacy-owned; no production cutover is implied.

`GET /v2/apps` now builds the marketplace's capability, category, and grouped
responses from the same public D1 rows. It preserves the legacy pagination
shape and score ordering, but intentionally returns `enabled: false` because
the public route has no user context; clients should combine it with
`/v1/apps/enabled`. Capability-specific grouped-category routes and the
authenticated `/v2/apps/search` filters are also read from the same projection.
Search only exposes approved public catalog fields and uid-scoped installed-app
state; private apps, paid entitlements, setup callbacks, reviews, subscriptions,
MCP state, and app-owner writes remain legacy-owned.

The migrated TTS surface is the desktop `/v1/tts/synthesize` OpenAI-compatible
contract. Mobile `/v2/tts/synthesize` remains on the legacy ElevenLabs contract
until its rate-limit and provider-shape migration is verified separately.

`/v1/tts/synthesize-workers-ai` is an additive raw-MP3 route backed by the
native `@cf/deepgram/aura-1` binding. It accepts bounded `{text, speaker}` JSON
using the model's documented speaker set and deliberately does not pretend to
support the existing provider-specific voice IDs. The existing
`/v1/tts/synthesize` route remains the voice-compatible external API seam until
voice parity and quality are qualified.

`/v1/stt/transcribe-workers-ai` is an additive raw-audio route backed by the
Python Worker's native `AI` binding and `@cf/openai/whisper-large-v3-turbo`.
It does not claim the legacy multipart/diarization contract; clients must send
`audio/*` (or `application/octet-stream`) as the body. The existing
`/v1/stt/transcribe` route remains the hosted provider seam for diarization and
the legacy segment response. The Python boundary converts the bounded request
body to the base64 form expected by the Workers AI Whisper model, so clients do
not need to know the binding's FFI representation.

`/v1/translate` preserves the standalone NLLB request/response shape while
using the native `@cf/meta/m2m100-1.2b` binding in staging. The Worker explicitly
limits this route to English, Chinese, French, Spanish, Arabic, Russian, German,
Japanese, Portuguese, and Hindi; the legacy NLLB service remains the rollback
target for other languages until quality and coverage are qualified.

`/v2/realtime/session` keeps the desktop ephemeral-token contract but calls the
configured OpenAI or Gemini mint API through the Python Worker's `workers.fetch`
FFI. Only a SHA-256 token hash and provider metadata enter D1; the token itself
is returned once to the authenticated client and is never logged or persisted.
`/v2/realtime/usage` writes uid/day token and micro-dollar aggregates to D1,
while the realtime WebSocket protocol remains owned by the separate Realtime
Worker/DO surface.

`/v1/auto/model-pick` uses a shared D1 24-hour cache. Without the upstream key,
an upstream failure, or an unusable model response it returns the existing
Gemini default with a provenance reason rather than failing the voice session.

`/v1/ai/*` is an authenticated, fixed-host proxy for OpenAI-compatible AI APIs.
The client cannot choose the destination: `AI_API_BASE_URL` and `AI_API_KEY` are
Worker secrets, and the proxy only forwards `content-type`/`accept` plus the
request path after `/v1/ai`. Requests and responses are bounded to keep model
payloads from turning the Python Worker into an unbounded buffer.

`/v1/embeddings-workers-ai` is an additive text-embedding seam backed by the
native `@cf/baai/bge-base-en-v1.5` binding. It accepts a bounded string or batch
and returns OpenAI-style `data[].embedding` vectors. The model's 768-dimensional
output is intentionally not substituted for the existing external embedding
model; Vectorize/index compatibility and retrieval quality must be qualified
before any client or index cutover.

`/v1/users/assistant-settings` stores the partial, sectioned settings document
as JSON in D1 and deep-merges section updates so a toggle cannot erase sibling
fields. `/v1/users/ai-profile` stores only the low-risk generated profile
projection with bounded text and metadata; it is intentionally separate from
entitlements, BYOK, and privacy state. These two routes are staging-only until
an import/backfill plan for existing Firestore users is approved.

`/v1/users/profile` is an identity-only projection owned by the Better Auth
Worker. It reads `id`, `name`, `email`, and `createdAt` from the auth D1 user
table and preserves the legacy 410 response for an unknown user. Firestore-only
fields such as data-protection level, onboarding answers, and migration status
are intentionally omitted until their D1 authority and backfill are approved;
the Flutter client already treats those fields as optional/defaulted.

`/v1/users/training-data-opt-in` stores the review state in staging D1 and
enables private cloud sync as the legacy route does. The HTTP response remains
the legacy success/message shape. The notification side effect is intentionally
not claimed yet: FCM token storage and delivery still belong to the legacy
notifier until that provider boundary is migrated.

`POST /v1/users/fcm-token` stores one token per sanitized
`platform + device-id-hash` key in staging D1 and keeps the legacy `{"status":"Ok"}`
response. Tokens are not returned by any public route. The legacy FCM sender
still owns delivery until it can read the D1 token authority with an explicit
provider and deletion contract.

Developer webhook configuration routes now use the staging D1 table
`cf_user_developer_webhooks`; supported types are `audio_bytes`,
`audio_bytes_websocket`, `realtime_transcript`, `memory_created`, and
`day_summary`. URL/configuration status is isolated from delivery health. The
legacy webhook sender still reads Redis, so these settings are staging-only
until delivery is moved to a Worker/Queue consumer with retry and disable
semantics.

`/v1/users/daily-summary-settings` and
`/v1/users/mentor-notification-settings` store notification preferences in the
staging app D1 database with the legacy defaults (22:00 local and frequency 0)
and bounded values. The legacy daily-summary scheduler and mentor notifier
still read Firestore, so these routes remain staging-only until those consumers
move to the D1 authority; the Edge fallback can be restored without a client
change.

The queue accepts infrastructure `probe` jobs and native Workers AI
`transcribe` jobs. Producers must use a stable `jobId` or idempotency key;
reusing either identity with a different request fingerprint is rejected.
Messages are claimed in D1 per uid, retried independently, and moved to
`omi-cf-jobs-dlq-staging` after the configured retry limit instead of being
discarded. Transcription audio is removed after completion or terminal failure;
an R2 lifecycle rule expires any `cf-transcriptions/` cleanup orphan after one
day. `GET /v1/cf/jobs/{jobId}` exposes the state machine without returning
payload data, and requires the same authenticated uid that created the job.
