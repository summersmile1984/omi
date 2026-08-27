# Omi Cloudflare Workers

This directory is the Worker-first deployment surface described in
[`dev/cloudflare-adaptation-plan.md`](../../dev/cloudflare-adaptation-plan.md).
It intentionally does not import the monolithic `backend/main.py`.

The first staging slice contains:

- `edge`: public routing, request IDs, trusted auth context and legacy fallback.
- `auth`: Hono + Better Auth + D1, with request-scoped auth construction.
- `api-core`: a minimal FastAPI/Python Worker composition root with a D1 probe,
  uid-scoped R2 asset API (`/v1/cf/assets/{key}`), uid-scoped transcription
  preferences, onboarding/privacy/notification/location-consent settings, and
  the public firmware stable/latest/version APIs.
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
- Queue: `omi-cf-jobs-staging`
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

`smoke:staging` checks Edge health by default. To enable the authenticated
checks, provide a staging Better Auth token through an environment variable or
an explicit JSON token file:

```bash
CLOUDFLARE_SMOKE_TOKEN_FILE=/tmp/cf-auth-signup.json npm run smoke:staging
```

To deliberately exercise billable native TTS as part of that authenticated
smoke, add `CLOUDFLARE_SMOKE_NATIVE_TTS=1`; the check asserts a non-empty
`audio/mpeg` response and is opt-in.

The authenticated smoke verifies unauthenticated rejection, the D1 probe, and
the Workers AI raw-audio input boundary. It deliberately sends an empty body,
so it does not invoke billable model inference; use a separate explicit audio
request for model quality or latency qualification.

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

Do not point these commands at production names from this worktree. The
staging smoke surface is:

```text
GET  /health                  all deployed Workers
GET  /ready                   auth D1 readiness
POST /api/auth/sign-up/email  Better Auth + D1
GET  /v1/cf/probe             Edge → Auth → Python API Core → D1
POST /v1/stt/transcribe      Edge → Python API AI → hosted ASR API
POST /v1/stt/transcribe-workers-ai
                              Edge → Python API AI → Workers AI binding (raw audio)
POST /v1/translate           Edge → Python API AI → Workers AI m2m100 translation
POST /v1/tts/synthesize      Edge → Python API AI → hosted OpenAI-compatible TTS API
POST /v1/tts/synthesize-workers-ai
                              Edge → Python API AI → Workers AI Aura binding
GET  /v1/auto/model-pick    Edge → Python API AI → Artificial Analysis API + D1 cache
GET/POST /v1/ai/*           Edge → Python API AI → fixed OpenAI-compatible AI API
WS   /v4/listen               Edge → Realtime → Durable Object → ASR API seam
R2   /v1/cf/assets/{key}      Edge → Python API Core → R2 + D1 metadata
JOB  /v1/cf/jobs              Edge → Jobs Worker → Queue → idempotent D1 ledger
GET  /v1/cf/jobs/{jobId}      Edge → Jobs Worker → uid-scoped D1 job status
GET  /v2/firmware/stable      Edge → Python API Core → GitHub Releases API
GET  /v2/firmware/latest      Edge → Python API Core → GitHub Releases API
GET  /v2/firmware/version     Edge → Python API Core → GitHub Releases API
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
GET  /v1/users/assistant-settings
PATCH /v1/users/assistant-settings
GET  /v1/users/ai-profile
PATCH /v1/users/ai-profile
                              Edge → Python API Core → D1
GET  /v1/users/profile         Edge → Better Auth → D1
```

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

`/v1/auto/model-pick` uses a shared D1 24-hour cache. Without the upstream key,
an upstream failure, or an unusable model response it returns the existing
Gemini default with a provenance reason rather than failing the voice session.

`/v1/ai/*` is an authenticated, fixed-host proxy for OpenAI-compatible AI APIs.
The client cannot choose the destination: `AI_API_BASE_URL` and `AI_API_KEY` are
Worker secrets, and the proxy only forwards `content-type`/`accept` plus the
request path after `/v1/ai`. Requests and responses are bounded to keep model
payloads from turning the Python Worker into an unbounded buffer.

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

The initial queue accepts only the `probe` kind as an infrastructure contract.
Unknown kinds are acknowledged as failed and recorded in D1; producers must
use a stable `jobId`, so retry or duplicate delivery cannot create a second
logical job. `GET /v1/cf/jobs/{jobId}` exposes the state machine without
returning payload data, and requires the same authenticated uid that created
the job.
