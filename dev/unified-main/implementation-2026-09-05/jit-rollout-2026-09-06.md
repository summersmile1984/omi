# Cloudflare JIT rollout decision — 2026-09-06

`GET /v1/jit/rollout-decision` is now available through authenticated Edge/Core.
It uses the existing `jit_authority.py` D1 provider and the unchanged upstream
decision policy staged from `backend/utils/jit_rollout.py`. The provider and
decision route were already used by frame requests; this change supplies the
public client boundary, its route inventory and end-to-end evidence.

The caller cannot choose another owner through query parameters or identity
headers. Edge verifies the session and cutover admission, strips caller identity
headers and signs the exact Core method/path. Core reads current owner/default
flags and account generation in one D1 snapshot. A global enabled kill switch
dominates an owner override. Missing rollout is disabled for an ordinary owner;
provider failure remains unknown with sanitized shared telemetry. The original
allowlist policy is preserved, including its known-kill override. Deletion
fences still deny access. Responses are `no-store`, with no decision cache.

No flag values or production settings were enabled by this change. No prompt,
model or upstream implementation was changed. The decision read does not reserve
a budget or supply trigger/ledger state: the other five JIT identities remain
blocked. The overall inventory is **602 staging-owned / 17 blocked / 619 total**.

## Verification

- Core's actual ASGI route against all 174 App migrations: the three new decision
  tests and existing frame metadata suite passed **22 tests**. They cover original
  wire fields, missing/owner/global flag transitions, query override rejection,
  provider unknown/error sanitization, request identity and account deletion.
  Existing tests cover legacy principals, the upstream allowlist, dominant kill
  and mutation-time generation/flag changes.
- Three new Edge tests execute the production handler with controlled transport:
  signed owner/path identity and stripped credentials, unauthenticated denial and
  cutover denial. Full Worker suite: **123 files / 980 passed**. Full Core suite:
  **733 passed**, with one existing Starlette/AnyIO deprecation warning.
- `npm run typecheck`, `npm run validate:backend-routes` (619 actual upstream
  HTTP/WebSocket identities), the manifest validator in `npm test`, Python and
  TypeScript formatting checks, and `git diff --check` passed.
- The shared `contracts/deployment/core.py` gained one authenticated tri-state
  decision case. Its complete **16-case suite passed on both targets**. Server
  used the already running, disposable `memweft-contract-60d8eed8a542` Docker
  environment on 34880/34881. Cloudflare used a fresh local workerd environment
  on 34920, started by `contracts/local-target.mjs --run-core` and fully closed
  by that runner. Both used real public signup/session/JWT and normal HTTP APIs;
  the shared client seeded no business database and supplied no auth bypass.
  The local CF model transport remains controlled; this is state/API evidence,
  not a new model-quality or complete release claim.

The hosted run `eddy-jit-20260906-a` deployed four owned Workers (Auth, Rate Limit,
Core and Edge) plus fresh D1 databases with ten Auth and 174 App migrations. Its
Edge wrapper only restricts the diagnostic deployment to a private probe key;
the business request uses the unchanged production Edge/Core handlers and real
Auth JWTs. Flag rows were deliberately seeded in the owned synthetic D1 database.

All **24 HTTP assertions passed**, including **14 JIT requests**: unauthenticated
denial, absent flags, separate owner overrides, rejected query/header spoofing,
global defaults, owner disable, global kill overriding owner state, immediate
uncached updates, deletion of an override restoring the global default, and
logout invalidating the old JWT while the other owner remains admitted. No model
call or email was made. This does not test trigger execution or ledger snapshots.

The run finished at `2026-09-06T12:53:21.189Z`. All four Workers and both D1
databases were observed absent after cleanup. Journal SHA-256:
`8708e544767672ee62f28f426046f56c66ee3283d5849d006d52e7f093df5f6b`.
The frozen Core route is byte-identical to current source; the Edge registration
was not modified after this hosted execution.

Private evidence under `$CODEX_HOME/eddy-production/`:

- `jit-rollout-hosted-20260906-a/result.json`
- `jit-rollout-local-20260906-a/trace/core-results.json`
- `jit-rollout-server-20260906-a/trace/core-results.json`
- `jit-rollout-core-final-20260906.log`
- `jit-rollout-workers-final-20260906.log`

## Production objective remains open

A fresh read-only observation at `2026-09-06T12:54:48.904Z` still found all nine
Eddy production Worker names absent. The existing Eddy macOS bundle again passed
`codesign --verify --deep --strict`, but live production login, capture,
processing and retrieval remain unverified. Remaining route implementations,
the CF-4 product and CI-1 dual-target qualifiers, a current frozen release
candidate and the actual production/macOS end-to-end run remain required.
