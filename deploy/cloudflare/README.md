# Omi Cloudflare Workers

This directory is the Worker-first deployment surface described in
[`dev/cloudflare-adaptation-plan.md`](../../dev/cloudflare-adaptation-plan.md).
It intentionally does not import the monolithic `backend/main.py`.

The first staging slice contains:

- `edge`: public routing, request IDs, trusted auth context and legacy fallback.
- `auth`: Hono + Better Auth + D1, with request-scoped auth construction.
- `api-core`: a minimal FastAPI/Python Worker composition root and D1 probe.
- `api-core`: a minimal FastAPI/Python Worker composition root, D1 probe, and
  uid-scoped R2 asset API (`/v1/cf/assets/{key}`).
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
```

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
```

The AI and realtime paths are API-first. They intentionally return `503` until
their provider is configured; no ASR/model process runs inside a Worker. Add
the provider endpoint/key as Worker secrets when a staging provider is chosen:

```bash
printf '%s' "$ASR_WS_URL" | npx wrangler secret put ASR_WS_URL --name omi-cf-realtime-staging
printf '%s' "$ASR_API_KEY" | npx wrangler secret put ASR_API_KEY --name omi-cf-realtime-staging
printf '%s' "$ASR_API_BASE_URL" | npx wrangler secret put ASR_API_BASE_URL --name omi-cf-api-ai-staging
printf '%s' "$ASR_API_KEY" | npx wrangler secret put ASR_API_KEY --name omi-cf-api-ai-staging
printf '%s' "$EMBEDDING_API_BASE_URL" | npx wrangler secret put EMBEDDING_API_BASE_URL --name omi-cf-api-ai-staging
printf '%s' "$EMBEDDING_API_KEY" | npx wrangler secret put EMBEDDING_API_KEY --name omi-cf-api-ai-staging
```

Do not point these commands at production names from this worktree. The
staging smoke surface is:

```text
GET  /health                  all five Workers
GET  /ready                   auth D1 readiness
POST /api/auth/sign-up/email  Better Auth + D1
GET  /v1/cf/probe             Edge → Auth → Python API Core → D1
POST /v1/stt/transcribe      Edge → Python API AI → hosted ASR API
WS   /v4/listen               Edge → Realtime → Durable Object → ASR API seam
R2   /v1/cf/assets/{key}      Edge → Python API Core → R2 + D1 metadata
JOB  /v1/cf/jobs              Edge → Jobs Worker → Queue → idempotent D1 ledger
```

The initial queue accepts only the `probe` kind as an infrastructure contract.
Unknown kinds are acknowledged as failed and recorded in D1; producers must
use a stable `jobId`, so retry or duplicate delivery cannot create a second
logical job.
