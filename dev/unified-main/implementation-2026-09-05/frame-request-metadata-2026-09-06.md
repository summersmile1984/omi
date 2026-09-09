# Cloudflare frame-request metadata foundation

Core now registers frame creation, pending delivery, status and state changes,
plus the original JIT decision envelope. Edge remains closed for the whole
frame-request/JIT families. Inventory remains **619 slots / 586 staging owners /
33 blocked**. This change does not deploy Workers or migrate production D1.

## Ownership and preserved contracts

The ordinary builder and CPython test runner stage the exact upstream
`backend/models/frame_request.py`, the pure lifecycle policy with only its model
import redirected, and selected original JIT policy/dataclasses. The source
projector now includes decorators, so the original `@dataclass` constructors
execute in CPython and Pyodide. Source identity and the existing CI trigger cover
all three upstream owners. No upstream source was edited.

App D1 is the sole Cloudflare control provider. Global defaults and account
overrides are read together with the current account generation. A global kill
wins over an account override. The original pure allowlist/negative/error
policy remains unchanged; it does not substitute a different memory engine for
selected users. A provider failure reports sanitized fallback telemetry. Even
an allowlisted decision cannot admit work without an authoritative generation.
A pre-existing principal with no cutover row retains generation 0.

Migration 0167 and the metadata owner enforce logical-intent replay, original
request-ID hashing, minute buckets, terminal retry numbering, six-day maximum
TTL, eight active requests per device/generation and a single active/attached
request per conversation. Flag/generation snapshots are rechecked in the SQL
statement. Claim/cancel writes compare the prior state, and account deletion or
generation changes cannot create new work. Polling prunes bounded expired rows;
it does not delete external pixels. Export and the existing account residual/
purge registry now include frame metadata. Conversation deletion removes its
metadata in the same database mutation.

Upload and promotion are deliberately unavailable in this implementation. A
client-supplied storage identifier cannot cause a successful uploaded or attached
state. Temporary image normalization/storage, guarded promotion to conversation
photos, temporary/permanent reads, independent retention cleanup and durable
pixel erasure remain the next required boundary. The screenshot-adjudication
writer's approved-image namespace is not used for unadjudicated JIT pixels.

## Verification

- Core metadata regression suite: **19 passed**. Actual HTTP handlers and all
  App D1 migrations cover deterministic IDs/wire shape, expiry, retry/replay,
  owner/device isolation, legacy generation, kill/deletion/generation changes,
  quota, conversation exclusion/deletion, user export and sanitized provider
  failure. Two full HTTP calls interleave at a D1 statement: one test proves
  last-slot quota enforcement, another proves a deduplicated winner. A competing
  cancellation proves that a stale claim cannot overwrite the terminal state.
- Source projection/release identity: **2 files / 9 tests passed**. Exact wire
  and policy bytes are checked, decorated JIT policy executes, its kill overrides
  allowlist, and TTL bounds retain upstream behavior. Source changes invalidate
  an old candidate; output collisions are rejected.
- Actual local workerd: **29 HTTP calls passed** through a fresh dry-run build
  and frozen normal Core `entry.py`, Python/Pyodide and local D1 with every App
  migration. Only global provider defaults and a synthetic conversation were
  seeded. Four overlapping creates yielded one request and one non-deduplicated
  response. Delivery, device/owner isolation, claim/cancel/retry, quota,
  conversation deletion, global kill, generation change and deletion tombstone
  were exercised through real HTTP. No AI or R2 binding was present. This proves
  metadata transport and state behavior, not pixels, model quality, public Edge,
  Better Auth login or production. Owned runtime processes closed normally.
- Full `env PATH=<pinned Node 22 bin>:$PATH bash deploy/cloudflare/ci/routes.sh`
  exited **0**: **6 inventory tests, manifest/typecheck, 114 Worker files /
  924 tests, 611 Core tests and 150 AI tests passed**. The existing Starlette/
  AnyIO deprecation warning remains. Four protected prompt-related files are
  byte-identical to baseline `9b7e48dca2`; their SHA-256 checks were rerun.

The initial broad run found two integration details: deletion triggers needed
this repository's established `adf_i_`/`adf_u_` names, and inserting new residual
surfaces ahead of the existing first surface broke its positional fixture. The
new triggers now follow that naming contract, and the established first surface
is retained. The guard tests were not weakened. Native execution preceded this
trigger-name/registry-order alignment; final-schema behavior is exercised by the
full lane. No inference prompt was changed.

Private evidence under `/Users/macstudio/.codex/eddy-production/`:
`frame-request-metadata-focus-20260906.log`,
`frame-request-source-focus-20260906.log`,
`frame-request-metadata-routes-20260906.log`,
`frame-request-metadata-native-20260906.mjs`, and
`frame-request-metadata-native-20260906-a/{result.json,runtime.log,core-build.log}`.
The local probe's signing value is synthetic. No provider or production
credential is embedded in the probe or repository.
