# Omi Cloudflare Workers

This directory is the Worker-first deployment surface described in
[`dev/cloudflare-adaptation-plan.md`](../../dev/cloudflare-adaptation-plan.md).
It intentionally does not import the monolithic `backend/main.py`.

Post-deploy acceptance, test identities, evidence capture, observation windows,
known product gaps, and rollback criteria are defined in
[`dev/cloudflare-staging-validation-plan.md`](../../dev/cloudflare-staging-validation-plan.md).

## Current main release workflow

`deploy:staging` (profile stage `beta`) and `deploy:production` now prepare local,
immutable candidates from the current Moonshine/Bun source. Both Server OS and
Cloudflare Web artifacts, all nine Worker bundles, CF3 resource configurations,
SQL authorities and dependency locks are qualified before any remote mutation.
These commands do not deploy. See [the release workflow](release.md) for exact
inputs, qualification, remote apply, version ownership and recovery commands.

A local candidate always has `release_ready=false`. CF-4 product coverage, CI-1
cross-target contracts, prior Worker/new-schema compatibility and actual remote
resource/domain/version observations remain required. The first-release schema
runner now verifies fresh Worker absence, empty D1 authorities and frozen SQL;
it refuses retained-version upgrades/restore until their executable compatibility
harness exists. CF-4 and CI-1 are still missing, so remote `apply` and `restore`
reject before contacting Cloudflare. Operator-written approval JSON cannot bypass
their absence. See [Eddy's observed first-release schema evidence](../../dev/unified-main/implementation-2026-09-05/eddy-first-release-schema.md).

Credentials come from the existing CF3 secret-name map. The publisher never
generates new application credentials or infers a resource owner from an error.
Unknown remote results require journal reconciliation; data resources and
first-release Workers are retained until cleanup ownership is separately proven.

### Historical production evidence (2026-09-01; not current main qualification)

The first independent release is live without changing `api.omi.me` or
`app.omi.me`. Auth D1 has 10 applied migrations and App D1 has 150, with no
pending migration in either authority. All eight active versions are at 100%:

- Auth `502fa91c-b92a-4d2e-a0ec-a735d7d10e03`
- Rate Limit `abdf358c-874c-4801-a00a-a4b08ba708cc`
- API Core `fd80438f-8fd9-4a1a-a118-acbc0db3d832`
- API AI `bb938ec8-75bf-44b4-a2f2-4c4dd62d3867`
- Realtime `569055c6-bc88-43a2-9f08-a1e1f72b8531`
- Jobs `63d79cb4-8a31-4a9a-8798-87bb448c7bab`
- Edge `d25adeca-01f0-43eb-a81e-a9ec5a0e1f6c`
- Web `d5321b10-92a6-457a-9be3-ac54db1598ae`

Release qualification passed 102 Cloudflare test files / 755 tests, API Core
389 tests, API AI 117 tests, and 12 Web test files / 47 tests. Edge and Web
readiness both returned HTTP 200 with every internal dependency at 200. The
authenticated production smoke passed, including the Web proxy and one real
Workers AI chat call. A 20-sample warm-path benchmark passed the 4,000 ms p95
budget for all six endpoints; the largest observed p95 was 1,847.97 ms.

A real browser signed in through the production Web Worker and rendered
Conversations, Memories, Apps, and Tasks. CDP captured 187 events across those
routes with zero console exceptions, failed requests, or HTTP 4xx/5xx
responses. The dedicated smoke identity was then deleted through the public
Queue workflow: Auth user/session/account, App cutover/intent, and chat
session/message residuals were all zero; one expected short-lived deletion
tombstone remained, the old bearer returned 401, and the browser returned to
`/login`.

The first staging slice contains:

- `edge`: public routing, request IDs, trusted auth context and legacy fallback.
  It also owns BYOK enrollment and request validation in D1, so downstream
  Workers receive a signed `byokActive` authority only when all four raw key
  headers match their HMAC-peppered enrollment fingerprints.
- `auth`: Hono + Better Auth + D1, with request-scoped auth construction.
- `api-core`: a minimal FastAPI/Python Worker composition root with a D1 probe,
  uid-scoped R2 asset API (`/v1/cf/assets/{key}`) with checksum and range
  semantics, uid-scoped transcription
  preferences, onboarding/privacy/notification/location-consent settings, and
  the public firmware stable/latest/version APIs. It also exposes staging-only
  D1-backed action-item and canonical memory CRUD surfaces, plus account usage,
  chat quota, subscription snapshot, and configured price-catalog reads.
  Migration 0161 enforces memory locks inside D1 writes across native, MCP,
  developer and conflict-review mutations. Locked writes return 402 and roll
  back their transaction; privacy deletion remains available. This does not
  qualify the remaining memory ledger history/revert routes. See the
  [verification record](../../dev/unified-main/implementation-2026-09-05/eddy-memory-mutation-lock-2026-09-06.md).
- `api-core`: a public firmware stable-release API backed by the GitHub Releases
  API; it keeps firmware metadata outside the Worker filesystem.
- `api-ai`: a minimal FastAPI/Python Worker composition root for provider APIs.
- `realtime`: the Durable Object/ASR protocol seam; supported mono audio is
  streamed directly to the Workers AI Nova-3 binding and no model runs locally.
- `jobs`: durable background work plus D1-backed X, task-provider, and Google
  Calendar connectors.
  X OAuth uses PKCE and the task integrations use one-time, ten-minute D1 state
  with a provider-scoped disconnect generation fence; both store only hashed
  OAuth state and AES-GCM-encrypted tokens. Todoist,
  Asana, Google Tasks, and ClickUp calls run over their hosted HTTPS APIs, so no
  task-provider service runs locally or in another container. Google Calendar
  uses a dedicated one-scope OAuth grant, encrypted D1 tokens, automatic token
  refresh, and the hosted Calendar API for event-picker reads.

The admin-key-protected `/v1/summary-app-ids` set is also authoritative in D1
for staging. It is not dual-written to the legacy Redis set; a production
cutover must explicitly import the existing set before routing these mutations
to Workers.

The public `GET/POST /email/unsubscribe` pair is now owned by API Core for
lifecycle consent. The signed query token supplies identity independently of
app cookies or bearer tokens. GET is scanner-safe; POST accepts the RFC 8058 form
without body-dependent admission. Auth owns account existence; one fenced App D1
row owns suppression and participates in export/account deletion. Configure Core
with the independent `LIFECYCLE_EMAIL_SIGNING_SECRET` reference described in
[resources.md](resources.md). Invalid links and deleted accounts receive neutral
HTML with no token echo; confirmation pages disable caching/referrers. Lifecycle
mail sending and external delivery are not verified by these endpoints.

`GET /v1/calendar/capture-gaps` is now owned by Jobs alongside the event picker.
It reuses the encrypted Calendar grant and bounded refresh path, then joins at
most 250 provider events with 500 uid-scoped D1 recording intervals using the
indexed start-time range. The upstream 31-day window, 24-hour recording lookback,
ten-second coverage threshold and discarded/attendance exclusions are preserved.
It does not create or change recordings; provider/database failures are errors,
not an empty successful result. Missing/invalid dates return query-field 422;
zone-less dates are UTC. [Verification](../../dev/unified-main/implementation-2026-09-05/eddy-calendar-capture-gaps-2026-09-06.md)
separates real dual-target admission from controlled provider joins.

Task request model validation returns HTTP 422 with FastAPI's `detail` field-error
array, including the `body` location and no Pydantic documentation URL. The same
response owner serves task creation, updates, shares and sync/batch model errors.
Non-model domain rejections retain their route-specific statuses. The regression
suite runs the real ASGI route beside current upstream `ActionItemCreateRequest`
and `ActionItemUpdateRequest`; this validates typed rejection parity, not the
entire task schema or all query/batch semantics.

## Local setup

```bash
npm ci
npm run python -- api-core sync
npm run python -- api-ai sync
npm run python -- api-core dev --port 8787 --inspector-port 9236
```

`scripts/python-worker.mjs` is the shared Python entry for local development,
dry runs, and the current release candidate builder. It runs an isolated
`workers-py==1.16.7` tool with `uv==0.12.3`, verifies the installed Wrangler and
workerd against the unchanged npm lock, and consumes each project's committed
`pylock.toml`. Dependency preparation and the unchanged-lock check finish before
the deploy command. Do not run the floating project `uv run pywrangler`: as of
2026-09-04 it resolves workers-py 1.17.1, whose minimum Wrangler 4.127.1 is newer
than this repository's locked 4.127.0. The pinned tool's own version guard stays
active. First installation needs access to the Python package registry.

For an already-installed offline tool, set
`CLOUDFLARE_PYWRANGLER_EXECUTABLE=/absolute/path/to/pywrangler`; the entry verifies
that it really is version 1.16.7 and still supplies pinned uv. A different tool
version fails closed. No project virtual environment or lock is upgraded by this
selection. This override is useful when a registry cache cannot resolve a fresh
tool environment; it does not establish that a clean online installation passed.

Every non-sync Python invocation projects the project's ordinary `src` files
and canonical `python/shared/*.py` modules into a private temporary stage via
`scripts/python-source.mjs`. Duplicate module owners, source links and alternate
build roots fail closed. The original working directory and locked dependencies
remain authoritative; generated configuration and local secrets are private,
relative migration paths retain their original owner, and cleanup runs after the
command. Neither the source tree nor CF5's regular-source-only rule is changed.
Dev runs use that source snapshot; restart the command after editing source.
CPython tests load the same canonical shared directory through their conftest.

API Core also stages the upstream screenshot wire types, canonicalizer, palette,
privacy prompt and survivor selection through `scripts/screen_frame_sources.py`.
The compiler uses the existing `backend/.venv/bin/python` prerequisite and
copies ordinary source files; only the model import paths are relocated. The
prompt and selection expressions come from their upstream syntax trees. A
missing expression or competing staged module fails the build. These backend
owners participate in the release source identity and the existing route CI
lane, so an upstream change requires a fresh candidate and verification.
Core pins Pillow 11.3.0 to the Pyodide 0.28.3 wheel and its SHA-256 in its own
`pylock.toml`; `npm run python -- api-core sync` prepares it with the other
dependencies. The independent [screenshot writer](workers/screen-frame-writer/README.md)
now owns the isolated `SCREEN_FRAMES` bucket, one-use approval receipts,
revocable content capabilities and erasure. Core and Jobs receive a service
binding only. Core and the writer share a dedicated signing key; Jobs can
request cleanup through its request-bound internal assertion. The eight public
screenshot routes remain blocked pending hosted model access, the full request
envelope, public Edge routing and business qualification. Screenshot processing
now uses native Images for source decoding and resizing; Core keeps the upstream
final JPEG/thumbnail encoder and exact-byte judge/approval contract. PNG alpha is
discarded without a source-sized pixel allocation, preserving the original RGB
colors. Native 64-megapixel and 20 MiB single-candidate evidence is recorded in
[the codec verification](../../dev/unified-main/implementation-2026-09-05/screen-image-hosted-2026-09-06.md).

Local development on Linux/macOS resolves the native executable from the locked
`workerd` package export, then uses its official Pyodide bundle/package cache
flags. The shell wrapper executes that binary directly, preserving Miniflare's
control file descriptor; the npm Node shim forwards only standard input/output
and would leave Wrangler waiting for readiness while its worker already runs. The default cache is `.wrangler/pyodide`;
`CLOUDFLARE_PYODIDE_CACHE_DIR` can select another directory. workerd validates a
cached bundle against its compiled integrity digest. The first download still
needs trusted TLS access to Cloudflare's Pyodide distribution. This does not
disable certificate checks or replace the runtime. See Cloudflare's
[Python runtime](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/)
and the pinned workerd's
[cache/integrity implementation](https://github.com/cloudflare/workerd/blob/v1.20260826.1/src/workerd/server/pyodide.c++).

Pass `--config` relative to `python/api-core` or `python/api-ai`, and keep
`python_modules` beside that config as required by Wrangler. Use a separate
inspector port for each concurrently running Worker. Secrets belong in local
`.dev.vars`; do not commit local configs, credentials or persistent D1 fixtures.
An individual Core health response does not qualify bindings to Auth, Edge, AI,
Jobs, queues, R2 or Vectorize. The real local HTTP/WS scope and residual gaps are
recorded in [`12-cf-runtime-evidence.md`](../../dev/unified-main/12-cf-runtime-evidence.md).

```bash
npm run python -- api-core deploy --dry-run
npm run python -- api-ai deploy --dry-run
```

These commands do not create or deploy remote resources. The shared release
builder uses this fixed Python entry to prepare dependencies and compile both
Python Workers, then verifies the frozen modules with locked Wrangler.

## Brand and stage resource plans

The default chat identity is public deployment configuration, projected from the
same validated manifest as the Web profile. `profile_input.py` emits
`brand_runtime = {brand_id, display_name, ai_persona_name}`; CF3 verifies its brand
binding and display-name agreement with the Web input, then writes
`BRAND_RUNTIME_JSON` to Core and AI. Generated resource/configuration hashes in
release candidates therefore include this identity. Direct Wrangler development
configs explicitly select the checked-in `omi-upstream` manifest; the template
contract tests compare their values with that manifest.

Core's default greeting and unnamed share sender use that identity. AI's default
chat, initial-message, stateless reply and CF completion prompts use the persona;
quota text and unnamed app labels use the product display name. Existing user,
plugin and persisted message text is not searched or replaced. A missing,
malformed, extra-field, non-string, empty or control-character configuration
returns HTTP 503 before chat quota/session/provider effects. There is no upstream
brand fallback for an unconfigured Cloudflare deployment: regenerate the resource
plan/rebuild the Worker before use. An otherwise valid configuration retains the
existing provider and storage error classification.

`BRAND_SUPPORT_EMAIL` is a separate public scalar projected only from the same
manifest's existing `brand.support_email`; it does not extend the three-field
identity JSON or introduce an operator naming input. CF3 requires a plain email
address, writes it only to Core, and hashes it with the generated configuration.
The direct template uses `omi-upstream`'s declared `help@omi.me`; the local runner
records its explicit synthetic contact. Missing/invalid contact makes both
fair-use status and public case presentation return503. Fair-use warning/support
text, the public case contact field, overage product names and unnamed task-share
sender/provenance use the brand projection. Existing fee calculations, thresholds,
user names and task descriptions are preserved; this change sends no email.

This covers default text chat identity, not every white-label product surface.
Share URL routing, OAuth/email templates
and the remaining CF4 route capabilities require their own owner verification.
The old chat-share URL constant is not made correct by changing a display name.

The app draft generator also formats its owned platform template with the
configured product display name, preserving creator input and generated plugin
text. Goal advice labels existing assistant chat lines with the configured AI
persona. Both reject missing brand configuration before model/context work while
keeping their existing provider-error behavior. The local contract exercises
public app generation and goal creation/advice through real Core/AI/D1 and
inference-only prompt echoes; remote vector retrieval quality is not qualified.

`npm run resources -- --manifest /path/to/brand.json --inventory /path/to/resources.json --web-build /path/to/web-build --output /path/to/plan`
renders the eight current Worker/Web configurations, both D1 migration authorities,
secret **name** mappings, and a plan-bound rollback contract. It consumes the same
rendered profile as the Moonshine builder. See [resource plan input and validation](resources.md)
for the complete local workflow, existing-resource ownership, and qualification limits.
Rendering performs no Cloudflare API call and never creates, renames, or deletes resources.
The current release builder consumes this exact plan and freezes its eight configurations.

## Staging resources

Resource names are deliberately isolated from existing account resources:

- D1: `omi-cf-auth-staging`, `omi-cf-app-staging`
- Workers: `omi-cf-edge-staging`, `omi-cf-auth-staging`, `omi-cf-api-core-staging`, `omi-cf-api-ai-staging`, `omi-cf-realtime-staging`, `omi-cf-rate-limit-staging`
- Jobs Worker: `omi-cf-jobs-staging`
- Queues: `omi-cf-jobs-staging`, `omi-cf-sync-fresh-staging`,
  `omi-cf-sync-backfill-staging`, and `omi-cf-jobs-dlq-staging`
- R2: `omi-cf-staging`, `omi-cf-conversation-recordings-staging`, the
  biometric-isolated `omi-cf-speech-profiles-staging`, and the reviewed desktop
  artifact mirror `omi-cf-desktop-updates-staging`
- Durable Objects: Realtime sessions and standalone rate-limit windows
- Vectorize: `omi-cf-conversations-v2`, `omi-cf-memories-v2`,
  `omi-cf-action-items-v2`, `omi-cf-transcript-chunks-v2`, and
  `omi-cf-x-posts-v2` (rebuildable 1024-dimensional BGE-M3 projections)

The deploy script only deploys the named staging environment. It applies D1
migrations before Workers and deploys Edge last. It never creates production
resources and never mutates existing Omi Workers.

## AI provider policy

Workers AI is the only generative-AI provider in the target isolated
deployment. Do not provision `OPENAI_*` or `GEMINI_*` secrets: the canonical
chat, file-Q&A, ASR, translation, TTS, summary, and embedding paths call the
bound Cloudflare `AI` service directly. The OpenAI/Gemini/BYOK/Assistants code
that remains in the tree is an explicitly disabled compatibility boundary; a
request that selects one of those paths fails closed and must never be used by
the new client. Canonical chat files use `CHAT_FILES` R2 plus the Workers AI
attachment Queue; only private image thumbnails require the optional Cloudflare
Images binding and HMAC secret. Calendar, Twilio, and other explicitly enabled
business integrations may still use their own REST APIs; those credentials are
not AI provider dependencies.

## Migration inventories

Four reviewed inventories keep the remaining legacy infrastructure explicit:

- `manifests/backend-routes.json` is generated from the hermetically imported
  FastAPI app and records every registered HTTP and WebSocket route. Each entry
  must be reviewed as `staging-owned`, `legacy-owned`, or `blocked`; regenerating
  after a new backend route leaves it `unclassified` and fails the fork route
  gate. The current inventory contains 619 backend route identities: 602 have
  Cloudflare staging owners, 17 are blocked with planned owners and missing
  contracts in [the CF-4 ledger](../../dev/unified-main/09-cloudflare-route-migrations.md),
  and 0 remain `legacy-owned`. This is a coverage classification, not a
  complete Cloudflare product qualification. Edge directly serves
  the dependency-free `/v1/health`, Apple domain-association, and OpenAI Apps
  challenge compatibility routes. This guard was added
  after the 2026-08-29 staging conversation-page API 404 incident exposed that
  the migrated-only route manifest could not prove complete backend coverage.

- `manifests/redis-primitives.yaml` assigns every public helper in
  `backend/database/redis_db.py` and every direct production Redis client caller
  to one final KV, Durable Object, D1, Queue, or Workflow owner. Cloudflare
  Worker source is forbidden from importing or connecting to Redis.
- `manifests/vector-namespaces.yaml` records every Pinecone namespace, current
  model/dimensions, authoritative hydration source, and the versioned Vectorize
  re-embedding target. Existing 3072-dimensional projections cannot be copied
  into Vectorize unchanged.
  Memory migration 0162 couples each canonical revision to durable projection
  work in D1; Jobs consumes that revision instead of a wall-clock timestamp.
  Migration 0163 journals every external memory-vector attempt before writing,
  uses immutable vector IDs, and compares the canonical revision at publication.
  Account erasure waits for in-flight writers and observed external cleanup;
  accepting an asynchronous delete request does not clear its journal.
  Individual memory deletion tasks now retain their exact projection outbox
  until that memory's artifacts through the observed revision have drained.
  Queue delivery retries while cleanup is pending; other sources, owners and
  newer revisions do not delay that completion or lose their mappings. See the
  [hosted deletion verification](../../dev/unified-main/implementation-2026-09-05/memory-vector-delete-2026-09-07.md).
  The [memory write contract](../../docs/doc/developer/ForkCloudflareMemory.mdx)
  describes the implemented boundary and remaining external-index qualification.
- `manifests/r2-namespaces.yaml` records every legacy `BUCKET_*` binding, object
  prefix, lifecycle, data classification, and isolated R2 bucket target. It
  forbids dual-write cutovers and requires residual scans before deletion.

`npm run validate:manifest` checks these inventories against current backend
source. The preflight/deploy route check separately imports `backend/main.py`
with the pinned OpenAPI runner and checks that `backend-routes.json` is current.
A new route, Redis helper/direct client, Pinecone namespace, storage bucket, or
Worker-side Redis dependency therefore fails before release. Refresh a
deliberately changed route surface with:

```bash
backend/scripts/openapi_runner.sh ../deploy/cloudflare/scripts/route_inventory.py --write
```

Then assign the new entries an explicit owner/runtime; the generated
`unclassified` state cannot pass the check. Inventory-only targets are not
provisioned resources and do not imply a production cutover.

`bash deploy/cloudflare/ci/routes.sh` (from the repository root, after
`make setup-backend`) runs the same local/CI route lane: hermetic backend
registration and drift negatives, inventory/manifest validation, TypeScript
checks/tests, and API Core Python tests. CI provisions Node 22 and the pinned
backend interpreter. Unit tests use the documented Workers stubs; this lane
does not claim workerd, browser, authentication integration, or production
qualification. `verify:migrations` reads remote D1 and is intentionally outside
this credential-free lane. Runtime contracts remain part of CI-1.

API Core now serves `GET /v2/desktop/prompts` through authenticated Edge
routing. Migration `0153_desktop_prompts.sql` stores operator-authored global
prompt documents in `cf_desktop_prompts`; `active=1` rows retain upstream
channel/build targeting, stable per-user rollout, defaults, and bounded
options. Missing configuration returns an empty list; missing D1 or malformed
configuration returns 503. Seed this table through reviewed operator migrations
or the existing D1 administration workflow; this route creates no admin write
API and never returns targeting metadata. No account data is stored in this
global configuration table.

The authenticated App Generator routes (`GET /v1/app/generate-prompts`,
`POST /v1/app/generate`,
`POST /v1/app/generate-description`, and
`POST /v1/app/generate-description-emoji`) are served by the API AI Python
Worker using the native Workers AI binding. Prompt generation keeps the legacy
five-prompt static fallback; description+emoji keeps its legacy fallback for
malformed model JSON. Successful calls record best-effort `app_generator`
usage in D1, and each route has a 30-per-hour Edge Durable Object rate limit.

## Commands

```bash
npm test
npm run typecheck
npm run verify:migrations -- --candidate /path/to/candidate
npm run deploy:staging -- --manifest /path/to/brand.json --inventory /path/to/resources.json --output /path/to/new-candidate
npm run smoke:staging
```

### Web Worker and Server OS from one source

The current upstream Web app is Moonshine/Bun. The fork-owned
[`deploy/web/build.ts`](../web/build.ts) stages that source with the profile/auth
and capability overlays, typechecks production sources, and builds either a
portable Bun artifact or a Worker with static assets. The release builder builds
both targets for the same brand and stage. Browser JWT/session and API/WS URLs
come from that same rendered profile; optional OAuth and direct provider flows
remain gated by their actual capability contract.

```bash
bun deploy/web/build.ts --target cloudflare --stage beta --manifest /path/to/brand.json --output /path/to/new-web-build
```

Run this command from the repository root. Publication uses the generated frozen
artifact through the common [release workflow](release.md); retired framework
commands and publisher compatibility aliases have been removed.

The integrations page opens the provider window synchronously before fetching
the OAuth URL, then navigates that window after the request completes. This is
required for browsers that block a popup opened after an asynchronous request;
if a popup is blocked, the flow falls back to the current tab.

The unified Web builder (`deploy/web/`) uses CLIENT-1's explicit profile Auth
origin and session bearer. It does not use the retired cookie-only Web proxy.
Web recording obtains an AUTH-1 product JWT, then sends
`{ "type": "auth", "token": "<JWT>" }` to `/v4/web/listen`. Realtime calls the
Auth service binding for signature plus current session/user ownership, then
applies uid admission, the optional account migration fence and fair-use policy
before starting ASR. Its internal signed bootstrap only admits the isolated
upgrade; it is never a browser credential. The fork web-ticket endpoint is
retired. Native `/v4/listen` and `/v2/voice-message/transcribe-stream` retain
their shipped Bearer upgrade protocol. See `contracts/realtime/web-listen.md`
for the source wire contract and verification boundaries.

MCP retains a separate issuer and explicit profile origin; the unified Web
MCP browser/callback flow still needs its own qualification. The historical root
`GET/POST /authorize` and `POST /token` paths are aliases to Better Auth's
`/api/auth/oauth2/*` provider, so older MCP clients use the same D1
client/consent/token authority instead of the legacy Firebase-backed handler.
The consent page sends only Better Auth's signed authorization query, and MCP access tokens terminate
at Edge; API Core receives a request-bound signed principal instead of the
bearer token. Root and Better Auth-suffixed authorization-server discovery are
served by both Edge and Web, including bodyless `HEAD`. MCP grant listing and
revocation traverse Edge → Auth with a request-bound assertion; revocation
removes the user's matching access tokens, refresh tokens, and consent, and
subsequent token verification requires that live consent to remain present.
Public Better Auth routes retain the
D1-backed per-IP limiter. The secret-guarded Edge session-verification request
bypasses that public limiter so service-binding calls without a client IP cannot
collapse every staging user into one shared rate-limit bucket.

After Edge verifies a client session, it removes the client cookie/bearer and
mints a fresh HMAC assertion for the exact downstream request. Assertions last
at most 60 seconds and bind the uid to one audience (`api-core`, `api-ai`,
`auth`, `jobs`, or `realtime`), HTTP method, and path. Every TypeScript
downstream verifies those claims directly; both Python Workers enforce the same
contract in ASGI middleware whenever internal assertion headers are present.
This prevents a captured assertion for one service or route from being replayed
against another. The explicit legacy fallback is the only path that preserves a
client bearer, because the legacy backend remains its verifier during cutover.

An isolated allocation has one server-authoritative account/data-plane binding.
For `allocation: new`, the resource renderer enables
`ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED` on Core, Edge and Realtime, retaining the
configured `ACCOUNT_CUTOVER_MANIFEST_ID` on Core/Jobs. The first authenticated
control read atomically registers a Better Auth principal as a bound `new`
account. This is an explicit native-account policy for an allocation with no
imported legacy authority; it never overwrites an existing ownership record or
initializes a Firebase principal. Missing rows with bootstrap disabled still
project as `legacy`. Existing allocations retain their template policies.

Edge and Realtime consult this owner before product traffic even when the
separate migration-only `account_activation_fence` capability is false. They
require `state=new`, allowed traffic and a bound destination, including during
account deletion. Auth/profile and control remain reachable. Native privacy
requests also initialize/read ownership, so deletion can be the first request
after signup; Jobs retains the authoritative deletion eligibility check and
privacy stays reachable while ordinary traffic is fenced. A failed ownership
read returns 503 before dispatching to Jobs.

The current release workflow records exact prior versions for all nine Workers,
then verifies SQL history, applies additive migrations, deploys dependencies in
the CF3-derived order, and verifies both public readiness envelopes plus the
active version annotations. A failed or lost process response is reconciled with
actual version/ledger observations. Recovery requires a separate explicit command
and a fresh prior-version/new-schema proof. It never interprets an unknown
version as a first release or deletes a Worker based on a missing snapshot.

D1 migrations and R2/Queue resources are not versioned by Workers rollback;
staging migrations must therefore remain backward-compatible with the captured
Worker versions. The current migrations are additive.

The desktop release manifest projection is intentionally one-way while
production promotion remains Firestore-owned. The legacy `/updates/releases`
bridge is also D1-backed in isolated staging (using `RELEASE_SECRET`), but the
production workflow remains on its Firestore bridge until the release pipeline
cutover. Historical replay first emits a read-only, content-bound plan; after
manual review, the explicit `--apply` path uses the default-off Jobs executor
and its D1 receipt. See
[`desktop-release-history-reconcile.md`](../../docs/cloudflare-architecture/desktop-release-history-reconcile.md).

To replay one retained production manifest into the staging D1 table, provide
the staging Edge URL and an admin key on a protected runner:

```bash
ADMIN_KEY='…' python3 .github/scripts/backfill-desktop-release-manifest.py \
  --release-id v1.2.3+10203-macos \
  --output /tmp/desktop-release-plan.json

ADMIN_KEY='…' python3 .github/scripts/backfill-desktop-release-manifest.py \
  --plan /tmp/desktop-release-plan.json \
  --target-base-url https://omi-cf-edge-staging.<account>.workers.dev \
  --apply
```

The dry run reads the exact manifest from `https://api.omi.me` and validates its
canonical digest. Apply submits the reviewed plan through Edge/Jobs and calls
API Core's existing immutable registration contract. It never writes a channel
pointer. Stable promotion and the four macOS Beta control endpoints are
API Core/D1 CAS operations in staging. Production release-pipeline cutover
remains legacy-owned until its Firestore authority is projected and replayed.

Windows release history uses the same reviewed-only staging discipline but a
separate plan contract. Run `.github/scripts/windows_release_history.py` to
produce a deterministic plan, then submit it to
`/internal/windows-release-history/reviews`, explicitly apply it, and request
`/artifacts/apply` to queue the exact installer, blockmap, and `latest.yml`
mirror into `DESKTOP_UPDATES`. The Jobs consumer verifies bounded bytes,
SHA-256 and R2 metadata on every transfer. This does not promote a Windows
channel; release authority (which build is live) remains the D1 pointer/CAS
contract above.

Mirrored artifacts are served publicly from
`GET /v2/desktop/artifacts/{releaseId}/{assetName}` (Jobs worker, range
requests and immutable edge caching, digest-etagged, only ledger rows in
`status='copied'`). The macOS feeds and the Windows update feed rewrite their
download URLs to that route automatically once the exact asset is mirrored —
`PUBLIC_API_BASE_URL` on API Core mints the origin, and an unmirrored or
half-copied release keeps its reviewed GitHub source URL instead of 404ing.
electron-updater resolves installers relative to the feed directory, so a
mirrored `latest.yml` also routes the sibling `.exe`/`.blockmap` downloads
through R2.

Before applying D1 migrations, the release observes each exact database name and
UUID from its candidate inventory, validates the remote migration-name ledger as
an exact prefix, and uses the frozen authority configuration and SQL directory.
Ledger observations do not prove historical SQL hashes or old Worker compatibility;
the prior-schema qualification runner owns that additional evidence.

`smoke:staging` checks Edge health by default. To enable the authenticated
checks, provide a staging Better Auth token through an environment variable or
an explicit JSON token file:

```bash
CLOUDFLARE_SMOKE_TOKEN_FILE=/tmp/cf-auth-signup.json npm run smoke:staging
```

The authenticated smoke token must come from a Better Auth sign-up/sign-in
response. The staging-only `/auth-issue` bridge is useful for testing JWT
verification, but it deliberately signs an identity without creating a Better
Auth `user` row; an identity-only token therefore returns the expected `410`
from `/v1/users/profile` and is not a valid full-smoke fixture. Neither form of
token requires an OpenAI or Gemini provider secret.

Repeat smoke runs may reach the two-per-hour knowledge-graph rebuild fence.
That probe accepts a `429` only when Edge returns a numeric `Retry-After`
header; all non-rate-limited runs must still return the canonical `409` fence.

To deliberately exercise billable native TTS as part of that authenticated
smoke, add `CLOUDFLARE_SMOKE_NATIVE_TTS=1`; the check asserts a non-empty
`audio/mpeg` response and is opt-in.

The authenticated smoke verifies unauthenticated rejection, the D1 probe, the
account usage/subscription/price-catalog reads, conversation list/search,
chat-session/message management reads and missing-row mutations, fair-use
status, folder/memory shell dependencies, and the
conversation, enabled-app, and memory reads through Web `/api/proxy` so a
missing Web→Edge binding fails the release. It also sends one real default-text
chat through Web → Edge → Workers AI, validates the legacy SSE completion, and
deletes the smoke account's chat rows even when response validation fails. It
fails before writing unless the credential belongs to a dedicated account with
empty chat history, so cleanup cannot erase an operator's existing chat. This
chat check invokes one billable model inference per authenticated smoke. The
raw-audio Workers AI boundary still uses an empty body and does not invoke ASR
inference; use a separate explicit audio request for ASR quality or latency
qualification. This historical standalone smoke does not grant current-main
release eligibility; the fixed CF-4 and CI-1 runners must supply the complete
product acceptance contract. Standalone `smoke:staging` may run public-only checks.

The staging deployment script requires an already authenticated Wrangler session or a
scoped `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`; it never prints
secret values. It creates/applies only the isolated `omi-cf-*-staging`
resources.

Before exercising authenticated routes, configure secrets explicitly. The
values are read from stdin by Wrangler and are never committed:

```bash
cf_auth_secret="$(openssl rand -base64 48)"
printf '%s' "$cf_auth_secret" | npx wrangler secret put BETTER_AUTH_SECRET --name omi-cf-auth-staging
# This must be the public Web origin, not the Auth Worker's workers.dev origin.
printf '%s' "$BETTER_AUTH_URL" | npx wrangler secret put BETTER_AUTH_URL --name omi-cf-auth-staging
cf_internal_secret="$(openssl rand -base64 48)"
for worker_name in omi-cf-auth-staging omi-cf-edge-staging omi-cf-api-core-staging omi-cf-api-ai-staging omi-cf-realtime-staging omi-cf-jobs-staging; do
printf '%s' "$cf_internal_secret" | npx wrangler secret put INTERNAL_ASSERTION_SECRET --name "$worker_name"
done
# One independent, stable memory privacy HMAC key shared by Core and Jobs.
# Keep it across releases. Auth-key rotation must not rotate memory identities;
# replacing this key requires a coordinated receipt/row rekey migration.
cf_memory_privacy_secret="$(openssl rand -base64 48)"
for worker_name in omi-cf-api-core-staging omi-cf-jobs-staging; do
printf '%s' "$cf_memory_privacy_secret" | npx wrangler secret put MEMORY_PRIVACY_SECRET --name "$worker_name"
done
# Independent Edge-only HMAC pepper for BYOK enrollment fingerprints. Keep it
# stable; rotating it requires users to re-enroll their four provider keys.
cf_byok_fingerprint_pepper="$(openssl rand -base64 48)"
printf '%s' "$cf_byok_fingerprint_pepper" | npx wrangler secret put BYOK_FINGERPRINT_PEPPER --name omi-cf-edge-staging
# Optional isolated staging OAuth clients. A provider is not advertised until
# both values exist; never reuse production OAuth credentials here.
printf '%s' "$GOOGLE_CLIENT_ID" | npx wrangler secret put GOOGLE_CLIENT_ID --name omi-cf-auth-staging
printf '%s' "$GOOGLE_CLIENT_SECRET" | npx wrangler secret put GOOGLE_CLIENT_SECRET --name omi-cf-auth-staging
printf '%s' "$APPLE_CLIENT_ID" | npx wrangler secret put APPLE_CLIENT_ID --name omi-cf-auth-staging
printf '%s' "$APPLE_CLIENT_SECRET" | npx wrangler secret put APPLE_CLIENT_SECRET --name omi-cf-auth-staging
# Required only while imported Firebase password hashes still exist. These four
# values must come from the matching Firebase Auth export configuration.
printf '%s' "$AUTH_FIREBASE_SCRYPT_SIGNER_KEY" | npx wrangler secret put AUTH_FIREBASE_SCRYPT_SIGNER_KEY --name omi-cf-auth-staging
printf '%s' "$AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR" | npx wrangler secret put AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR --name omi-cf-auth-staging
printf '%s' "$AUTH_FIREBASE_SCRYPT_ROUNDS" | npx wrangler secret put AUTH_FIREBASE_SCRYPT_ROUNDS --name omi-cf-auth-staging
printf '%s' "$AUTH_FIREBASE_SCRYPT_MEM_COST" | npx wrangler secret put AUTH_FIREBASE_SCRYPT_MEM_COST --name omi-cf-auth-staging
# Optional, staging-only Flutter Better Auth bridge (never use in a release build).
cf_dev_issuer_secret="$(openssl rand -base64 48)"
printf '%s' "$cf_dev_issuer_secret" | npx wrangler secret put AUTH_DEV_ISSUER_SECRET --name omi-cf-auth-staging
# Independent staging-only credential for the Fair Use support/admin routes.
printf '%s' "$FAIR_USE_ADMIN_KEY" | npx wrangler secret put FAIR_USE_ADMIN_KEY --name omi-cf-api-core-staging
# Independent staging-only credential for desktop preview publish/delist.
printf '%s' "$DESKTOP_PREVIEW_PUBLISH_KEY" | npx wrangler secret put DESKTOP_PREVIEW_PUBLISH_KEY --name omi-cf-api-core-staging
# Admin-only Persona lookup/deletion uses the same header contract as legacy;
# provision this explicitly on API Core (service bindings do not share secrets).
printf '%s' "$ADMIN_KEY" | npx wrangler secret put ADMIN_KEY --name omi-cf-api-core-staging
# Beta release-pipeline credential and read-only GitHub evidence token. Keep
# these independent from ADMIN_KEY and scope the GitHub token to repository
# metadata/actions-artifact reads only.
printf '%s' "$BETA_PROMOTION_TOKEN" | npx wrangler secret put BETA_PROMOTION_TOKEN --name omi-cf-api-core-staging
printf '%s' "$GITHUB_TOKEN" | npx wrangler secret put GITHUB_TOKEN --name omi-cf-api-core-staging
# Team-only notification API key; configure on the Jobs Worker only.
printf '%s' "$ADMIN_KEY" | npx wrangler secret put ADMIN_KEY --name omi-cf-jobs-staging
# Keep the checked-in DLQ_REPLAY_STAGING_ENABLED=false default. During a
# controlled staging positive-probe window only, provision an independent
# 32-byte signing secret and explicitly enable the matching Worker var.
cf_dlq_replay_secret="$(openssl rand -base64 48)"
printf '%s' "$cf_dlq_replay_secret" | npx wrangler secret put DLQ_REPLAY_SIGNING_SECRET --name omi-cf-jobs-staging
# The probe itself reads these operator-only values from the environment; do
# not put them in .dev.vars or a Worker response/log.
# Independent staging-only credential for app tester and moderation routes.
printf '%s' "$APPS_ADMIN_KEY" | npx wrangler secret put APPS_ADMIN_KEY --name omi-cf-jobs-staging
# Required only when an isolated staging account has a registered FCM device.
# Use a staging Firebase service account; never copy production credentials.
printf '%s' "$FIREBASE_SERVICE_ACCOUNT_JSON" | npx wrangler secret put FIREBASE_SERVICE_ACCOUNT_JSON --name omi-cf-jobs-staging
# Required for checkout, customer portal, subscription upgrade/cancel,
# regular-billing webhook reconciliation, Stripe Connect onboarding/status,
# country specs, and deletion of a mapped subscription or connected account.
# Use the environment-matched restricted/secret key.
printf '%s' "$STRIPE_SECRET_KEY" | npx wrangler secret put STRIPE_SECRET_KEY --name omi-cf-jobs-staging
# Configure the Stripe endpoint as POST
# https://omi-cf-edge-staging.<account>.workers.dev/v1/stripe/webhook and use
# its environment-matched signing secret. During rotation, the previous secret
# is accepted only when it is provisioned explicitly.
printf '%s' "$STRIPE_WEBHOOK_SECRET" | npx wrangler secret put STRIPE_WEBHOOK_SECRET --name omi-cf-jobs-staging
printf '%s' "$STRIPE_WEBHOOK_SECRET_PREVIOUS" | npx wrangler secret put STRIPE_WEBHOOK_SECRET_PREVIOUS --name omi-cf-jobs-staging
# Connect uses a distinct endpoint/signing secret. The refresh secret signs
# browser GET callbacks that replace expired single-use Account Links; generate
# an independent high-entropy value instead of reusing an internal assertion.
printf '%s' "$STRIPE_CONNECT_WEBHOOK_SECRET" | npx wrangler secret put STRIPE_CONNECT_WEBHOOK_SECRET --name omi-cf-jobs-staging
printf '%s' "$STRIPE_CONNECT_WEBHOOK_SECRET_PREVIOUS" | npx wrangler secret put STRIPE_CONNECT_WEBHOOK_SECRET_PREVIOUS --name omi-cf-jobs-staging
stripe_connect_refresh_secret="$(openssl rand -base64 48)"
printf '%s' "$stripe_connect_refresh_secret" | npx wrangler secret put STRIPE_CONNECT_REFRESH_SECRET --name omi-cf-jobs-staging

# X connector. Keep the encryption secret stable; rotating it requires an
# explicit token re-encryption migration. RAPID_API_* are optional fallbacks.
printf '%s' "$X_OAUTH_CLIENT_ID" | npx wrangler secret put X_OAUTH_CLIENT_ID --name omi-cf-jobs-staging
printf '%s' "$X_OAUTH_CLIENT_SECRET" | npx wrangler secret put X_OAUTH_CLIENT_SECRET --name omi-cf-jobs-staging
printf '%s' "$X_OAUTH_REDIRECT_URI" | npx wrangler secret put X_OAUTH_REDIRECT_URI --name omi-cf-jobs-staging
printf '%s' "$X_TOKEN_ENCRYPTION_SECRET" | npx wrangler secret put X_TOKEN_ENCRYPTION_SECRET --name omi-cf-jobs-staging
printf '%s' "$RAPID_API_HOST" | npx wrangler secret put RAPID_API_HOST --name omi-cf-jobs-staging
printf '%s' "$RAPID_API_KEY" | npx wrangler secret put RAPID_API_KEY --name omi-cf-jobs-staging

# Task integrations. The encryption secret must remain stable; rotation needs a
# token re-encryption migration. Provider credentials must use staging OAuth
# applications whose redirect URIs are the matching Edge `/v2/integrations/*`
# callback routes.
printf '%s' "$TASK_INTEGRATION_TOKEN_ENCRYPTION_SECRET" | npx wrangler secret put TASK_INTEGRATION_TOKEN_ENCRYPTION_SECRET --name omi-cf-jobs-staging
printf '%s' "$TODOIST_CLIENT_ID" | npx wrangler secret put TODOIST_CLIENT_ID --name omi-cf-jobs-staging
printf '%s' "$TODOIST_CLIENT_SECRET" | npx wrangler secret put TODOIST_CLIENT_SECRET --name omi-cf-jobs-staging
printf '%s' "$ASANA_CLIENT_ID" | npx wrangler secret put ASANA_CLIENT_ID --name omi-cf-jobs-staging
printf '%s' "$ASANA_CLIENT_SECRET" | npx wrangler secret put ASANA_CLIENT_SECRET --name omi-cf-jobs-staging
printf '%s' "$GOOGLE_TASKS_CLIENT_ID" | npx wrangler secret put GOOGLE_TASKS_CLIENT_ID --name omi-cf-jobs-staging
printf '%s' "$GOOGLE_TASKS_CLIENT_SECRET" | npx wrangler secret put GOOGLE_TASKS_CLIENT_SECRET --name omi-cf-jobs-staging
printf '%s' "$CLICKUP_CLIENT_ID" | npx wrangler secret put CLICKUP_CLIENT_ID --name omi-cf-jobs-staging
printf '%s' "$CLICKUP_CLIENT_SECRET" | npx wrangler secret put CLICKUP_CLIENT_SECRET --name omi-cf-jobs-staging

# Google Calendar. A dedicated client is preferred, but the Jobs Worker falls
# back to the shared GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET used by Better Auth
# when the dedicated pair is absent. Whichever client is used must include this
# authorized redirect URI:
# https://omi-cf-edge-staging.<account>.workers.dev/v2/integrations/google-calendar/callback
# Enable the Google Calendar API (`calendar-json.googleapis.com`) in the same
# Google Cloud project before running the OAuth consent or Calendar API probe.
# Keep the encryption secret stable; rotation requires token re-encryption.
printf '%s' "$GOOGLE_CALENDAR_CLIENT_ID" | npx wrangler secret put GOOGLE_CALENDAR_CLIENT_ID --name omi-cf-jobs-staging
printf '%s' "$GOOGLE_CALENDAR_CLIENT_SECRET" | npx wrangler secret put GOOGLE_CALENDAR_CLIENT_SECRET --name omi-cf-jobs-staging
# If reusing the Better Auth OAuth client, provision the same pair on the Jobs
# Worker explicitly; service bindings do not share secrets between Workers.
if [ -n "${GOOGLE_CLIENT_ID:-}" ] && [ -n "${GOOGLE_CLIENT_SECRET:-}" ]; then
  printf '%s' "$GOOGLE_CLIENT_ID" | npx wrangler secret put GOOGLE_CLIENT_ID --name omi-cf-jobs-staging
  printf '%s' "$GOOGLE_CLIENT_SECRET" | npx wrangler secret put GOOGLE_CLIENT_SECRET --name omi-cf-jobs-staging
fi
printf '%s' "$GOOGLE_CALENDAR_TOKEN_ENCRYPTION_SECRET" | npx wrangler secret put GOOGLE_CALENDAR_TOKEN_ENCRYPTION_SECRET --name omi-cf-jobs-staging
```

The `/auth-issue` bridge is hidden (`404`) unless `AUTH_DEV_ISSUER_SECRET` is
configured. It accepts only a matching bearer secret and a bounded `uid`, then
uses Better Auth's JWT plugin to mint the same 24-hour token shape as the local
development bridge. The Flutter client enables this path only when both
`OMI_AUTH_SERVER_URL` and `OMI_AUTH_DEV_ISSUER_SECRET` are supplied to a
non-release build; do not put the issuer secret in a release build or commit it.
Point `OMI_AUTH_SERVER_URL` at the Auth Worker URL and `OMI_API_BASE_URL` at the
Edge Worker URL when exercising the app against staging.

### Firebase identity import

`scripts/import-firebase-identities.mjs` is the fail-closed Firebase Auth export
to Better Auth D1 migration tool. It preserves the Firebase uid, rejects
disabled/phone/custom-claim/unsupported identities, maps only password,
Google, and Apple sign-in authorities, and produces deterministic source,
configuration, and canonical user/account checksums. Imported password hashes
use a versioned envelope; the Auth Worker verifies the original Firebase
scrypt hash, then conditionally replaces it with a Better Auth native hash
after the first successful email sign-in. Wrong passwords and unsuccessful
sign-ins cannot trigger the write. Concurrent first logins are idempotent; a
transient D1 write failure preserves the verified envelope, emits bounded
fallback telemetry without identity or password data, and retries at the next
login. All new and reset passwords use Better Auth's native algorithm.

Before handling a protected export, run the credential-free schema/import
preflight. It applies the real Auth migrations to an in-memory SQLite/D1
equivalent and exercises apply → verify → replay plus changed-source conflict,
revoked projection, and account-deletion fence rejection. The fixture is fully
synthetic and makes no network requests:

```bash
npm run identity:dry-run
```

The command must report `status=passed`, `network_requests=0`, and every
admission check as `true`. It verifies importer mechanics only; it is not
evidence that a production Firebase export or provider configuration has been
reconciled.

Migration `0010_firebase_identity_provenance.sql` also stores a per-user
`sourceRecordSha256` digest (user plus imported account rows, never the source
data itself). The bridge requires this digest to be present and well-formed;
when upgrading an existing projection, rerun the same-source `apply` command
after the complete identity checksum has passed so nullable pre-0010 rows are
backfilled. A conflicting existing digest fails closed.

Both input files must be regular, non-symlinked files with mode `0600` or
stricter. The user export is bounded to 64 MiB and the hash configuration to
128 KiB; the reader also rejects malformed UTF-8 before JSON parsing. This is
an operator-side resource and source-integrity boundary, not a claim that a
large production export has already been replayed. Validate without network
access first:

```bash
npm run identity:import -- validate \
  --users /secure/firebase-users.json \
  --hash-config /secure/firebase-hash-config.json
```

`apply` and `verify` use Cloudflare's parameterized
[D1 query API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/),
so user values and password envelopes are never placed in shell arguments or a
temporary SQL file. Set `CLOUDFLARE_D1_DATABASE_ID` to the new empty Auth D1;
`apply` additionally requires `CLOUDFLARE_IDENTITY_IMPORT_CONFIRM` to equal that
exact ID. The token needs only D1 read/write on the isolated target account.
Any Google/Apple identity in the export also requires the matching complete
provider configuration in the environment before apply or verify.

```bash
export CLOUDFLARE_IDENTITY_IMPORT_CONFIRM="$CLOUDFLARE_D1_DATABASE_ID"
npm run identity:import -- apply \
  --users /secure/firebase-users.json \
  --hash-config /secure/firebase-hash-config.json
npm run identity:import -- verify \
  --users /secure/firebase-users.json \
  --hash-config /secure/firebase-hash-config.json
```

Run `verify` before routing sign-in traffic to the imported D1. It proves the
exact pre-cutover source image; after a user signs in or changes a password,
the intentional native hash and `updatedAt` changes mean that exact source
checksum is no longer a post-cutover health check.

Migration `0004_identity_import_ledger.sql` claims one immutable source before
writing. Deterministic `INSERT OR IGNORE` batches accept only an exact planned
subset, so an interrupted request can replay the same source; conflicting rows,
nonzero sessions, a different source, or a final checksum mismatch fail closed.
Keep all four Firebase scrypt secrets configured until
`SELECT COUNT(*) FROM account WHERE password LIKE 'firebase-scrypt-v1$%'`
returns zero; native hashes remain usable after those migration-only secrets
are removed.
The current non-empty shared staging Auth D1 is not an import target. A real
Firebase export remains a separately approved production-data operation.

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

The AI and realtime paths are API-first; no ASR/model process runs inside a
Worker. Realtime, transcription, chat, embeddings, translation and TTS use
Cloudflare's native Workers AI binding and require no OpenAI, Gemini, generic
`AI_API_KEY`, or external ASR key. The staging configuration intentionally
leaves those secrets unset. Unsupported codecs or formats return a typed
failure; they do not silently fall back to an external AI provider. The Python
Workers use Cloudflare's native `workers.fetch` only for explicitly selected
business APIs (for example Google Calendar), not for the AI inference path.

The repository still contains compatibility-only external-provider adapters so
that a future, separately approved migration window can fail closed at its
boundary. They are disabled for this deployment and are not setup steps. Do
not run `wrangler secret put` for `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`AI_API_KEY`, `ASR_API_KEY`, or an external AI base URL for the native path.

`/v1/stt/transcribe-async` is an additive staging route for clients that can
send a raw audio body. It accepts at most 5 MB, stages the bytes under an
uid-scoped temporary R2 key, records an idempotent D1 job, and lets the Queue
consumer run native Workers AI Whisper. The object is deleted after a terminal
result; the poll response contains only the bounded normalized transcription.
Before completion, the consumer unions the result segment/word intervals and
upserts one `sync_fresh` Fair Use source keyed by job ID, so Queue redelivery
cannot double-count speech. A D1 meter failure keeps the job retryable and does
not publish an unmetered completed result.
`/v2/sync-local-files` is separately owned by the Jobs Worker in isolated
staging. Multipart uploads are parsed one file at a time, hashed into a
45-day content ledger, and staged under `cf-sync/` in R2. The consumer decodes
the native length-prefixed Opus/PCM WAL format in Worker-compatible WASM,
emits bounded two-minute WAV windows, and sends those windows to Workers AI
Whisper with VAD. Successful timed segments are merged into the uid-scoped D1
conversation projection with an updated-at compare-and-swap, then summarized
through Workers AI and recorded in both account-usage and exact Fair Use
sources. Silence is a successful terminal outcome. Missing timestamps,
provider errors, summary errors, or D1 meter errors remain retryable and never
publish a false `completed` response.

Recent capture is accepted into the independent `sync_fresh` queue only when a
short-lived server-signed manifest binds uid, device, conversation, filenames,
and digests. Everything else enters `sync_backfill`, which allows one in-flight
job per uid, four concurrent consumers, a 30-day lookback, and daily user/global
processed-speech caps. Queue acknowledgement ambiguity leaves the pollable D1
job and R2 bytes intact for the five-minute reconciler. Completed content
replays return the ledger result without re-running AI or double-counting
speech; partial/failed work keeps the ledger retryable so the client retains
its WAL. The mobile uploader sends no more than two WALs per request because a
single WAL can reach 32 MiB and Cloudflare Free/Pro request bodies are capped at
100 MB.

Wrangler aliases `opus-decoder` to its pinned non-Web-Worker core entrypoint.
The package's public entrypoint eagerly loads an optional browser Worker class,
which Cloudflare isolates do not provide; the alias keeps the same libopus WASM
decoder without introducing a Node process or a browser Worker dependency.
Workers AI JSON Mode returns the schema payload under a `response` object, while
older text-generation responses can contain a JSON string there; the Jobs
parser accepts both shapes. Conversation compare-and-swap writes use SQL
`RETURNING id` instead of `D1Result.meta.changes`, because the conversation FTS5
triggers legitimately inflate the latter above one for a single matched row.

Live staging evidence on 2026-08-29 used a generated 16 kHz mono PCM WAL against
Jobs version `55c1aba5-2eec-41dc-bb8f-1fabd308f9ce`. A manifest-bound recent
upload completed on `sync_fresh`; an exact replay completed immediately with
zero Queue attempts and no second Fair Use source; a seven-hour upload completed
on `sync_backfill`; and a 31-day upload returned
`backfill_lookback_exceeded` without creating a job. Both successful paths
produced two timed transcript segments, a structured title, one 6740 ms exact
speech source, and no remaining R2 staging object. The temporary D1 rows and
Better Auth accounts used by this verification were deleted after the checks.

This staging path intentionally normalizes Whisper output to a single speaker;
speaker diarization and the legacy finalization-trigger integration path remain
explicit parity gaps. App-key conversation ingest has its own migrated durable
integration fanout. Neither path calls the monolithic Python backend or runs a
local ASR process.

Browser WebSockets cannot attach an `Authorization` header during the HTTP
upgrade. Edge therefore signs a random, 30-second, one-use bootstrap for
`/v4/web/listen`; the isolated Durable Object accepts only that bootstrap and
verifies the first `{type: "auth", token: ...}` message through the Auth service
binding before opening the ASR provider socket. Binary audio before successful
authentication is rejected, and no two browser connections share a default DO
session. Header-authenticated native realtime routes retain their existing
upgrade contract. External provider messages are forwarded unchanged. Native
Nova-3 final events are normalized into Omi speaker-segment arrays for current
Web, Flutter, and desktop consumers; Blob/typed-array client frames are
converted to binary `ArrayBuffer` before provider forwarding. Provider `Error`
events fail closed instead of leaving a silent session. Final word intervals
are also unioned into one revisioned D1 `realtime` source per provider
connection. D1 failures store the latest snapshot in Durable Object storage and
retry it from an alarm; connection or audio duration is never substituted for
detected speech.

Do not point these commands at production names from this worktree. The
staging smoke surface is:

```text
GET  /health                  public Edge liveness
GET  /ready                   Edge → all internal dependency readiness
GET  Web /api/worker-ready    Web → Edge service-binding readiness
POST /api/auth/sign-up/email
                              Better Auth + D1
GET  /v1/cf/probe             Edge → Auth → Python API Core → D1
POST /v1/stt/transcribe      Edge → Python API AI → hosted ASR API
POST /v1/stt/transcribe-workers-ai
                              Edge → Python API AI → Workers AI binding (raw audio)
POST /v2/voice-message/transcribe
                              Edge → Python API AI → Workers AI binding (Web/Flutter multipart or desktop PCM)
POST /v2/realtime/session     Edge → explicit 409 live-model capability denial (CF-4 owner pending)
POST /v2/realtime/usage       Edge → Python API AI → D1 usage projection
POST /v1/stt/transcribe-async
                              Edge → Jobs → R2 → Queue → Workers AI Whisper
GET  /v1/stt/transcribe-async/{jobId}
                              Edge → Jobs → uid-scoped D1 result
POST /v2/sync-capture-manifest
                              Edge → Jobs → device/conversation-bound signed proof
POST /v2/sync-local-files     Edge → Jobs → R2 → fresh/backfill Queue → Workers AI
GET  /v2/sync-local-files/{jobId}
                              Edge → Jobs → uid-scoped D1 sync result
POST /v1/embeddings
POST /v1/embeddings-workers-ai
                              Edge → Python API AI → Workers AI BGE binding
POST /v1/translate           Edge → Python API AI → Workers AI m2m100 translation
POST /v1/tts/synthesize      Edge → Python API AI → Workers AI Aura binding
POST /v1/tts/synthesize-workers-ai
                              Edge → Python API AI → Workers AI Aura binding
POST /v2/tts/synthesize      Edge → Python API AI → Cloudflare unified ElevenLabs model
GET  /v1/auto/model-pick    Edge → Python API AI → Workers AI model policy + D1 cache
GET/POST /v1/ai/*           staging-disabled legacy compatibility surface; new client uses Workers AI routes
WS   /v4/listen               Edge → Realtime → Durable Object → Workers AI streaming path (external fallback optional)
WS   /v4/web/listen           Edge bootstrap → first-message ticket → isolated DO → Workers AI streaming path (external fallback optional)
R2   /v1/cf/assets/{key}      Edge → Python API Core → R2 + D1 metadata/checksum
JOB  /v1/cf/jobs              Edge → Jobs Worker → Queue → idempotent D1 ledger
GET  /v1/cf/jobs/{jobId}      Edge → Jobs Worker → uid-scoped D1 job status
POST /v1/cf/conversations     Edge → Python API Core → D1 idempotent projection upsert
GET  /v1/cf/conversations     Edge → Python API Core → D1 conversation projection
GET  /v1/cf/conversations/count
GET  /v1/cf/conversations/{conversationId}
                              bounded list/count/detail reads with uid isolation
GET  /v1/conversations       Edge → Python API Core → D1 canonical conversation list
GET/POST /v1/advice
PATCH/DELETE /v1/advice/{adviceId}
POST /v1/advice/mark-all-read
                              uid-scoped D1 proactive coaching CRUD and durable
                              read/dismiss state
GET/POST/DELETE /v3/memories
POST  /v3/memories/batch
PATCH/DELETE /v3/memories/{memoryId}
PATCH /v3/memories/{memoryId}/visibility
PATCH /v3/memories/{memoryId}/read
PATCH /v3/memories/{memoryId}/baseline
POST  /v3/memories/{memoryId}/review
DELETE /v3/memories/batch
POST  /v3/memory-imports/batch
                              uid-scoped canonical D1 memory CRUD for isolated
                              Better Auth staging accounts; deletes tombstone
                              and there is no Firestore fallback or dual write;
                              import batches persist bounded, deduplicated
                              evidence rows without extraction or promotion
GET  /v1/account/cutover/control
                              Edge → Python API Core → D1 account migration control projection
GET  /v1/users/me/usage       Edge → Python API Core → D1 idempotent usage sources
GET  /v1/users/me/subscription
                              Edge → Python API Core → D1 subscription/usage projection
GET  /v1/payments/available-plans
                              Edge → Python API Core → configured D1 price catalog
GET  /v1/fair-use/status      Edge → Python API Core → D1 fair-use read projection
GET  /v1/fair-use/case/:ref/status
                              Edge → Python API Core → public D1 case projection
GET  /v1/conversations/count
POST /v1/conversations/search
GET  /v1/conversations/{conversationId}
DELETE /v1/conversations/{conversationId}
                              canonical read/search projection and non-cascade delete;
                              finalization, reprocess, and merge are D1/Queue-owned;
                              merge copies bounded R2 audio metadata before atomic cleanup
GET  /v1/conversations/{conversationId}/shared
PATCH /v1/conversations/{conversationId}/visibility
                              public D1 share index and privacy-redacted read
GET  /v1/conversations/{conversationId}/photos
                              bounded photo projection; locked rows fail closed
GET  /v1/conversations/{conversationId}/transcripts
GET  /v1/conversations/{conversationId}/analytics
                              bounded transcript buckets and D1 speaker analytics
GET  /v1/conversations/{conversationId}/suggested-apps
                              approved app catalog projection with uid-scoped state
GET  /v1/conversations/{conversationId}/recording
                              R2 head check for Worker playback or imported uid/{id}.wav
POST /v1/sync/audio/{conversationId}/precache
                              Edge → Jobs → D1/R2 inventory → Queue; idempotently
                              rebuilds missing legacy playback WAVs from copied chunks
GET  /v1/sync/audio/{conversationId}/urls
GET  /v1/sync/audio/{conversationId}/{audioFileId}
                              uid-scoped R2 playback metadata, one-hour HMAC URLs,
                              authenticated fallback, and single-range WAV streaming
GET  /v3/speech-profile
GET  /v4/speech-profile
GET  /v3/speech-profile/status
POST /v3/upload-audio
GET/DELETE /v3/speech-profile/expand
GET  /v3/speech-profile/audio
                              isolated biometric R2 profile/sample ownership,
                              Workers AI speech validation, 60-second signed URLs,
                              and single-range PCM WAV streaming
GET  /v1/conversations/{conversationId}/action-items
GET  /v1/conversations/{conversationId}/action-items/count
                              standalone D1 action-item projection; locked rows fail closed
PATCH /v1/conversations/{conversationId}/segments/text
                              bounded D1 transcript edit with updated-at CAS
PATCH /v1/conversations/{conversationId}/segments/{segmentIdx}/assign
PATCH /v1/conversations/{conversationId}/assign-speaker/{speakerId}
                              D1 speaker assignment with updated-at CAS;
                              speech-training bulk assignment remains legacy
PATCH /v1/conversations/{conversationId}/events
PATCH /v1/conversations/{conversationId}/summary
                              structured event flags and default/app summaries → D1
DELETE /v1/conversations/{conversationId}/calendar-event
POST /v1/conversations/{conversationId}/calendar-event
POST /v1/conversations/{conversationId}/calendar-event/auto-link
                              local link/remove and overlap matching → Jobs → D1 + Google Calendar API
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
                              Edge → Python API Core → D1 + per-user dismissal; Better Auth
                              identity is required before dispatch. Register /pending before the
                              public /:announcementId matcher so the signed Core context survives.
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
                              MCP credentials remain on dedicated routes
GET  /v1/approved-apps         Edge → Python API Core → approved public app D1 projection
GET  /v1/apps/popular          Edge → Python API Core → popular public app D1 projection
GET  /v1/apps                  Edge → Python API Core → public + owned + tester-assigned apps
GET  /v1/apps/tester/check     Edge → Python API Core → D1 tester membership
POST /v1/apps                  Edge → Jobs → multipart + R2 logo + D1/Stripe mapping
PATCH /v1/apps/{appId}         Edge → Jobs → owner-only D1/R2/Stripe update
PATCH /v1/apps/{appId}/change-visibility
                              Edge → Jobs → owner-only D1 visibility update
POST /v1/apps/{appId}/refresh-manifest
                              Edge → Jobs → bounded HTTPS manifest refresh → D1
DELETE /v1/apps/{appId}        Edge → Jobs Queue → provider-safe app deletion
POST/GET /v1/apps/{appId}/keys
DELETE /v1/apps/{appId}/keys/{keyId}
                              Edge → Jobs → owner-only one-time app API keys in D1
POST/GET /v1/mcp/keys
DELETE /v1/mcp/keys/{keyId}
                              Edge → Jobs → uid-scoped one-time MCP API keys in D1
POST/GET /v1/dev/keys
DELETE /v1/dev/keys/{keyId}
                              Edge → Jobs → uid-scoped one-time Developer API keys in D1
GET  /v1/dev/user/memories
GET  /v1/dev/user/memories/vector/search
GET  /v1/dev/user/action-items
GET  /v1/dev/user/folders
GET  /v1/dev/user/conversations
GET  /v1/dev/user/conversations/{conversationId}
GET  /v1/dev/user/goals
GET  /v1/dev/user/goals/{goalId}
GET  /v1/dev/user/goals/{goalId}/history
                              Edge → Python API Core → Developer-key D1/Vectorize reads
POST /v1/dev/user/memories
POST /v1/dev/user/memories/batch
PATCH/DELETE /v1/dev/user/memories/{memoryId}
POST /v1/dev/user/action-items
POST /v1/dev/user/action-items/batch
PATCH/DELETE /v1/dev/user/action-items/{actionItemId}
                              Edge → Python API Core → D1 + vector outbox writes
PATCH/DELETE /v1/dev/user/conversations/{conversationId}
                              Edge → Python API Core → D1 + vector outbox writes
POST /v1/dev/user/goals
PATCH/DELETE /v1/dev/user/goals/{goalId}
PATCH /v1/dev/user/goals/{goalId}/progress
                              Edge → Python API Core → D1 goal writes
GET  /v1/apps/{appId}/logo/{version}
                              Edge → Jobs → immutable current-logo R2 object
POST /v1/apps/tester
POST/DELETE /v1/apps/tester/access
GET  /v1/apps/public/unapproved
PATCH /v1/apps/{appId}/popular
POST /v1/apps/{appId}/approve
POST /v1/apps/{appId}/reject  Edge → Jobs → independent admin key + D1/outbox
POST /v1/integrations/notification
POST /v2/integrations/{appId}/user/conversations
POST /v2/integrations/{appId}/user/memories
GET  /v2/integrations/{appId}/memories
GET  /v2/integrations/{appId}/conversations
POST /v2/integrations/{appId}/search/conversations
POST /v2/integrations/{appId}/notification
GET  /v2/integrations/{appId}/tasks
                              Edge → Python API Core → app-key D1/Workers AI authority
GET/PUT/DELETE /v1/integrations/google_calendar
GET  /v1/integrations/google_calendar/oauth-url
GET  /v2/integrations/google-calendar/callback
GET  /v1/calendar/google/events
POST /v1/tools/calendar-events
                              Edge → Jobs → encrypted D1 grant + Google Calendar API
POST /v1/import/limitless    Edge → Jobs → bounded ZIP/R2 staging → D1/Queue
GET  /v1/import/jobs         Edge → Python API Core → uid-scoped D1 import jobs
GET  /v1/import/jobs/{jobId}
POST /v1/import/jobs/{jobId}/cancel
DELETE /v1/import/jobs/{jobId}
                              Limitless import lifecycle is staging-owned; historical
                              Firestore import jobs remain outside this projection
DELETE /v1/import/limitless/conversations
POST /v1/staged-tasks/migrate
POST /v1/staged-tasks/migrate-conversation-items
POST /v1/action-items/restore-legacy-conversation-items
                              Edge → Python API Core → authenticated inert compatibility
                              responses; retired Firestore migration behavior is not revived
GET  /v2/apps                  Edge → Python API Core → paginated/grouped public app D1 projection
GET  /v2/apps/capability/{capability_id}/grouped
                              Edge → Python API Core → capability/category D1 projection
GET  /v2/apps/search
                              Edge → Python API Core → authenticated D1 search/filter projection
GET  /v1/apps/enabled          Edge → Python API Core → uid-scoped D1 install projection
POST /v1/apps/enable           Edge → Python API Core → idempotent free-app D1 install
POST /v1/apps/disable          Edge → Python API Core → uid-scoped D1 uninstall
POST /v1/apps/review
PATCH /v1/apps/{appId}/review
PATCH /v1/apps/{appId}/review/reply
GET  /v1/apps/{appId}/reviews
                              Edge → Python API Core → D1 public-app reviews
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
DELETE /v1/users/store-recording-permission
                              Edge → Jobs → D1 intent + paged R2 cleanup
GET  /v1/users/private-cloud-sync
POST /v1/users/private-cloud-sync
GET  /v1/users/training-data-opt-in
POST /v1/users/training-data-opt-in
POST /v1/users/fcm-token
DELETE /v1/users/fcm-token   Edge → Python API Core → uid/device-scoped D1 delete (idempotent)
ANY  /v1/users/developer/webhook/*
GET  /v1/users/developer/webhooks/status
GET  /v1/users/notification-settings
PATCH /v1/users/notification-settings
GET  /v1/users/daily-summary-settings
PATCH /v1/users/daily-summary-settings
GET  /v1/users/daily-summaries
POST /v1/users/daily-summaries
GET  /v1/users/daily-summaries/{summaryId}
PATCH /v1/users/daily-summaries/{summaryId}/visibility
DELETE /v1/users/daily-summaries/{summaryId}
GET  /v1/daily-summaries/{summaryId}/shared
POST /v1/users/daily-summary-settings/test
POST /v1/users/daily-summaries/{summaryId}/regenerate
                              Edge → Python API Core → D1 daily-summary projection;
                              public shares fail closed on private or ambiguous IDs;
                              generation is deterministic until the LLM/notification owner moves
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
GET  /v1/users/export          Edge → Python API Core → D1 user-data projections
POST/DELETE /v1/users/me/byok-active
                              Edge → D1 HMAC fingerprint enrollment
GET/DELETE /v2/messages        Edge → Python API Core → D1 chat-history projection
POST /v2/messages/share
GET  /v2/messages/shared/{token}
                              Edge → Python API Core → D1 30-day chat share
POST /v2/messages              Edge → Python API AI → Workers AI or validated
                              user OpenAI API + D1 text-chat exchange
POST /v1/initial-message
POST /v2/initial-message
POST /v2/chat/initial-message
POST /v2/chat/generate-title
                              Edge → Python API AI → Workers AI + D1 chat helper
GET/POST /v1/action-items      Edge → Python API Core → D1
GET  /v1/action-items/ids      Edge → Python API Core → D1
GET  /v1/action-items/search   Edge → Python API Core → Workers AI + Vectorize + D1
PATCH /v1/action-items/batch   Edge → Python API Core → D1
POST  /v1/action-items/batch   Edge → Python API Core → D1
POST /v1/action-items/batch-delete
                              Edge → Python API Core → D1
GET  /v1/action-items/pending-sync
PATCH /v1/action-items/sync-batch
                              Edge → Python API Core → D1 Apple Reminders projection
POST /v1/action-items/share
GET  /v1/action-items/shared/{token}
POST /v1/action-items/accept
                              Edge → Python API Core → D1 30-day share + atomic claim
GET/PATCH/DELETE /v1/action-items/{actionItemId}
                              Edge → Python API Core → D1
PATCH /v1/action-items/{actionItemId}/completed
                              Edge → Python API Core → D1
GET  /v1/tools/conversations
POST /v1/tools/conversations/search
POST /v1/tools/conversations/search-chunks
GET  /v1/tools/memories
POST /v1/tools/memories/search
GET/POST /v1/tools/action-items
PATCH /v1/tools/action-items/{actionItemId}
                              Edge → Python API Core → D1/Vectorize tool envelope
GET  /v1/agent/tools
POST /v1/agent/execute-tool
                              Edge → Python API Core → D1-backed first-party tool directory/executor;
                              third-party provider tools remain fail-closed until their Worker adapters land
GET/DELETE /v1/knowledge-graph
GET /v1/knowledge-graph/canonical
POST /v1/knowledge-graph/rebuild
POST /v1/knowledge-graph/extract
                              Edge → Python API Core → canonical D1 memory graph;
                              extraction uses Workers AI and never writes product state
POST /v1/memories/extract
POST /v1/conversations/topic
POST /v1/connectors/synthesize
POST /v1/users/ai-profile/synthesize
                              Edge → Python API Core → Workers AI return-only synthesis;
                              no local model, legacy backend, or product-state write
GET /v1/goals/suggest
GET /v1/goals/advice
GET /v1/goals/{goalId}/advice
POST /v1/goals/extract-progress
                              Edge → Python API Core → D1 + Workers AI/Vectorize;
                              extracted progress atomically updates goal/event/history
POST /v1/chat-first/blocks/validate
                              Edge → Python API Core → D1 capability/entity checks;
                              bounded block union with retry-stable opaque IDs, no chat-state write
POST /v1/chat/deferrals     Edge → Python API Core → D1 idempotent deferral outbox
                              24-hour due receipt, generation/continuity fence, no chat-state write
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
POST /v1/workflow-migrations/task-goal-links
                              Edge → Python API Core → D1 task/goal projection + receipt
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
PUT  /v1/integrations/apple-health/sync
PUT/DELETE /v1/integrations/apple_health
                              Edge → Python API Core → D1 Apple Health projection
GET  /v1/trends              Edge → Python API Core → D1 global trend projection
```

The calendar meeting routes use a uid-scoped natural key
(`calendar_source` + `calendar_event_id`) and store bounded metadata in D1.
They are staging-only until the legacy conversation context reader is migrated
to the same authority. Jobs separately owns the Google Calendar OAuth grant and
event discovery; conversation finalization, reprocess, and merge are now
Cloudflare-owned lifecycle boundaries.

`GET /v1/trends` reads the global category/topic projection from D1. Categories
are limited to the five released trend families and topics are returned in
descending `memories_count` order without exposing the underlying memory IDs.
Use the reviewed D1 backfill generator for the initial Firestore export; the
request path has no Firestore or per-user dependency.

`POST /v1/chat-first/blocks/validate` is staging-only and validates the
main-chat block contract against the isolated account's D1 entities. It is a
pure capability check: the Worker does not generate prompts, persist chat
messages, or fall back to the legacy backend. A missing entity, stale account
generation, cold-start subject, or incomplete cutover returns a typed rejection.

`POST /v1/chat/deferrals` is the matching staging-only kernel outbox boundary.
It stores a bounded question projection in D1 with a stable deferral identity
and returns the same due receipt on retry. Prompt materialization, provider
calls, and chat-row writes are intentionally outside this route.

Only routes explicitly listed as migrated are sent to the partial Worker
implementations. Authenticated routes that are not yet migrated use
`ORIGIN_BACKEND_URL` when configured; staging without that binding returns
`404 route not migrated` instead of silently treating the partial Worker as the
owner.

The destructive `DELETE /v1/users/store-recording-permission` operation is
staging-owned by Jobs. Admission atomically disables recording storage and
persists a uid-scoped cleanup intent before returning the legacy `{status: ok}`
shape. The queue worker deletes the dedicated conversation-recordings prefix,
all private-sync/playback/chunk prefixes, audio-only generic assets, and their
D1 metadata in bounded pages. Recording reads fail closed as soon as the D1
intent exists; a lease, exponential retry, scheduled reconciler, and two zero
scans make the cleanup resumable without treating queue delivery as authority.
Re-enabling storage is fenced until cleanup completes. This ownership remains
staging-only until legacy GCS recordings have completed the R2 copy/checksum
cutover described by `manifests/r2-namespaces.yaml`.

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
FCM/reminder delivery, or legacy conversation-item restoration; those side
effects remain on the legacy owner until their separate contracts move. Task
sharing is D1-owned in staging: the share snapshot has a 30-day expiry, public
previews expose only sender name/description/due date, and acceptance plus task
copies execute in one D1 batch guarded by a unique recipient claim. Better Auth
display names travel only inside the signed, request-bound Edge assertion.
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
screen-activity vector lifecycle and paid semantic search remain on the legacy
owner until their contracts move. MCP memory, conversation, and action-item
semantic search use separate staging Vectorize projections described below.

`GET /v1/crisp/unread` is now a staging-owned support-message read. The Edge
authenticates the Better Auth session, API Core resolves only the caller's
non-sensitive profile email through the Auth Worker, and the Worker fetches the
matching conversation/messages from Crisp. Missing Crisp configuration, profile,
or conversation preserves the legacy empty response; provider JSON failures are
reported as a bounded `502` without logging credentials or message data.

`GET /v1/integrations/{app_key}` now reads the connection bit from the
Cloudflare Calendar/task-integration projections. It returns only the
uid-scoped boolean (including Gmail's Calendar grant scope) and treats unknown
integration keys as disconnected without exposing provider credentials.

`DELETE /v1/integrations/{app_key}` now has the same staging-owned boundary for
the Google Calendar grant and its `gmail` alias. Unknown providers return a
bounded `404` until their provider-specific D1 disconnect contract is migrated;
the route never falls back to the legacy generic Firestore handler.

`PUT /v1/integrations/{app_key}` now accepts the canonical `google_calendar`
grant (including the hyphenated spelling) through the Jobs Worker. The generic
boundary rejects other providers with a bounded `404` until a provider-owned D1
projection exists; credentials are encrypted before they are persisted.

Apple Health is device-pushed rather than OAuth-backed. The iOS sync payload is
bounded and stored in the uid-scoped `cf_apple_health` D1 projection; the
follow-up connection save and disconnect calls are also routed explicitly to
API Core so they cannot fall back to the legacy generic integration handler.

`GET /v1/integrations/{app_key}/oauth-url` is also staging-owned by the Jobs
Worker for the Google Calendar grant and its derived aliases (`gmail`,
`google_mail`, `email`, `contacts`, and `google_contacts`). It stores a hashed,
single-use state in D1 and preserves the legacy `400` response for unsupported
providers; the exact `google_calendar` route remains the canonical match.

`npm run backfill:d1 -- --input export.ndjson` generates reviewed D1-ingestion
SQL from newline-delimited records. Every record must name one of the
whitelisted D1 tables (including `cf_conversations` and the provider-only
`cf_app_payment_links` mapping) and carries
`{ "table": "cf_action_items", "row": { ... } }`;
the generator validates uid/id, normalizes timestamps/booleans/JSON, escapes SQL,
and uses uid+id upserts. Input is capped at 64 MiB and decoded as fatal UTF-8;
oversized or malformed exports fail before any JSON/SQL processing. It only
writes SQL to stdout. Review the output and
apply it explicitly to the isolated staging database with Wrangler `--file`;
the command does not connect to Firestore or production by itself. Generated
files intentionally omit manual `BEGIN`/`COMMIT`: [D1 remote import is already
transactional and rejects embedded transaction statements](https://developers.cloudflare.com/d1/best-practices/import-export-data/).

Legacy X posts have an additional explicit-user export boundary. Run the
exporter only in the source environment that already has authorized Firestore
credentials; it will not scan all users and requires every uid to be named:

```bash
cd backend
uv run python scripts/export_cloudflare_x_posts.py \
  --uid 'EXACT_SOURCE_UID' \
  --output /secure/operator-directory/x-posts.jsonl

cd ../deploy/cloudflare
umask 077
npm run backfill:d1 -- \
  --input /secure/operator-directory/x-posts.jsonl \
  > /secure/operator-directory/x-posts.sql
npx wrangler d1 execute omi-cf-app-staging \
  --remote --file /secure/operator-directory/x-posts.sql
```

The exporter writes a new mode-0600 file, refuses overwrites and more than 5,000
rows, emits only a row-count/checksum summary, and whitelists post fields so
OAuth material and unknown Firestore fields cannot cross the boundary. The
JSONL and reviewed SQL both contain user-authored text: never commit them, and
remove the exact operator-owned files after reconciliation. Each `cf_x_posts`
upsert and its `x_post` vector-projection outbox entry share one transaction;
the Jobs Worker then rebuilds the BGE-M3 projection, while list and search still
hydrate from uid-scoped D1. Production import additionally requires the
approved identity/cutover mapping and must target production D1 explicitly;
staging validation uses isolated synthetic accounts instead of production data.

`CLOUDFLARE_SMOKE_TOKEN_FILE=/path/to/staging-token.json npm run benchmark:staging`
warms and samples six non-mutating staging endpoints, reporting p50/p95/max and
an optional p95 budget (`CLOUDFLARE_BENCHMARK_P95_MS`, default 4 seconds). It is
report-only by default; set `CLOUDFLARE_BENCHMARK_ENFORCE=1` to make a budget
exceedance fail the command.

For a provider-backed Calendar check, use only a disposable staging account and
a short-lived Google access token:

```bash
CLOUDFLARE_CALENDAR_PROBE_BEARER_TOKEN='…' \
CLOUDFLARE_CALENDAR_PROBE_ACCESS_TOKEN='…' \
CLOUDFLARE_CALENDAR_PROBE_CONFIRM=1 \
npm run calendar:positive-probe
```

The probe verifies encrypted integration save, Google events read, and cleanup;
it never creates an event, prints the access token, or targets production. It
does not replace the interactive Google OAuth callback probe, which still
requires the staging client ID/secret and registered callback URI.

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
as one batch. Relationship `detach` now clears uid-scoped action-item and
workstream links in the same D1 batch, preserving the legacy 450-link safety
bound and the goal mutation receipt; AI advice/suggestion use the Workers AI
route in `goal_ai_routes.py`. Focus/unfocus/lifecycle writes require
`Idempotency-Key` and `X-Account-Generation`; the five-slot focus cap and
replacement rule are enforced in a D1 batch. This route group is staging-only
pending historical goal/event backfill and downstream reader cutover.

The workstream routes migrate canonical workflow metadata, journal events,
artifact descriptors, continuation checkpoints, and task/goal-origin work
intents to D1. The task-goal link migration preserves imported/unchanged/failed
outcomes and records a generation-scoped idempotency receipt. Mutating operations use generation-scoped idempotency receipts;
artifact revisions and checkpoints enforce their monotonic version/sequence
rules. Workstream search/index refresh and candidate automation remain outside
the current native scope. The goal detail reader now composes the bounded
goal/workstream/task/progress-event projections in D1; relationship detach is
part of the same D1 authority and AI advice/suggestion run through Workers AI.
This group is staging-only pending historical workstream backfill and
downstream reader cutover.

The R2 asset route stores a SHA-256 integrity projection in D1 alongside the
uid-scoped object metadata. Uploads can supply `X-Content-SHA256` for fail-closed
verification; downloads support one `bytes` range and `If-None-Match`, while
multi-range and unsatisfiable requests return `416`. Logical asset keys point
to immutable R2 storage keys, so an overwrite cannot destroy the previous
object before its D1 pointer commits. Superseded, deleted, or uncommitted
objects are tracked in D1 and retried by the Jobs Worker's 15-minute cleanup
sweep. R2 reads stream through the Python ASGI response; ordinary uploads retain
the bounded 25 MB surface. Larger clients can use the authenticated
`action=mpu-create`, `action=mpu-uploadpart`, `action=mpu-complete`, and
`action=mpu-abort` operations on the same route. D1 records the uid-scoped
upload/part ledger and completion verifies the assembled SHA-256 before the
canonical pointer is committed; failed completion is sent to the existing R2
cleanup ledger. Presigned direct-to-R2 URLs remain intentionally unexposed
until a client actually needs that boundary.

The folder routes migrate system/custom folder metadata and ordering to D1.
Folder conversation listing and single-conversation moves now use the D1
conversation projection and refresh non-discarded folder counts transactionally.
The bulk move route validates every uid-scoped conversation, rejects locked or
missing rows, and updates all selected conversations plus folder counts in one
D1 batch. Folder deletion side effects remain staging-only until the
conversation authority moves in production.

The chat history and desktop persistence routes use explicit uid/app/session-
scoped D1 projections. Empty history returns a deterministic Worker-owned
greeting. `/v2/messages` GET/POST/DELETE share the canonical D1 target resolver:
explicit `chat_session_id` requires caller ownership (missing/foreign is 404)
and the selected session determines its app, even when a conflicting `app_id`
is supplied. Omitted IDs select the latest app-scoped session; an unmigrated
principal with no session keeps its legacy app-scoped history. Offset pages past
empty history return `[]`. Explicit clear keeps the selected session and resets
count/preview; default clear removes only the selected session and its messages;
desktop scoped deletes update retained session counts in the same D1 batch.
Session create/list/read/update/delete, starred filtering,
reported-message hiding, idempotent `client_message_id` retries, and monotonic
desktop journal revisions are Worker-owned in staging. Accepted human
`desktop_chat` writes also create one idempotent `cf_chat_quota_events` row in
the message batch. Chat sharing stores 30-day D1 tokens and ordered message references
beside that projection; its public route exposes only message id, text, sender,
timestamp, and the explicit sender display name. Creating a new share also
removes indexed expired shares so D1 preserves the legacy Redis TTL lifecycle.
Chat generation remains API-AI-owned. Default text chat now acquires a D1 chat
session, reads its bounded unreported history, calls the configured Workers AI
chat model, commits the human/AI exchange plus session count/preview in one D1
batch, and emits the legacy `data:` plus base64 `done:` SSE contract used by
Web/mobile clients. The ordinary source `python/shared/chat_target.py` owns
selection and transactional provider-result commits for text chat, initial
messages and the existing narrow completion contract. Migration
`0155_chat_clear_epoch.sql` adds an empty default epoch to existing sessions;
explicit clear rotates it. The target captured before model IO includes uid,
session, app and epoch. Conditional D1 inserts reject a late result after clear
or deletion without recreating its session; paid model work remains accounted.
The narrow completion contract also resolves its target before idempotent cache
replay. Reusing a key for a different owned session/app, or an old cached row
without a provable session, returns 409 without another provider or quota write;
missing/foreign explicit sessions still return 404. Its context read uses the
selected app as well as UID/session, including non-default app sessions.
This forward migration has only local qualification here; previous-release and
remote rollout/rollback eligibility remain pending.
Before provider work, API AI atomically reserves one D1 quota event. The
conditional insert is the Free-plan hard-cap boundary, so concurrent requests
cannot both consume the final monthly slot; paying plans retain the legacy
overage behavior. D1 failure returns the legacy SSE-visible retry reply without
saving the human turn or invoking Workers AI. Successful inference settles the
same event with prompt/completion tokens and USD cost in the exchange batch.
The default-model price variables follow Cloudflare's published
[Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/),
and the synchronous model response exposes the required
[`usage` object](https://developers.cloudflare.com/workers-ai/models/llama-3.2-3b-instruct/).
The native text-chat path also accepts an optional public/private app projection
and bounded page context. App sessions, history, prompts, and message rows stay
scoped by `(uid, app_id)`; unavailable or disabled apps return a deterministic
`404` before quota/provider work. Page context is inserted as explicitly
untrusted reference data and is never persisted as an instruction. Attachments
still return a typed `409 attachments_not_migrated` on this Workers AI path;
the separate Jobs attachment bridge remains the opt-in provider boundary.
RAG/tools, BYOK request overrides, and provider-streamed token delivery remain
separate qualification boundaries rather than silently degrading to generic chat.

Historical Firestore chat session/message replay has a separate reviewed
planner (`npm run chat:reconcile -- --input <manifest.json>`). It accepts only
de-identified, bounded exports, records source-row/export fingerprints, and
emits guarded apply/verify SQL. Apply requires a completed `new` account
cutover with matching generation and `destination_backend_bound=1`; it never
overwrites an occupied D1 row or imports file/provider state. A zero-row
verification result is required before treating a replay as complete.

The Jobs Worker also exposes a separately gated operator apply seam at
`POST /internal/chat-history/apply`. It is disabled unless
`CHAT_HISTORY_IMPORT_STAGING_ENABLED=true`, requires `secret-key: ADMIN_KEY`,
and requires an HMAC-SHA256 signature from the at-least-32-byte
`CHAT_HISTORY_IMPORT_SIGNING_SECRET` over a canonical payload containing the
batch id, manifest hash, and sorted entry uid/entity/generation/source/import/
plan hashes in `x-chat-history-plan-signature`. The body is bounded to 1 MiB/20 entries and
must be the reviewed planner output; the Worker recomputes source-row,
import, plan, manifest, and batch hashes, rejects sensitive fields and file
references, and applies sessions before messages in one D1 batch. Migration
`0130_chat_history_apply_receipts.sql` records content-bound receipts for
idempotent replay. Account-generation, cutover, and deletion fences are
checked before and during the batch. This is an operator-controlled staging
seam only: it does not fetch Firestore/GCS, and no real export or production
owner cutover is claimed until the export and provider/file continuity review
has completed.

The account read routes use D1 as the isolated staging authority. Conversation
and memory writes update one idempotent `cf_usage_sources` row in the same D1
batch as the source projection, and migration `0046_account_usage.sql`
backfills existing projected rows. Usage periods and history buckets are UTC;
the legacy per-user timezone lookup is still an explicit parity gap.
`cf_user_subscriptions` is the isolated D1 subscription projection and
`cf_subscription_prices` is the allowlisted price catalog. An empty price
catalog returns HTTP 200 with no plans and `show_subscription_ui: false`, so
the Web Worker does not offer a checkout that cannot succeed. The Jobs Worker
now owns checkout-session creation, customer-portal creation, immediate and
scheduled subscription changes, cancellation-at-period-end, and Stripe webhook
reconciliation in staging. Payment mutations use Stripe idempotency keys.
Cross-plan changes use immediate invoiced proration; same-plan interval changes
use the provider-required create-then-update Subscription Schedule sequence.
Cancellation first releases any active schedule and stores bounded feedback on
the D1 subscription projection. The webhook verifies the exact raw request body
before persisting an event-id/hash inbox row and publishing only the event id to
the Queue. Queue processing retrieves current Stripe objects instead of trusting
delivery order, then projects only allowlisted prices for a live
Cloudflare-cutover account. Missing Stripe credentials fail closed; no staging
price IDs are synthesized or copied from production.
Checkout admission re-checks the D1 account-deletion fence immediately before
calling Stripe, so a deletion intent returns `409` without creating an
external Checkout Session. This staging endpoint currently projects `checkout.session.completed`,
subscription created/updated/deleted, and Subscription Schedule
created/updated/completed/canceled/released events for regular Omi plans and
Payment Link subscriptions for paid marketplace apps. Schedule processing
retrieves both the latest schedule and its current subscription before changing
D1, so out-of-order deliveries cannot roll the price projection backward. Keep
it on an isolated Stripe test-mode endpoint. BYOK/reviewer overrides, phone
quota, and production subscription import remain legacy-owned.

Migration `0059_creator_payments.sql` makes the Jobs Worker authoritative for
creator payout setup: Stripe Connect account creation, onboarding status,
Account Link refresh/return, `account.updated` webhook projection, supported
countries, PayPal details, and default payment-method selection. The D1
`uid ↔ acct_*` mapping is unique and every authenticated refresh verifies that
mapping before calling Stripe. Account creation uses a uid-derived Stripe
idempotency key. Stripe's browser refresh callback is a GET, so the Worker
accepts a bounded signed refresh URL and redirects directly to a newly created
single-use Account Link; the legacy authenticated POST response remains
available to existing clients. Connect webhook signatures use their own
rotatable secret and the exact raw body. Paid app create/update now provisions
Product, monthly Price, and Payment Link objects through Jobs and commits their
owner-bound mapping with the D1 catalog row. Existing hosted links are consumed
directly by the Cloudflare entitlement flow.

Migration `0061_app_payment_links.sql` keeps the Stripe account, product, price,
Payment Link, and hosted URL in a provider-only D1 mapping instead of the public
app catalog. Creator deletion verifies every mapping against Stripe, deactivates
the hosted Payment Link, expires every still-open Checkout Session, and only
then writes a non-identity retirement tombstone and purges the catalog. The
account-deletion intent fences concurrent Checkout while cleanup is running;
the retirement tombstone prevents a delayed Checkout webhook from resurrecting
an entitlement after the catalog row is gone. Creator deletion also cancels all
projected subscriber renewals before product data is purged. Production cutover
remains blocked until existing paid-app provider identifiers have been imported
and verified in `cf_app_payment_links`, the Jobs Stripe secret is provisioned,
and the paid mutation path has been exercised against the intended Stripe mode.

`POST /v1/apps` and owner-authorized `PATCH /v1/apps/{app_id}` now keep mutable
app records in `cf_app_catalog` while retaining the legacy multipart
`app_data` + `file` contract. Logos are bounded PNG/JPEG/GIF/WebP bytes stored
under a versioned `cf-app-logos/{uid}/{app_id}/{version}` R2 key. The catalog
publishes only the current immutable URL; failed mutations delete the staged
version, app deletion removes the current object, and account deletion sweeps
the full uid prefix. Client-supplied identity, approval, payment identifiers,
reviews, chat tools, persona fields, and MCP credentials cannot cross this
generic mutation boundary. New rows are always `under-review`; only the owner
can read private, pending, or disabled rows. Paid mutations require a completed
creator Stripe profile and write the Product/Price/Payment Link mapping with the
catalog in one D1 batch. If that batch fails, the unpublished hosted link is
deactivated before the staged logo is removed. When an external integration
declares a chat-tools manifest, create/update performs one bounded HTTPS fetch,
validates and normalizes the initial tools into the catalog, and fails open with
shared fallback telemetry if the dependency is unavailable. Its timeout uses
the `AbortController` and timer APIs supported by workerd. The explicit
owner-only refresh route reuses the same SSRF, timeout, size, and tool-count
limits. Fetch uses workerd's manual redirect mode and rejects every non-2xx
response without following its location. The route preserves its interactive
contract by returning `502` without changing D1 when the manifest cannot be
fetched. Visibility changes are also owner-only and use an optimistic catalog
update so they cannot overwrite a concurrent app mutation.

Migration `0062_app_deletion_fences.sql` moves owner-authorized
`DELETE /v1/apps/{app_id}` to Jobs without making provider cleanup synchronous
with the browser request. Admission validates paid-app mapping completeness and
Stripe configuration before hiding the app and removing its install mappings,
then atomically stores a generic job plus an app-scoped deletion fence. Queue
processing deactivates the Payment
Link, expires open Checkout Sessions, verifies every projected subscriber
against Stripe, releases matching Subscription Schedules, and stops renewal in
bounded batches. Each provider event clears the job's per-subscription
verification marker, while Checkout and subscription webhooks observe the fence
and cancel rather than re-project a billable entitlement. Only after every
subscription has been reverified does one D1 batch remove installs and catalog
data, persist the paid-app retirement tombstone, and complete the job. Provider
or ownership failures keep the app hidden and the durable job retryable; the
scheduled reconciler bounds automatic attempts, and an exact owner retry resets
that budget. The HTTP response retains the legacy `{"status":"ok"}` shape.

Migration `0060_app_subscriptions.sql` projects paid-app subscriptions into D1
from the same raw-body-verified Stripe inbox. Checkout processing requires the
Payment Link's `app_id`, an exact `uid_` client reference, a live cutover
account, and an approved paid catalog row; it then binds both identities into
Stripe subscription metadata and the unique D1 mapping. Subsequent subscription
events refresh or revoke the entitlement, and inactive projections remove the
installed-app row. Authenticated GET/DELETE app-subscription routes read this
mapping and verify uid/app/customer ownership before cancellation. Cancellation
is idempotent and preserves access through the current paid period. No Stripe
key is needed for D1 reads; provider mutations fail closed when it is absent.

`GET /v1/users/me/usage-quota` and the mobile subscription projection now read
the UTC-month `cf_chat_quota_events` authority. Free, Neo, Plus, Unlimited, and
Operator use the same question limits as the backend contract. Architect uses
the UTC-month `cf_llm_usage_daily` cost ledger. A still-running API-AI provider
event makes that projection return `503` rather than displaying a false zero;
desktop persistence events are priced by their separate client/server bucket
report and do not pretend to have an event-local settlement.

Migration `0075_user_byok_enrollments.sql` moves BYOK enrollment to D1. The
client still sends only the SHA-256 fingerprints of its OpenAI, Anthropic,
Gemini, and Deepgram keys; Edge stores only HMAC-peppered fingerprints and a
seven-day heartbeat. Raw `X-BYOK-*` keys remain request-local. Edge strips keys
for inactive, missing, or expired enrollments, rejects active fingerprint drift,
and signs `byokActive` into the request-bound internal context only after all
four keys validate. Subscription and quota reads trust that signed authority,
not caller-controlled header presence.

Migration `0056_llm_usage_daily.sql` moves the four
`/v1/users/me/llm-usage` read/write contracts to a D1 daily aggregate. Rows keep
legacy feature/model telemetry separate from the flat `desktop_chat` cost
bucket, so bucket reports do not appear in feature summaries and the total-cost
read never double-counts the per-account breakdown. A D1 trigger projects the
one NULL-to-settled chat quota transition into the feature ledger, including a
backfill for already-settled staging events; retrying settlement cannot add the
same provider usage twice. This is an isolated-staging authority until
production subscription and legacy Firestore usage documents are imported.

`GET /v1/payments/overage-info` reuses the same UTC-month question and cost
authority. Neo and Operator attribute provider cost to excess questions using
the legacy proportional formula; Architect applies the configured markup only
to cost above its allowance. `OVERAGE_MARKUP_MULTIPLIER` defaults to `1.15`.
The projection returns `503` while a Workers AI chat event has not settled, so
the billing explainer cannot present an understated accrued charge.

The Auth Worker includes the Better Auth account creation timestamp in the
request-bound, audience-bound internal identity assertion. API Core uses that
signed value for `GET /v1/users/me/paywall`, `GET /v1/users/me/trial`, and the
desktop quota override without receiving an Auth-D1 binding. The three-day
paywall remains controlled by `TRIAL_PAYWALL_ENABLED` and defaults off, exactly
like the backend. It applies only to old Free desktop accounts; paid plans,
mobile/unknown platforms, missing creation timestamps, and requests carrying
all four legacy BYOK provider headers fail open. Stored BYOK enrollment and
fingerprint authority still require a separate production import.

`GET /v1/fair-use/status` reads `cf_fair_use_states` plus idempotent
`cf_fair_use_usage_sources`. Rolling totals include only `realtime` and
`sync_fresh`; `sync_backfill` and `custom_stt` remain separate cost lanes and
cannot change live status. Limits use the D1 subscription snapshot so
unlimited-transcription plans receive their raised thresholds. New isolated
accounts have no imported state or speech sources and therefore receive the
legacy `stage=none`, zero-usage response instead of a route 404. Migration
`0048_fair_use_usage_revision.sql` makes source snapshots monotonic. Staging
Workers now ingest exact interval-union speech for Workers AI raw/voice-message
requests, hosted-ASR requests that return timed segments or words, Queue jobs,
and Realtime provider connections. Generic Workers AI and unknown hosted ASR
sources deliberately record `dg_ms=0`; provider classification and sync-local
conversation finalization/backfill remain separate migration boundaries.

Migration `0049_fair_use_enforcement.sql` adds the D1 state machine, immutable
case events, and a leased notification outbox. The Jobs Worker scans a bounded
batch every five minutes, applies the legacy strict soft-cap and raised
unlimited-plan thresholds, uses the 72,000-second basic monthly usage projection
for the `free_exhausted` synthetic classifier, and otherwise classifies at most
30 metadata-only conversation summaries with Workers AI. A per-user D1 lease
and 12-hour cooldown make repeated cron delivery idempotent. Stage progression
remains classifier-gated (`none → warning`, then prior 7-day event counts gate
`warning → throttle → restrict`), and every completed evaluation records an
event even when no action is taken. Paid upgrades clear only enforcement whose
last classifier type is `free_exhausted`.

API AI, Queue consumption, and Realtime check the same D1 live-usage boundary
before invoking ASR. Restrict-stage accounts are blocked only while the default
live soft caps remain exceeded, and the all-plan 30-hour daily ceiling is also
enforced. Responses preserve the legacy `429`, `Retry-After`, and
`X-Omi-Rate-Limit-Reason: fair_use` contract. Realtime rechecks every five
minutes so a long-lived connection cannot bypass a later escalation. Expired or
malformed restrict deadlines persistently downgrade to throttle. The public
case route exposes only case reference, effective stage, timestamps, support
copy, and support email; it never returns UID, usage, or classifier evidence.

Fair-use actions atomically create an FCM outbox row. The Jobs Worker claims
rows with a recoverable lease, uses the FCM HTTP v1 API when the optional
staging service-account secret is configured, deletes only
provider-confirmed unregistered tokens, and retries transient failures with
bounded backoff. An account with no registered device completes the outbox row
without requiring Firebase credentials. The six support/admin routes are D1
backed in staging and require the independent `FAIR_USE_ADMIN_KEY` Worker
secret; the API compares `X-Admin-Key` in constant time and stores only its
short hash as the resolving/clearing actor. Dashboard reads, case lookup,
event resolution, state reset, and manual stage changes never accept a user
session as admin authority. Production state import remains legacy-owned until
its reviewed backfill boundary moves.

The 2026-08-28 staging release exercised a generated spoken WAV through the
raw Workers AI route, the Web/Flutter multipart route, and the Queue route.
Each provider result contained one timed segment and committed exactly 3,160 ms
of `sync_fresh` speech. Repeating both synchronous requests with the same
operation key left source count and total speech unchanged, including a fresh
multipart boundary; the Queue retry returned the original completed job ID.
The resulting Fair Use read stayed at `stage=none`, reported 0.2% daily usage,
and kept Deepgram usage at zero. Realtime interval accumulation and Durable
Object alarm recovery are covered by the Worker integration suite; the release
did not claim a live external-ASR fault injection. The isolated account, jobs,
usage rows, and generated audio were deleted after verification.

The same staging release also exercised the deployed five-minute Fair Use cron
with an isolated basic account. A `sync_fresh` source at 7,200,001 ms (one
millisecond above the default daily soft cap) plus exactly 72,000 projected
monthly transcription seconds produced one `free_exhausted` evaluation, one
`none → warning` event, and one public case reference; a later cron left the
event count at one during the 12-hour cooldown and delivered the no-device
notification outbox row in one attempt. The unauthenticated public case read
returned only the documented privacy-safe fields. A temporary restrict state
made the raw Workers AI, voice-message, and async transcription entries all
return `429` with `Retry-After` and `X-Omi-Rate-Limit-Reason: fair_use` before
body validation. Expiring that deadline changed the same empty-audio probe back
to its normal `400` validation result and persisted `throttle`. All injected
state, event, outbox, and usage rows were deleted, the case returned `404`, and
the full staging smoke passed again after cleanup. No production Firebase
credential was copied into staging; notification delivery with an actual
registered device remains an explicit credentialed staging check.

The staging follow-up configured a newly generated `FAIR_USE_ADMIN_KEY` only on
API Core. The release smoke confirmed the dashboard route fails closed with
`403` when no key is supplied. An isolated D1 state/event/usage fixture then
exercised all six admin route shapes through Edge: flagged-user and user-detail
reads, internal case lookup, event resolution, a manual restrict transition
with its 30-day deadline, state reset, and reset of a legacy user with no prior
state all returned `200`; resolving a missing event returned `404`. The two
temporary states, event, notification, and usage rows were verified at zero
after cleanup.

`POST /v1/users/desktop-usage/daily` now reaches Python Core through the
600/hour authenticated Edge policy. D1 owns one row per uid/date/device;
concurrent retries merge each bounded running counter by maximum. Exact dates
must be within two days of today in the supplied IANA timezone. Export includes
these rows, and account deletion fences and purges them. The real local
recording contract covers concurrent delivery, export and queued erasure; this
does not imply an Eddy production deployment.

The daily-summary routes use an explicit D1 projection (indexed date/visibility
plus bounded JSON fields). On-demand create and in-place regenerate now use the
Workers AI synthesis binding, the upstream free-chat admission policy and the
user's IANA local-day boundaries (latest registered device timezone, UTC when
absent). Create reuses an existing date without inference. One D1 day lease
serializes generation, with a 90-second model deadline and 120-second lease;
empty or contentless days release it without a create cooldown. Successful
creation arms the 30-second create cooldown; regenerate reserves its separate
30-second cooldown before inference. Summary deletion revokes its writer.

`daily_summary_content.py` bounds recent conversation material to 200 rows and
24,000 rendered characters, reports truncation through shared fallback telemetry,
and declines days without speech or useful summary content. Tasks, local usage
stats and up to three eligible canonical memory references come from D1. Model
output supplies only validated prose and mapped source citations; provider
failure returns 503, while malformed JSON uses the upstream basic-recap fallback
with telemetry. Persistence atomically checks the lease and selected source
snapshots, so locks, memory rejection, source deletion or an expired writer
cannot publish stale content. Export includes recaps but omits generation tokens;
the existing account deletion owner purges both projection and control rows.

The export download filename uses the validated runtime brand ID, such as
`eddy-export.json`. A legacy deployment missing presentation metadata can still
export its owner's data as `user-data-export.json`, with a shared fallback event.

The settings-test route shares this generation owner, but scheduled delivery,
push notification and notification-token requirements remain an explicit
unqualified upstream boundary. A successful manual recap is not proof that the
notification pipeline or the complete Cloudflare target is deployed.

The conversation routes use an explicit D1 projection (indexed metadata plus
bounded JSON transcript/structured fields). The POST `/v1/cf/conversations`
staging ingress accepts a pre-transcribed, bounded conversation and upserts by
the uid/id key; it does not run LLM enrichment. Canonical GET list/count/detail
routes now read this projection in staging. List responses never include
transcript segments, and locked rows redact derived content. Rows can also be
loaded by the reviewed backfill/import workflow. Title and starred metadata
mutations update the canonical D1 projection in staging. Visibility and
unauthenticated shared reads use a unique D1 owner index maintained in the same
batch as the conversation row. A cross-account duplicate conversation id is
rejected instead of choosing an owner; public responses remove geolocation,
external integration/merge data, and the encryption tier before resolving only
the referenced uid-scoped people rows.
Conversation search uses a D1 FTS5 index maintained by insert/update/delete
triggers over IDs, titles, summaries, categories, and transcript text. Search
remains uid-scoped, excludes locked rows, and supports the Web pagination,
discarded, date, and speaker filters. Default conversation deletion removes the
D1 projection and refreshes folder counts; `cascade=true` soft-deletes the
conversation's derived memories, removes its action items, writes vector
retraction outbox entries for each, and enqueues an idempotent
`conversation_audio_purge` Jobs message that deletes the conversation's
`chunks/`, `merged/`, `playback/` and `sync-playback/` ASSETS prefixes plus
its `{uid}/{conversation_id}.wav` recording. The first-party
pre-transcribed `/v1/conversations/from-segments` route now uses the same
Workers AI enrichment, D1 batch, client-session-id claim, app/Developer webhook
fanout, usage, and vector outboxes as Developer ingest, with device provenance
resolved from the released headers. Conversation finalization, reprocess, and
merge now use D1/Queue leases and API Core; cascade deletion and its audio
cleanup are Worker-owned, while realtime/audio integration fanout and
finalization-triggered fanout remain legacy-owned;
production reader cutover still requires those write authorities and readers to
move together. App-key conversation/memory ingest is a separate migrated
boundary described below. The isolated `/v2/sync-local-files` finalizer is the exception:
when private cloud sync is enabled, Jobs decodes each accepted WAL into bounded
16 kHz mono WAV windows, stores deterministic `sync-playback/` objects in R2,
and streams their PCM payloads into one dense `conversation.wav` with an exact
wall-clock/captured-audio spans manifest. The file list, dense artifact stamp,
and R2 intent ledger are committed before the staging WAL is deleted. Disabled
private cloud sync retains no playback object.

First-party `/v1/tools/*` conversation, transcript-chunk, memory, and
action-item retrieval runs in Python API Core. Vectorize supplies ranked
candidates only; every hit is mapped through projection state and re-hydrated
from uid-scoped D1 before it is returned in the typed tool envelope. Tool task
create/update calls the canonical action-item D1 authority, which records its
vector outbox in the same batch and publishes only a Queue hint afterward.
Edge applies the released `tools:search` and `tools:mutate` one-hour limits.
Google Calendar OAuth, connection state, refresh, and event-picker reads run in
Jobs against D1 and the hosted Calendar API. The canonical hyphenated callback
and the legacy underscore callback alias share the same single-use D1 state and
encrypted-token authority. Calendar-event creation and other
conversation links (including auto-link) and the calendar-event tool also run in
Jobs. The Calendar-only OAuth grant accepts attendee email addresses; contact
lookup and non-Calendar integrations remain outside this cutover. Explicit
Calendar disconnects revoke all pending states and advance a per-user OAuth
generation; the callback uses a D1 compare-and-set before persisting a grant,
so an in-flight provider exchange cannot restore a connection after disconnect.

The data-protection migration inventory now runs in API Core over D1 at
`GET /v1/users/migration/requests?target_level=enhanced`. It preserves the
legacy public/shared conversation exclusion and treats D1 rows without an
explicit protection level as `standard`. The single, batch, and finalize
mutation endpoints are also Edge/API Core-owned in staging, but return a
retryable 503 until completed cutover and an encryption executor are proven;
they never call legacy or create a fake receipt. A live staging check returned
200 for an isolated Better Auth account, 401 without auth, and 400 for an
invalid target; the smoke account was then deleted and verified absent from
Auth and the App D1 deletion intent table.

The `/v1/candidates/control` read is also Cloudflare-owned for isolated staging
accounts. Until the legacy Firestore task-control document has a D1 projection,
API Core returns the closed shell (`workflow_mode=off`, generation `0`, and
`chat_first_ui=false`) so released clients cannot accidentally enable a partial
task-intelligence surface. The remaining candidate list/create/migrate/resolve
routes now use the same authenticated API Core boundary and return the legacy
feature-disabled `404 Not found` response; they never attempt a Firestore read
or accept a candidate write without a projected workflow generation. This keeps
Cloudflare accounts fail-closed while candidate storage and generation fences
remain a separate D1 migration surface.

The `/v1/sync/audio/*` Worker boundary serves those already-materialized WAV
windows without ffmpeg or a local media service. `/urls` returns one-hour HMAC
URLs for cached objects; a cache miss is reported as `pending` and admits one
fingerprinted `legacy_audio_rebuild` job to the shared Jobs queue (it never
decodes PCM in the request). Repeated polls reuse the same `cf_jobs` row and
message identity, while queue publication failures are recorded as a failed
job and return a retryable 503. Admission and processing are fenced by the
uid-scoped conversation, recording/account deletion state, and R2 playback
metadata. The download route rechecks the signed uid/conversation/audio
identity, locked state, D1 ownership, and R2 key prefix, then streams full or
single-range responses. Tokenless downloads still require Better Auth. Existing imported
`playback/*.mp3` and `merged/*.wav` objects are readable after the reviewed R2
copy. Worker-native conversations also return the dense WAV through
`conversation_audio`, so Flutter can use its spans-aware single-artifact path
without ffmpeg or MP3 encoding. When a copied legacy conversation has no ready
playback object, `/precache` creates one deterministic Jobs queue item. Jobs
inventories `chunks/{uid}/{conversation_id}/` in R2 and rebuilds raw `.bin`,
legacy packet-count `.opus`, encrypted `.enc`/`.opus.enc`, and framed
`.batch.bin`/`.batch.enc` sources into deterministic `sync-playback/*.wav`
objects. Opus decoding runs in Worker Wasm and encrypted objects use the same
HKDF-SHA256/AES-GCM contract as the backend through Workers Web Crypto; there
is no runtime GCS, filesystem, ffmpeg, or local ASR dependency. Each R2 put is
fenced by `cf_sync_playback_objects`; the metadata CAS promotes the intent,
while the five-minute Jobs maintenance pass promotes referenced crash survivors
or deletes stale unreferenced objects after one hour.

The staging speech-profile boundary stores the released mobile client's 16 kHz
PCM WAV upload and its post-validation duration in one object write to the
biometric-isolated `SPEECH_PROFILES` R2 bucket. Workers AI Whisper replaces the
legacy local VAD process as the fail-closed speech-presence gate; malformed,
silent, shorter-than-five-second, and longer-than-two-minute uploads never
reach storage. Read and sample-delete operations are uid-prefix-bound, and
playback uses a 60-second HMAC token bound to the exact uid and object key with
single-range streaming. Account deletion purges and residual-scans the bucket.
People reads use the same token contract instead of exposing R2 object keys;
single-sample deletion removes the exact indexed R2 object and its aligned D1
transcript reference, while person deletion clears the complete uid/person R2
prefix before removing the D1 row.
The legacy best-effort hosted speaker-embedding side effect is not part of the
upload success contract and remains a downstream realtime-identification
cutover boundary; staging does not run or bundle a local speaker model.
Production promotion remains forbidden until the legacy biometric bucket is
copied, checksummed, frozen, delta-copied, and residual-verified under the R2
migration inventory.

### Legacy private-cloud-sync copy and rebuild

The data plane is intentionally split: Cloudflare's managed migration tools
copy immutable source bytes, while Jobs interprets and rebuilds them only after
they are in R2. Configure the GCS bucket identified by
`BUCKET_PRIVATE_CLOUD_SYNC` as a read-only source and the environment-specific
`ASSETS` bucket (`omi-cf-{environment}`; currently `omi-cf-staging`) as the
destination. Jobs and API Core intentionally share this binding so imported
playback objects and rebuilt WAVs have one readable owner. Use
[Super Slurper](https://developers.cloudflare.com/r2/data-migration/super-slurper/)
for the initial `chunks/`, `merged/`, and `playback/` bulk copy with destination
overwrite disabled. It preserves source objects and metadata and does not delete
the GCS source. If legacy writes remain active during the copy, configure
[Sippy for GCS](https://developers.cloudflare.com/r2/data-migration/sippy/)
first, then run Super Slurper behind it as described in Cloudflare's
[migration strategy](https://developers.cloudflare.com/r2/data-migration/migration-strategies/).

Use separate narrow GCS credentials and R2 targets for staging and production.
After the bulk task reports complete, compare source/destination object counts
and byte totals for each prefix, freeze legacy writes, copy the delta, and run a
residual scan before changing the namespace state to `staging-owned` or
`production-owned`. Do not delete the source bucket during this workflow.

Encrypted legacy chunks additionally require the legacy backend encryption
secret on Jobs only. Supply it through stdin; never place it in Wrangler vars or
the repository:

```bash
printf '%s' "$LEGACY_AUDIO_ENCRYPTION_SECRET" | npx wrangler secret put LEGACY_AUDIO_ENCRYPTION_SECRET --name omi-cf-jobs-staging
```

Remove that Worker secret after the encrypted chunk inventory is drained and
the residual scan proves every referenced conversation has a readable imported
or rebuilt playback artifact. Unencrypted imports do not require the secret.

Live staging playback evidence on 2026-08-29 used Core version
`2fb48e7d-8ac2-4def-a841-64df4b696284`, Jobs version
`e5862eb9-8ed9-4d5f-89df-15b0b56699cf`, and Edge version
`ed6b119b-8368-44ff-90cf-ebede7a74c84`. A manifest-bound spoken 16 kHz PCM WAL
completed on `sync_fresh`, persisted one committed `cloudflare-r2` window and
one committed dense artifact, and returned `conversation_audio.status=cached`.
The dense response was a 149,926-byte `audio/wav` with a 44-byte RIFF header,
149,882 PCM data bytes, 16 kHz mono format, 4.684 seconds of captured and wall
duration, and one span mapping artifact offset zero to wall offset zero. A
header range returned exact `206 bytes 0-43/149926`. The first live attempt also
proved that R2 rejects an arbitrary readable stream without a known length; the
Jobs upload now wraps the exact header-plus-PCM byte count in
`FixedLengthStream`, and the redeployed path completed.

The successful D1 readback showed both playback intents promoted to
`committed`; the failed-attempt fixture remained recoverable as `stored` plus
`staging`, rather than being reported as completed. Both input WAL keys, both
per-window keys, and both dense keys were deleted after verification. The
conversation/FTS, sync job/file/content/capture/playback, Fair Use, control,
probe, and Better Auth user/session/account/verification counts all read back at
zero for the isolated test UID.

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

`GET /v1/conversations/{conversation_id}/suggested-apps` resolves the conversation's
suggested app ids against the approved D1 catalog. Private/persona/disabled apps
are filtered, payment links receive the caller-bound reference, and enabled/paid
state is computed from uid-scoped D1 projections.

`PATCH /v1/conversations/{conversation_id}/segments/{segment_idx}/assign`,
`PATCH /v1/conversations/{conversation_id}/assign-speaker/{speaker_id}`, and
`PATCH /v1/conversations/{conversation_id}/segments/assign-bulk` update
the bounded transcript projection with an updated-at compare-and-set and emit a
PII-free structured confirmation event. They preserve the legacy `is_user` and
`person_id` mutation rules. Bulk assignment resolves exact segment IDs and
completed-conversation `#index:N` compatibility targets, while rejecting
unresolved targets before mutation. The legacy implementation's per-route speech
training is disabled; person speech-sample extraction remains a separate follow-up
workflow.

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
call Google Calendar or mutate the external event. The POST link and auto-link
routes fetch the event through the encrypted Jobs grant, persist the normalized
link, and best-effort append the public conversation URL to the Google event.
`POST /v1/tools/calendar-events` creates a Google Calendar event through the
same grant and returns the legacy-compatible tool envelope.

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

The Sentry feedback webhook and poller are now API Core-owned. Sentry signatures
are verified at the Worker boundary, feedback issue IDs are stored as
uid-scoped D1 action items with `sentry-feedback:<issue_id>` idempotency keys,
and provider failures retain the legacy `skipped` response envelope. Event
details are fetched through the Worker fetch API; the route does not connect to
Firestore or a local process. Configure `SENTRY_WEBHOOK_SECRET`,
`SENTRY_ADMIN_UID`, and `SENTRY_AUTH_TOKEN` in the staging Worker secret set
before enabling Sentry delivery.

The calendar onboarding routes expose only a uid-scoped D1 projection of the
connected/skipped/re-auth-required flags. Google Calendar OAuth tokens, refresh,
and event-picker reads are owned by Jobs in separate fenced D1 tables; tokens
are never returned by either surface. Calendar event links and event creation
are also Jobs-owned. Jobs synchronizes the connected, access-token, and
reconnect-required state into the API Core onboarding projection on OAuth
success, refresh failure, and explicit disconnect, so the browser status stays
aligned with the grant authority. This group is staging-only until existing
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

The team-only `POST /v1/notification` contract now writes a bounded request to
the Jobs Worker's shared leased FCM outbox. It requires the independent
`ADMIN_KEY` secret and never sends directly from the request; scheduled Jobs
delivery preserves retry, invalid-token cleanup, and account-deletion fences.

The app catalog metadata routes (`/v1/app-categories`,
`/v1/app/proactive-notification-scopes`, `/v1/app-capabilities`, and
`/v1/app/payment-plans`) are static, public responses and now run in API Core
without D1 or external providers. App create/update/delete, subscriptions,
enable/disable side effects and MCP key/data routes are Cloudflare-owned; MCP
OAuth grants and hosted transport are Cloudflare-owned, while the secure CIMD
metadata-fetch boundary remains a separate migration surface. The three
installation routes below accept approved
public catalog rows, owner rows, or a pending app explicitly assigned through
`cf_app_tester_access`, provided there is no external setup callback. A paid
row additionally requires a current signed-webhook D1 entitlement. Owner and
tester installs never change the public install counter.

`/v1/approved-apps` and the authenticated `/v1/apps/popular` route read only the
approved, public, non-disabled, non-persona records in `cf_app_catalog`.
Authenticated `GET /v1/apps` unions that public set with the caller's owned
apps and explicitly assigned pending tester apps, deduplicates by catalog id,
and strips owner-only prompts and credentials from tester projections. Existing rows
enter through the whitelisted D1 backfill generator; new owner mutations write
the same authority while list reducers continue to omit reviews, payment
identifiers, credentials, and prompts. `GET /v1/apps/enabled`,
`POST /v1/apps/enable`, and `POST /v1/apps/disable` project only the
uid/app-id relationship into `cf_user_enabled_apps` and maintain the catalog
install counter for idempotent retries. Paid installs fail closed unless
`cf_app_subscriptions` is active/trialing and its current period has not ended;
the enabled-app read applies the same check. Default Persona creation is now
Cloudflare-owned through `POST /v1/user/persona`; image-backed Persona mutation, external setup
callbacks, and CIMD remain separate migration surfaces; no production cutover
is implied.

`PUT /v1/users/preferences/app` stores the caller's selected catalog row in
`cf_user_app_preferences`. Selection uses the same D1 public/owner/explicit
tester visibility boundary, but preserves the legacy setter contract by not
requiring installation, payment entitlement, or external setup completion.
Staging D1 is authoritative and does not dual-write the legacy Redis key.
Production cutover therefore requires importing existing
`user:{uid}:preferred_app` values and moving the conversation processor's
preference read to this D1 authority before routing production traffic.

`GET /v2/apps` now builds the marketplace's capability, category, and grouped
responses from the same public D1 rows. It preserves the legacy pagination
shape and score ordering, but intentionally returns `enabled: false` because
the public route has no user context; clients should combine it with
`/v1/apps/enabled`. Capability-specific grouped-category routes and the
authenticated `/v2/apps/search` filters are also read from the same projection.
Search exposes approved public catalog fields and uid-scoped installed-app
state; `my_apps=true` instead reads the caller's own pending/private/disabled
rows without weakening the public filter. Authenticated app detail reads use
the same owner and explicit tester-access exceptions, add the caller-bound
Payment Link client reference, and expose `is_user_paid` only from the current
D1 entitlement.
Public-app reviews now use `cf_app_reviews`; writes update the catalog's
rating average/count in the same D1 transaction, and catalog reads hydrate
bounded review lists plus the signed user's own review. `owner_uid` is an
explicit non-public catalog column, so review writes fail closed until older
rows are backfilled. Review/reply push notifications remain an external API
boundary. App tester membership/access and moderation now use D1 plus an
independent `APPS_ADMIN_KEY`; approve/reject verifies the catalog owner before
atomically changing approval state and publishing to the shared leased FCM
outbox. Image-backed Persona mutation, setup callbacks, and CIMD remain separate migration
work.

`POST /v1/app/thumbnails` now stores the bounded multipart image directly in
the public `ASSETS` R2 bucket and returns a Worker-owned immutable thumbnail URL
plus the generated id. `GET /v1/app/thumbnails/{thumbnail_id}.jpg` streams that
object with an immutable cache policy and fails closed for malformed or missing
ids. The route keeps the legacy response shape while removing the per-request
filesystem/GCS dependency; existing legacy thumbnail URLs are not rewritten by
this staging slice.

MCP API credentials now use `cf_mcp_api_keys`. `POST /v1/mcp/keys` returns the
`omi_mcp_` secret once, while D1 stores only the SHA-256 digest of its 32-hex
payload. List responses expose the legacy-compatible metadata and full scope
set without the secret; uid-scoped deletion is idempotent. Account deletion
fences new writes and purges this table with the rest of the product D1
authority.
The prefix constraint uses bounded `substr` checks plus simple GLOB predicates:
repeated character-class GLOBs accepted by local SQLite exceed D1's deployed
pattern-complexity limit, so the final-schema regression test forbids them.

`GET /memory/vector/search` now runs in API Core for the isolated staging
profile. Workers AI creates the query embedding, Vectorize returns only
tenant-namespaced candidates, and `cf_vector_projection_state` maps those
candidates back to uid-scoped D1 memories before locked, deleted, invalid, or
archived rows are filtered. The route preserves the legacy vector response
fields and never falls back to Firestore; unavailable AI/Vectorize/D1
dependencies return `503`.

Developer API credentials now use the separate `cf_developer_api_keys`
authority. `POST /v1/dev/keys` returns an `omi_dev_` secret once and stores only
the SHA-256 digest of its 32-hex payload; list and uid-scoped idempotent delete
retain the public metadata contract. The nine read routes for memories,
memory-vector search, action items, folders, conversations, goals, and goal
history verify the credential and per-route scope in Python API Core, require a
completed destination-bound account cutover, exclude locked/deleted/archived
rows, and hydrate vector candidates through uid-scoped D1 state. Edge preserves
only the raw Developer Authorization header and strips cookies and internal
identity assertions. Memory and action-item create, batch-create, update, and
delete routes write canonical D1 state and the vector projection outbox.
Conversation metadata update/delete and goal create/update/progress/delete use
the same D1 owners. Both Developer conversation-creation shapes now use Workers
AI in API Core and commit the conversation, grounded memories, action items,
usage, app/Developer webhook outboxes, and vector outboxes together. Memory
candidates must be user-specific, transcript-grounded, non-task statements at
the D1 storage boundary; weak-model topic labels and speaker scaffolding are
dropped rather than propagated to product data.

The MCP REST tools consume either those keys or a request-bound `mcp-oauth`
context in API Core. Exact API-key parsing, OAuth scope/client bounds, account
deletion fences, and destination-bound cutover state are checked before every
uid-scoped query. OAuth access tokens stop at the Auth Worker: its private
`POST /internal/mcp/verify` verifies signature, issuer, audience, expiry,
scope, and DPoP binding against the canonical `MCP_RESOURCE_URL`; Edge may then
sign only the uid, data scopes, and OAuth client id for one API Core method and
path. `GET /internal/mcp/principal` applies the same data-plane fence before
the transport advertises tools. Memory, conversation,
action-item, goal, chat, people, screen-activity, daily-summary, and profile
reads share the existing D1 authorities; memory and action-item writes use the
same projections and write limits. Edge forwards only Authorization and uses a
SHA-256 key digest as the Durable Object rate-limit subject. Profile name/email
come from a request-bound Auth service binding, not a direct Auth D1 binding.
The backfill tool maps legacy key metadata to D1, rejects raw key material, and
uses `omi_mcp_legacy` when the historical display prefix is absent. Memory,
conversation-summary, transcript-chunk, and action-item embeddings are rebuilt
with multilingual Workers AI BGE-M3 (1024 dimensions) into four versioned
staging Vectorize indexes.
D1 remains authoritative: Vectorize stores only hashed tenant namespaces and
candidate IDs, each hit maps through `cf_vector_projection_state`, and the
uid-scoped source row is hydrated with lock/deletion/date checks before return.
Memory, action-item, and pre-transcribed conversation writes publish a Queue
hint after atomically recording a D1 outbox; the Jobs cron backfills missed
writes, retries provider failures, removes stale vectors, and participates in
account deletion before D1 purge. Live staging tests verified English and
Chinese semantic recall, transcript-first conversation merge, UTC date metadata
filters after Vectorize metadata-index convergence, and post-test source/key
deletion. A conversation created through the public staging Edge became
searchable in 19.9 seconds through the Queue path rather than waiting for the
five-minute repair cron. The Auth Worker now installs Better Auth's MCP OAuth
Provider on the existing D1/session/JWKS authority. Migration `auth/0005`
creates the provider's client, protected-resource, token, consent, and client
assertion tables; the existing verification table supplies database-backed
DPoP replay reservations. Public clients require PKCE and are linked to the
single `MCP_RESOURCE_URL`. Anonymous dynamic client registration is fail-closed
unless `MCP_ALLOW_UNAUTHENTICATED_DCR=true`; only the isolated staging profile
sets that compatibility flag. The Streamable HTTP/SSE transport, token
verification, 22-tool registry, grant management, and discovery aliases are
Cloudflare-owned.

Client ID Metadata Documents (CIMD) are intentionally not advertised. Better
Auth's secure CIMD transport must resolve once, reject every special-use
address, pin an approved address while preserving the original TLS identity,
and refuse redirects. Workers cannot use raw TCP sockets for generic HTTPS on
port 443, while ordinary `fetch()` and `resolveOverride` do not provide that
arbitrary-host pinning contract. Enabling the plugin with a transport that
cannot satisfy those requirements would create an SSRF or availability defect;
production therefore keeps anonymous DCR closed until a separately qualified
secure metadata-fetch boundary exists.

App integration credentials now use `cf_app_api_keys`. `POST` returns the
`sk_` secret once, while D1 stores only the SHA-256 digest of its 32-hex-byte
payload; list responses expose metadata only and owner-authorized deletion is
idempotent. API Core validates the app/key pair, current D1 installation, paid
entitlement when applicable, and the requested manifest action before reading
or mutating user data. Reads use the canonical D1 conversation, memory, and
action-item projections and preserve locked-content redaction. Conversation
ingest uses the API Core Workers AI binding for structured summary/action-item
extraction; text-memory ingest uses it for fact extraction, while explicit
memories remain provider-free. Provider failures fail closed before product
data is written. The legacy 10/hour conversation, 60/hour memory, and 10/hour
notification limits are serialized in `cf_integration_hourly_usage`.

Conversation ingest also publishes one row per enabled
`memory_creation`-trigger app to `cf_integration_webhook_outbox` in the same D1
batch as the conversation. Jobs leases those rows, revalidates the public HTTPS
destination, sends the bounded payload with a stable idempotency key and a
30-second timeout, retries only transient failures, and stores a bounded
successful response message in the target app chat. Integration notifications
share `cf_notification_outbox`, the existing FCM sender, and the canonical chat
tables; no Redis, Firestore, local process, or parallel notification sender is
introduced. OAuth setup callbacks and CIMD remain separate migration work; MCP
OAuth, hosted transport, key lifecycle, and REST data tools are
Cloudflare-owned.

The migrated TTS surfaces now include both the desktop
`/v1/tts/synthesize` request shape and the mobile
`/v2/tts/synthesize` ElevenLabs contract. The mobile route runs through the
Python Worker's `AI` binding using Cloudflare's unified third-party model
catalog, so it needs no local TTS process, ElevenLabs SDK, or provider API key.
The Cloudflare account must have sufficient
[AI Gateway Unified Billing credits](https://developers.cloudflare.com/ai-gateway/features/unified-billing/)
before this third-party model can synthesize; missing credits surface as a
stable upstream `502` without exposing Cloudflare's provider error to clients.
It preserves the shipped Sloane voice ID, `eleven_turbo_v2_5` model alias,
`mp3_44100_128` output default, optional voice settings, and raw MPEG response.
The accepted output formats are the formats published by the
[Cloudflare Eleven Turbo v2.5 catalog](https://developers.cloudflare.com/ai/models/elevenlabs/eleven-turbo-v2-5/).
Every Cloudflare-owned route that previously consumed a first-party UID request
limit now uses the standalone rate-limit Durable Object: chat send, prerecorded/native/
async STT, conversation search, memory create/delete/modify, and all TTS routes.
The manifest names each route's policy and mechanically checks that it matches
the Edge matcher. Limits and one-hour windows mirror
`backend/utils/rate_limit_config.py`; `RATE_LIMIT_BOOST` and
`RATE_LIMIT_SHADOW_MODE` remain
available as staging Worker vars. The object serializes concurrent increments
and persists the fixed window; a limiter dependency failure preserves the
legacy first-party fail-open behavior and emits bounded `recordFallback`
telemetry. Callers explicitly selecting `failClosed` receive 503 for both transport
failures and malformed success responses; a partial `{ allowed: true }` reply is
not an admission decision. The same Durable Object also exposes an internal-only `/reserve` →
`/release` primitive for future reversible quota callers: each reservation gets
a one-time token, release is atomic and idempotent, stale tokens cannot
decrement a later window, and reservation storage is reclaimed by the window
alarm. This is a foundational seam only; no legacy route or manifest owner is
changed until a caller-specific reservation contract is migrated.

The TTS fine-grained limiters are also Redis-free in staging. After the Python
API AI Worker validates the request and confirms its Workers AI binding, it
calls the internal `omi-cf-rate-limit-staging` Durable Object
directly through a cross-Worker `RATE_LIMITS` binding. Edge uses the same object
without creating a circular service dependency. Both `/v1/tts/synthesize`
variants share a 20-request rolling 60-second window and a 50,000-character
UTC-day budget, matching `backend/routers/desktop_tts_updates.py`. Mobile
`/v2/tts/synthesize` keeps its separate 50-request rolling 60-second window,
10,000-character UTC-day budget, and 5,000-character request maximum from
`backend/routers/tts.py`. Invalid/provider-unavailable requests do not consume
the fine budget. An unavailable limiter preserves desktop fail-closed `503` and
mobile fail-open behavior; the latter emits bounded fallback telemetry. API AI
`/health` exercises a non-mutating DO RPC so staging readiness verifies the
Python-to-TypeScript binding without a billable synthesis.

Both `/v1/tts/synthesize` and `/v1/tts/synthesize-workers-ai` are backed by the
native `@cf/deepgram/aura-1` binding. The desktop request's historical voice IDs
are mapped to the Aura speaker set; this preserves a usable request shape but
does not claim the retired provider's voice-quality parity. Neither route reads
`TTS_API_KEY` or calls an external TTS API.

`/v1/stt/transcribe-workers-ai` is an additive raw-audio route backed by the
Python Worker's native `AI` binding and `@cf/openai/whisper-large-v3-turbo`.
It does not claim the legacy multipart/diarization contract; clients must send
`audio/*` (or `application/octet-stream`) as the body. The existing
`/v1/stt/transcribe` route remains the hosted provider seam for diarization and
the legacy segment response. The Python boundary converts the bounded request
body to the base64 form expected by the Workers AI Whisper model, so clients do
not need to know the binding's FFI representation. Successful timed results
fail closed unless their exact segment/word interval union is committed to one
idempotent `sync_fresh` D1 source; an explicit `Idempotency-Key` is the stable
operation identity even when multipart boundaries change, while requests
without one use Edge's request identity. Silence produces no usage row.

`/v2/voice-message/transcribe` is the staging Worker owner for the existing
Web/Flutter voice-input contract. It accepts bounded multipart `files` using
WAV, WebM, or MP4 containers plus the optional `language` field; it also keeps
the desktop `application/octet-stream` surface for 8–48 kHz, one- or two-channel
linear16 PCM by adding a WAV header in memory. The 10 MiB audio bound matches the
Flutter client's existing chunk size, multiple small multipart files are
combined in request order, and the response preserves `transcript`,
`stt_provider`, `stt_model`, `outcome`, and optional detected `language`. Empty
model text is a successful `expected_silence` result. Invalid containers fail
before inference, and provider/configuration failures use the existing bounded
transcription error shape without exposing upstream text. Timed results use the
same exact D1 speech meter; multipart parts are summed after interval unioning
within each provider result.

This route intentionally has no local codec/model process or ffmpeg dependency.
It is not yet production-parity for user-saved language resolution, context
keywords, the trial paywall, or the legacy daily audio-duration budget. Staging
therefore retains the Edge `stt:transcribe` 60-request/hour guard and an explicit
route-to-legacy rollback until those entitlement and usage authorities move to
D1; the route must not be described as a production cutover before then.

The 2026-08-28 staging release exercised this route through Edge with an
isolated Better Auth account and a generated spoken WAV. Web-compatible
multipart and desktop linear16 PCM both returned HTTP 200, `workers-ai`,
`success`, language `en`, and the same non-empty transcript; an unauthenticated
request returned HTTP 401. The permanent release smoke separately verifies the
authenticated empty-audio 400 boundary without billable inference. All test
account rows and generated audio were deleted after validation.

On 2026-08-30 the stronger versioned LibriSpeech release fixture (mono PCM,
16 kHz, manifest-pinned SHA-256 and exact expected transcript) passed the same
public staging Edge. The authoritative multipart probe matched the complete
normalized phrase; raw Workers AI requests matched it twice with timed segments
in 2,713/2,615 ms; desktop linear16 matched in 1,629 ms; and the Queue path
returned the same job ID on idempotent replay before completing with the exact
phrase and timings on its fourth two-second poll. Those four logical operations
created exactly four positive `sync_fresh` speech sources and zero Deepgram
usage, and the completed async job's temporary R2 object was absent. The formal
account-deletion path then removed the Auth user, job, usage, and cutover rows;
their combined residual was zero. The release probe now sends a stable Omi
product User-Agent because Cloudflare's security layer rejected Python urllib's
default User-Agent with HTTP 403 before the Edge route executed.

The same clean-English fixture passed the browser ticket-first Realtime path on
2026-08-30 against Realtime version
`e6fd4d46-781a-4b99-9355-e11977735426`. The socket reported native
`workers-ai` readiness in 1,357 ms and emitted one normalized final segment
6,915 ms after connection start, after the fixture had been paced in real time
and followed by one second of silence to exercise the 300 ms endpointing
contract; the complete normalized phrase matched the manifest. Exactly one
revision-1 `realtime` Fair Use source recorded 4,560 ms of detected speech. The
formal deletion workflow then reduced the Auth user/account/session and App D1
usage/cutover/intent residuals to zero, and a prefix scan found no account left
by the Realtime probes.

This remains clean-English evidence, not a multilingual/noisy WER,
first-interim, reconnect, or device-codec qualification. The native Nova-3
WebSocket binding currently requires `sample_rate` as a string and rejects the
documented `channels`, `interim_results`, `vad_events`, `punctuate`,
`smart_format`, and `diarize` options during upgrade; the Worker therefore sends
only the binding subset qualified in remote preview. Staging has no
`ASR_WS_URL`/`ASR_API_KEY`, so compatibility-only codecs and stereo fail
explicitly until an external fallback is selected or a Worker-compatible codec
adapter is qualified.

`/v1/translate` preserves the standalone NLLB request/response shape while
using the native `@cf/meta/m2m100-1.2b` binding in staging. The Worker explicitly
limits this route to English, Chinese, French, Spanish, Arabic, Russian, German,
Japanese, Portuguese, and Hindi; the legacy NLLB service remains the rollback
target for other languages until quality and coverage are qualified.

`/v2/realtime/session` is now a native Edge boundary: after Better Auth
verification it signs a short-lived first-message ticket for the Realtime
Worker/DO and returns the `wss:///v4/web/listen` transport. It does not call or
require an OpenAI/Gemini provider secret; explicit `openai`/`gemini` selectors
are rejected with `409 external_realtime_disabled`. The native WebSocket path
uses the bound Workers AI model and records speech fair-use usage in D1. The
older `/v2/realtime/usage` accounting endpoint remains an optional, non-native
compatibility surface and is not needed by the Workers AI client.

`/v1/auto/model-pick` no longer queries Artificial Analysis or defaults to a
Gemini provider. It returns the configured Workers AI chat model from the
shared D1 cache (with the binding's model as the deterministic fallback), so
no external AI key is required for model selection. This response is for the
Cloudflare-native client; released desktop clients that still enumerate the
retired `geminiFlashLive`/`gptRealtime2` IDs remain outside the no-legacy-
compatibility scope and must be updated separately before they can use this
selection.

`/v1/ai/*` is retained only as a non-staging compatibility implementation. The
staging Edge guard returns `503 external_ai_disabled` before reading the body or
calling API-AI, so `AI_API_BASE_URL`/`AI_API_KEY` are not deployment
requirements. New clients must use the bounded Workers AI routes; re-enabling
this legacy proxy is outside the current no-OpenAI/Gemini scope.

Default `POST /v2/messages` text chat uses the native
`@cf/meta/llama-3.2-3b-instruct` binding with a bounded 24-message/32,000-character
D1 history. A request carrying Edge-validated BYOK authority instead calls the
fixed OpenAI Chat Completions endpoint with the user's request-local OpenAI key
and does not reserve or settle Omi quota. The API AI Worker never accepts raw
BYOK header presence as authority. Model output is validated before both
exchange rows are committed; a provider or D1 failure emits no partial
persisted exchange. After app authorization and quota admission, a first default
session is atomically visible before model IO; concurrent first requests share
its ID and quota ownership. Clear/delete fences the captured epoch, and a late
model completion cannot recreate a removed owner. Model failure may leave a
zero-message session removable with the public clear endpoint; a rejected first
quota request creates neither a session nor messages. The Worker emits one compatibility `data:` frame followed
by the legacy base64 `done:` message; native provider-token streaming remains a
later latency qualification.

The v1/v2 initial-message aliases and the v2 session initial-message/title
helpers also run on the native Workers AI binding. Initial messages assemble a
bounded prompt from the caller's D1 AI profile, visible reviewed memories, the
last five messages, and an accessible app/persona prompt when supplied. The AI
message and session preview/count update commit in one D1 batch only after a
valid model response. Title generation uses at most ten bounded messages and
updates only the caller's session. All four helper routes retain the legacy
`chat:initial` limit of 60 requests per hour and require no local model service.

`/v1/embeddings` and `/v1/embeddings-workers-ai` are backed by the native
`@cf/baai/bge-base-en-v1.5` binding. They accept a bounded string or batch and
return an OpenAI-style `data[].embedding` envelope for client compatibility;
neither route reads an external embedding secret. They remain separate from the
multilingual 1024-dimensional BGE-M3 model used by the four isolated MCP
semantic-search projections; other embedding/index contracts still require
their own model, dimension, and retrieval-quality qualification.

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

The Auth Worker also owns signed internal `GET /internal/users/:uid`,
`GET /internal/users/:uid/residual`, and `DELETE /internal/users/:uid`
contracts used by the staging account-deletion workflow. The delete is idempotent
and removes the uid's Better Auth session, account, user, outstanding
delete-verification rows, owned OAuth clients, access/refresh tokens, and
consents in one D1 batch, then fails closed unless a residual query returns
zero for every identity table. These endpoints require a
60-second assertion bound to the Auth audience, uid, method, and exact path;
they are not public account-management APIs. Better Auth's public
`/delete-user` remains explicitly disabled; only the Jobs Worker may call this
final identity boundary after product D1/R2 deletion and residual verification.

Edge and Jobs now own a staging-only `DELETE /v1/users/delete-account`
workflow for Better Auth principals that have a completed, destination-bound
`isolated-staging-v1` cutover manifest. Jobs writes a durable App-D1 intent
before publishing an opaque job id to Queue. Migration `0052` installs mutation
fences that reject later inserts and updates for the uid while either the
intent or the 25-hour deletion tombstone exists. The consumer deletes the
eight uid-scoped `ASSETS` prefixes plus the uid prefix in each isolated
conversation-recording and speech-profile bucket, and all inventoried product D1 rows in
bounded batches, requires two zero-residual scans separated by 30 seconds,
then calls the signed Auth delete and validates the zero identity residual in
that response before transferring the intent to its tombstone. The internal
Auth call has a 15-second workerd-compatible timeout so a stalled Service
Binding releases the durable lease for retry instead of holding it for five
minutes. The D1 intent-to-tombstone transition is atomic, duplicate public
requests are idempotent, and the scheduled reconciler republishes durable
intents whose initial Queue send failed. Queue and DLQ payloads contain no uid.

The explicit residual inventory covers 73 product identity-bearing column
sites introduced by all App-D1 migrations, two deletion-control surfaces, and
ten R2 prefix surfaces across the three buckets. A schema guard fails whenever a later migration adds an
identity column without extending the inventory. D1 queries are parameterized,
R2 checks expose presence only, and partial batches, storage errors, or
non-zero residuals fail closed. For accounts with an Omi plan subscription id,
one or more paid-app subscription ids, a creator Connect account id, or an owned
paid-app Payment Link mapping, admission requires the Jobs-only
`STRIPE_SECRET_KEY`; after the durable fence settles, Jobs deactivates each owned
Payment Link and expires its open Checkout Sessions, reads each subscription,
lists its customer's active Subscription Schedules from Stripe, releases every
schedule attached to that subscription, and idempotently sets
`cancel_at_period_end=true` on the Omi plan and every paid-app subscription,
then deletes the platform-controlled connected account before any product purge.
Stripe documents that live Express-style
accounts can be deleted only after their balances reach zero, so a non-zero
balance or any other provider refusal keeps the deletion intent for retry. An already scheduled or
terminal subscription satisfies the cancellation goal without another
subscription mutation. Transport,
credential, malformed-response, and non-terminal-result failures retain the
intent and all product data for reconciliation. Production account deletion
remains blocked on production secret provisioning and identity/cutover
evidence.

Local validation applies every App-D1 migration with Wrangler, exercises the
real SQLite trigger boundary, and proves that both an
active intent and the tombstone block late writes while expired tombstone
cleanup restores writes. The TypeScript suite additionally covers the full
D1/R2 purge, two residual scans, Auth finalization, Queue-send recovery,
Auth-retry recovery, idempotent repeated deletion, Stripe schedule release,
subscription cancellation, Connect-account deletion, already-terminal
subscriptions, Payment Link retirement, open Checkout expiration, delayed
webhook fencing, and fail-closed provider retry behavior.

`/v1/users/training-data-opt-in` stores the review state in staging D1 and
enables private cloud sync as the legacy route does. The HTTP response remains
the legacy success/message shape. Its training-data notification side effect is
not yet migrated; the new Jobs FCM sender is currently scoped to the fair-use,
app-moderation, and app-integration outbox contracts.

`POST /v1/users/fcm-token` stores one token per sanitized
`platform + device-id-hash` key in staging D1 and keeps the legacy `{"status":"Ok"}`
response. Tokens are not returned by any public route. Fair-use, app moderation,
and app-integration delivery read this D1 token authority through the shared
leased Jobs outbox and FCM HTTP v1 adapter described above; other notification
producers remain on their legacy sender.

`DELETE /v1/users/fcm-token` removes the matching uid/device/token row and is
idempotent, so web sign-out retries do not leave stale notification targets or
surface a 404.

The memory-summary and chat-message feedback routes store uid-scoped ratings
in `cf_user_feedback`. Chat feedback also updates the matching D1 message JSON
in the same batch, preserving the client-visible rating projection. Every vote,
including voice/notification chat and memory keep/discard, also appends its
upstream envelope to `cf_feedback_events` in that transaction. The legacy
LangSmith submission remains a non-blocking observability side effect and is
not part of the staging request success boundary.

Jobs owns the five `/v1/admin/feedback` routes with its existing `ADMIN_KEY` and
hashed reader attribution. Core's signed internal service builds bounded,
pointer-only daily reports in App D1 and hydrates individual windows on demand.
The existing five-minute scheduled lane ensures yesterday's report after 01:30
UTC. Leased publication preserves an earlier report on failure; per-user event
and report-entry erasure prevents stale context reads or republication. See
[the feedback contract](../../docs/doc/developer/ForkCloudflareFeedback.mdx)
for the exact bounds, legacy defaults, unavailable encrypted context and tests.

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

The queues accept infrastructure `probe` jobs, native Workers AI `transcribe`
jobs, and content-idempotent `sync_local_files` jobs. Generic producers must use
a stable `jobId` or idempotency key;
reusing either identity with a different request fingerprint is rejected.
Messages are claimed in D1 per uid, retried independently, and moved to
`omi-cf-jobs-dlq-staging` after the configured retry limit instead of being
discarded. Transcription audio is removed after completion or terminal failure;
R2 lifecycle rules expire any `cf-transcriptions/` or `cf-sync/` cleanup orphan
after one day. `GET /v1/cf/jobs/{jobId}` exposes the state machine without returning
payload data, and requires the same authenticated uid that created the job.

Realtime callbacks retain their admitting socket across every asynchronous quota
or binary-decoding boundary. After either wait, a superseded/closed socket must
not enqueue or forward audio to the replacement provider. The same check precedes
provider startup after fair-use admission. The production-session regressions in
`tests/realtime.test.ts` suspend both quota and Blob reads, replace the connection,
and verify that only the current connection's audio reaches its provider.

The recording stream now opens its UID-scoped conversation in D1 before reporting
`ready` / `conversation_session`. Transcript segments commit before broadcast;
reconnect advances a connection token and resumes persisted timestamps. The old
connection cannot write through that token, and completed/deleted generations
are never reopened. Within a native Durable Object, pending D1 opens serialize
across socket replacement so an old response cannot acquire ownership last.
Migration `0154_live_recording_sessions.sql` adds this identity owner and the same
account-deletion fences; Jobs includes it in purge/residual checks. Explicit
`POST /v1/conversations/:id/finalize` retains the existing Queue/Jobs/Core owner.
Silence/disconnect auto-finalization, continuous-capture rotation, every missing
route and remote provider qualification remain separate unfinished CF-4 work.

Chat and task sharing now mint `/chat/:token` and `/tasks/:token` capabilities
from `PUBLIC_SHARE_BASE_URL`, projected from the same validated profile used by
all clients. The share custom domain belongs to the existing Web Worker; Edge
and Auth trust its exact origin alongside the Web origin. The fork Web build
adds the two pages and a strict public proxy that validates and reduces previews
before returning `private, no-store` responses. Task acceptance reuses the
existing Better Auth session/JWT and authenticated API proxy. Missing brand/share
configuration fails before D1 mutation. Unit and local product contracts cover
owner isolation, expired/locked/invalid capabilities, self and duplicate
acceptance, malformed responses and a real recipient copy. This local evidence
does not qualify remote routes or authorize a deployment.

`bash deploy/cloudflare/ci/product.sh` builds an isolated actual seven-Worker target,
applies all normal migrations, runs the shared identity/onboarding/Tasks suite and
a separate recording/Queue contract, then stops its process tree. Both fork
manifest lanes execute this same command. Read `contracts/README.md` for tools,
metadata, synthetic inference boundaries and retained evidence. A local green
report never sets release qualification or authorizes remote deployment.


### Developer conversation questions

`POST /v1/dev/user/ask` combines the existing Developer key authority, the
25/hour `dev:ask` limiter, tenant-scoped summary/transcript Vectorize retrieval,
D1 source rechecks and native Workers AI. Its default prompt and wire types
are staged from unchanged upstream sources. `WORKERS_AI_DEVELOPER_ASK_MODEL`
selects its native Llama 3.3 70B FP8 fast model independently of integration
classification; missing or invalid source citations return 503. Python timezone validation loads
packaged TZif data rather than assuming a host OS database. See
[the Developer Ask contract](../../docs/doc/developer/ForkCloudflareDeveloperAsk.mdx)
for provider errors, usage accounting and verification boundaries.

## Calendar share recipients

`GET /v1/conversations/{conversation_id}/share-recipients` runs in Core behind
Edge session authentication and account admission. The build stages the original
upstream response models and calendar-recipient functions through
`scripts/share_email_sources.py`; their owners are included in the release source
identity. It reads the owned conversation's calendar metadata from D1 and the
owner's email from Auth, then rechecks the conversation after that awaited lookup.
Locked conversations return 402, unavailable subjects return 404, and concurrent
calendar changes return 409. Missing owner email returns an empty suggestion with
sanitized fallback telemetry; Auth failures return an error. All responses disable
caching. It neither invokes a model nor sends mail.

Recipient suggestions do not qualify outbound delivery. The internal POST
transaction and its remaining provider boundary are described below.

## Share-email transaction (internal qualification)

Jobs registers the upstream POST share-email handler and delegates preparation,
claim and finalization to Core's signed `/internal/share-email/` service. The
public Edge route remains unregistered until native sender/delivery qualification.
The read-only recipient endpoint remains independently available.

Migration 0171 adds recipient claims, per-UTC-day quota and dispatch receipts.
One D1 batch reserves new recipients, charges their quota, publishes the owned
conversation and records its write revision. Every conversation write advances
that revision, including a same-value visibility write. A definite provider
rejection refunds the original day's charge and revokes only publication still
owned by that attempt. Other actors' share changes remain authoritative.

Jobs calls the Cloudflare Email Service structured binding once after Core
atomically changes the attempt from prepared to dispatching. Accepted and unknown
outcomes retain the claim and share link; unknown outcomes return 504 and emit
sanitized shared fallback telemetry. A repeated confirmed/ambiguous recipient is
not sent again. An active in-flight duplicate returns 409. The scheduled sweep
expires at most 100 prepared and 100 dispatching attempts per invocation; it never
sends email. Abandoned prepared attempts release claims and quota; abandoned
dispatching attempts become ambiguous. Payload HTML is removed on finalization.
Receipts, recipient addresses and quota appear in user export; internal mail
payloads and leases do not. All three authorities participate in account erasure.

The source projector stages the original request, normalization, sender-name and
markdown rules. Only the email footer's hardcoded upstream brand is replaced by
the validated brand identity and public share origin. Default model prompts are
unchanged, and sending does not call a model.

To qualify the remaining provider boundary, Jobs needs a native `send_email`
binding named `SHARE_EMAIL` and `SHARE_EMAIL_FROM_ADDRESS` from an onboarded
Eddy sending domain. These are deliberately not provisioned by this change: the
Eddy sender identity is not yet selected. The release resource contract must
include that verified binding/sender before the public route is admitted. The
[Cloudflare binding API](https://developers.cloudflare.com/email-service/api/send-emails/workers-api/)
defines the structured message and error codes. Resend is not used in this CF path.

[Transaction verification](../../dev/unified-main/implementation-2026-09-05/share-email-transactions-2026-09-06.md)
records the native Auth/Edge/Jobs/Core/D1 run with controlled provider outcomes,
zero outbound emails, local recovery/erasure tests and the exact remaining scope.

## JIT rollout decision

`GET /v1/jit/rollout-decision` uses the authenticated Edge-to-Core boundary and
the same D1 policy owner as frame requests. The original upstream tri-state and
allowlist decision rules are staged by `frame_request_sources.py`; no model or
prompt is involved. `cf_jit_flags` stores deployment defaults under the empty UID
and explicit per-owner overrides under the authenticated UID. A global enabled
kill switch cannot be cleared by an owner override. Each read takes one current
flags/account-generation snapshot and returns `no-store`, with no decision cache.
Missing rollout state is disabled; provider failures preserve unknown status and
emit sanitized shared telemetry. Account-deletion fences still deny access.

This read does not grant a reservation or claim that trigger/ledger routes are
available. The remaining five JIT route identities retain their own migration
requirements. The shared Server/Cloudflare HTTP suite includes the authenticated
tri-state wire contract; provider flag transitions and owner isolation also run
against the actual D1 implementation. See the
[client API guide](../../docs/doc/developer/ForkCloudflareJit.mdx).
The [verification record](../../dev/unified-main/implementation-2026-09-05/jit-rollout-2026-09-06.md)
distinguishes the hosted decision flow from the remaining trigger/ledger work.

## Canonical memory apply rules

`scripts/memory_kernel_sources.py` packages the original canonical memory models,
apply engine, promotion/graph receipt rules, pure Short-term lifecycle and
canonical lineage resolution into the normal Core build. It rewrites only the
explicitly mapped modules' import names;
unexpected upstream dependencies fail the build. Core tests consume the same
projection, and release identity includes every source file. No generated Python
copy is maintained in the repository. The original consolidation JSON Schema is
a frozen data contract so runtime Pydantic versions cannot change the prompt;
tests compare it to the upstream model. No default prompt is changed.

`memory_consolidation_sources.py`, called by the same projector, selects the
upstream L2 decision schemas, complete-batch validation, source-attribution
conservation and normalization/promotion receipt rules. The D1 adapter in
`memory_consolidation_apply.py` commits up to 20 decisions with normalization,
supersession, graph assertions, deterministic review records and the complete
journal/outbox chain in one guarded batch. Its patch builders are compared
against the original upstream persistence requests. Source/head races or late
storage failures leave no partial batch. This adapter does not yet enable
scheduled model execution; candidate retrieval, provider-window batch sizing,
leases, recurrence handoff, other intake families and default-read alignment
remain required. `memory_consolidation_llm.py` now connects the unchanged upstream
messages to Workers AI and the validated apply owner. It records actual model
usage and does not substitute a route after errors. The model override is
`WORKERS_AI_MEMORY_CONSOLIDATION_MODEL`, defaulting to
`@cf/qwen/qwen3.8-27b`. Overrides require its Chat Completions contract
(ordinary messages and one completed assistant choice). JSON is validated after
generation, as in upstream; no provider `response_format` is added. Input/output
byte bounds and a 180-second timeout protect the bridge, while provider token-window failures remain pending.
See the [model invocation verification](../../dev/unified-main/implementation-2026-09-05/memory-consolidation-llm-2026-09-07.md). Native POSTs
now use the upstream required-processing metadata, preserve source attribution
and remain pending until a real processor receipt; their initial vector work
is delete-only. The 1 MB body cap and stable internal retry identity remain in
force. See the [native intake verification](../../dev/unified-main/implementation-2026-09-05/native-memory-normalization-2026-09-07.md) and
[consolidation verification record](../../dev/unified-main/implementation-2026-09-05/memory-consolidation-apply-2026-09-07.md).

Native memory lists and ordinary product search now filter persisted lifecycle
state before pagination and counts. Pending required input remains visible to
its owner but is excluded from processed search. Archive/hidden/superseded,
removed-source and user-rejected rows are excluded; processed sensitivity
restrictions retain the upstream policy. Historical processed data needs no
new control or receipt. This does not yet supply active-alias collapse or full
device/cursor/archive parity. See the [default-read verification](../../dev/unified-main/implementation-2026-09-05/memory-default-read-2026-09-07.md).

The same builder selects the three original privacy scrubber/event function
bodies from `backend/database/memory_apply_store.py` into a separate pure module;
no Firestore client or persistence adapter is staged. `memory_privacy_plan.py`
uses those functions to prepare complete semantic/provenance tombstones and
content-free deletion operations, privacy epoch heads and delete-only outboxes.
The original lineage resolver includes incoming aliases, cycles and missing
survivors, retaining tombstones in retry inventories. This is a proposed apply
result, not a deletion acknowledgement: the caller still needs transaction
admission, opaque anti-resurrection receipts, physical provider cleanup and
history finalization. Public deletion handlers have not been migrated by this
step. See the [privacy-rule verification record](../../dev/unified-main/implementation-2026-09-05/memory-privacy-rules-2026-09-07.md).

Migration 0174 preserves existing memory rows, indexes, views and triggers while
allowing the upstream `content=None` shape only for a deleted tombstone. Every
current Core creator and the Jobs X extractor computes the same uid/item HMAC
using `MEMORY_PRIVACY_SECRET`. A live, opaque 30-day deletion receipt vetoes
reinsertions and updates inside D1, including MCP's deterministic-ID upsert and
old writers that omit the key. Existing unrelated legacy rows remain editable;
new writes always carry their key. Neither the key nor receipt inventory is
included in user export. The existing Jobs schedule expires receipts, and
account erasure includes their table. The public deletion coordinator now
atomically scrubs and seals its authoritative lineage before provider cleanup
(0175), then finalizes under the provider/hold/gate checks in migration 0176.
Native single/batch/all/default, MCP and Developer deletion acknowledge 200 only
after physical erasure; pending cleanup returns 503 `memory_cleanup_pending`
with `Retry-After: 2`. Existing Jobs Queue/cron uses signed Core continuations to
complete durable work. All/default requests retain their parent scope across
100-item batches; default retains Archive. Referencing history and memory
usage-source rows are removed, while original source conversations remain.
The [deletion verification record](../../dev/unified-main/implementation-2026-09-05/memory-privacy-delete-2026-09-07.md)
distinguishes actual local orchestration, hosted transaction proof and the
remaining production qualification.

The pure engine computes one complete apply result: memory items, graph
assertions, operation receipt, next control head and projection/vector outbox.
Its committed result is a proposed database bundle, not proof of persistence.
It preserves generation/head conflicts, deterministic retry identities,
content-mismatched replay rejection, deleted-source denial, restricted-content
delete-only projection and receipt-gated Short-term promotion. Hosted Python
Worker scenarios execute these exact rules with synthetic inputs; they do not
exercise a new public API or a D1 transaction.

D1 native single/batch memory intake now persists the upstream apply result,
operation receipt, commit/head, pending kernel outbox and usage work in
one guarded batch (migration 0172). Existing item columns remain authoritative;
the added JSON column contains only model fields without physical columns.
Whole internal replay is idempotent, and stale account/control state aborts the
batch. Receipts and commits follow owner-scoped export and account erasure.
Cloudflare native batches accept at most 100 items and 1,000,000 UTF-8 request
bytes, including JSON and metadata; the per-item content limit remains 50,000
characters. Edge bounds the body before Python and Core checks it again.
Oversize returns 413 with `memory_batch_too_large`, `max_bytes` and
`max_memories`, with no writes. The macOS fork import consumer now plans requests
by encoded bytes and count before writing; Electron's item-count-only importer
still needs that adaptation. Each accepted request remains one independent
atomic transaction.

Native content PATCH now uses that same journal/control writer. Migration 0173
adds target revision/metadata/ownership admission to its transaction guard.
The unchanged apply engine returns corrected content to pending Short-term,
clears graph admission and emits delete-only projection work until processing
readmits it. The current paid-plan lock, account/source generation and closed
record checks remain enforced before commit. Pre-journal rows are projected
read-only and adopted at their existing IDs in the correction transaction;
no synthetic historical commit is persisted. An explicit correction records
its real commit, operation and outbox atomically without charging new intake.
The patch policy is behaviorally compared against the original upstream
`update_canonical_memory_content` implementation. See the
[content-edit verification record](../../dev/unified-main/implementation-2026-09-05/memory-apply-edit-2026-09-07.md).

The same ordinary mutation owner now handles native visibility, review votes,
read/dismiss and baseline changes. Product fields, journal/head, review feedback
and projection work commit together. Existing graph-backed Long-term updates
also refresh their upstream graph assertion in the guarded batch (0177); only
the admitted uid/item/revision may publish that assertion. Owner export and
privacy/account erasure include this graph store. Concurrent target deletion
rejects a review with 503 and no feedback/history write. See the
[product-field verification record](../../dev/unified-main/implementation-2026-09-05/memory-product-mutation-2026-09-07.md).

Canonical review resolution now joins the same apply/privacy owners (0178).
Accept/correct return the reviewed candidate to pending Short-term without
invalidating its conflict peers. Reject and timeout/drop complete canonical
privacy erasure; pending provider cleanup returns 503 and resumes through the
existing durable inventory. Reads and transactions require the exact canonical
commit, revision, hash and consolidation review route. Historical timestamp
rows are stale projections; native intake no longer manufactures review
candidates from structural conflicts. Producing real canonical review decisions
still depends on the unfinished consolidation owner. See the
[review-resolution verification record](../../dev/unified-main/implementation-2026-09-05/memory-review-canonical-2026-09-07.md).

The other intake and mutation families, consolidation,
source-deletion and projection writers still
need to converge on this transaction owner. History/revert and JIT require that
complete authority; their inventory states are unchanged. See the
[kernel verification](../../dev/unified-main/implementation-2026-09-05/memory-kernel-2026-09-06.md)
and [native intake transaction record](../../dev/unified-main/implementation-2026-09-05/memory-apply-intake-2026-09-06.md).
