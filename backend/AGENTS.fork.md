# Backend — Fork Rules (cloud-neutral / self-hosted)

Upstream rules live in [`AGENTS.md`](./AGENTS.md); this file adds only what is
true for this fork. It is fork-owned: upstream never touches it, so it never
conflicts on an upstream sync.

Canonical mutation guidance lives in
[the fork transaction guide](../.github/agent-docs/backend-memory-transactions.md).
`fork/canonical_mutations.py` adds changed patch arguments before upstream hashes
the operation. Register both public and internal mutation entrypoints; do not
edit the upstream adapter or its tests. The startup lane exercises feedback,
receipt replay and conflicting reuse through the original apply owner.
`fork/memory_operation_clock.py` scopes a nondecreasing clock to the original
operation transition body; persisted-record decoding and terminal-state guards
remain upstream-owned. It is installed for Server API/maintenance and copied by
the Cloudflare kernel projection. The startup and CF intake lanes both exercise
clock regression (the observed Workers regression was 999 microseconds).

## Cloud-neutral runtime switches

The image contains one generated target/stage/brand table. `fork.bootstrap`
admits the API or worker before workload import, then projects the selected row
into the environment switches consumed at adapter call boundaries:

| Variable | Effect |
|---|---|
| `FIRESTORE_PG_DSN` | Routes **both** customer and compute data through the `firestore_pg` PostgreSQL facade. Never split one process between PostgreSQL and Firestore. |
| `STORAGE_BACKEND=minio` | Selects the GCS-compatible MinIO adapter instead of Google Cloud Storage. |
| `VECTOR_STORE_PROVIDER=qdrant` | Routes the existing vector index to the explicitly migrated Qdrant collections; dimension/schema mismatch is fatal. |
| `QUEUE_BACKEND=redis` | Selects the Redis worker queue instead of Cloud Tasks; each queue authenticates with its `QUEUE_REDIS_{SYNC,AUDIO_MERGE,ACCOUNT_DELETION,FINALIZATION}_WORKER_SECRET`. |
| `AUTH_PROVIDER=better_auth` | Verifies asymmetric JWTs fetched from `AUTH_JWKS_URL` instead of Firebase ID tokens. |

`get_firestore_client()` from `database._client` remains the only supported way
to obtain a client, and `FIRESTORE_PG_DSN` changes what it returns for the whole
runtime, including customer entitlement and quota reads.

## Self-hosted identity

`AUTH_PROVIDER=better_auth` is the self-hosted path. JWT verification accepts
only asymmetric ES256/RS256/EdDSA keys from `AUTH_JWKS_URL`. The optional
auth-server `/auth-issue` development bridge must stay disabled unless it is
protected by `AUTH_DEV_ISSUER_SECRET`, and it must never be reachable in a
production deployment.

The self-host deletion worker's existing `endpoints.delete_account` seam calls
`fork/auth_identity.py`, using the same trusted internal origin/secret as live
session verification. A missing user is accepted only after exact zero counts
for users, sessions and accounts; final receipt publication checks again.
Unknown responses and outages retain retryable deletion state. See
`fork/tests/test_auth_identity.py` and the shared AUTH local/CI lane.
The same adapter binds `database.auth._firebase_get_user` for profile reads,
including functions captured by model and conversation consumers before startup.
Only an authoritative missing-user response returns absence; malformed/outage
results emit sanitized shared fallback telemetry before the existing optional
profile/default-name path. Upstream mode retains its original SDK owner.
`fork/referral_transport.py` uses that same identity owner and its validated
creation time before calling the upstream referral transaction. Missing creation
metadata keeps imported accounts ineligible; authority faults return retryable
503. Public invitation and signup destinations come from the admitted profile.
The existing auth contract lane executes these registered HTTP routes; the shared
product lane grants and rereads the trial through real Server and CF runtimes.

## Fork discipline

Do not modify upstream files under `backend/`. Fork behavior belongs in
fork-owned modules and is attached at startup; see
[`dev/unified-main/00-upstream-touch-policy.md`](../dev/unified-main/00-upstream-touch-policy.md)
for the technique catalog and the allowlist that CI enforces.

## Explicit startup and schema admission

Use `uvicorn fork.main:app` for the API and `python -m fork.worker` for all queue
consumers. Do not install a `sitecustomize` hook: ordinary import errors there do
not stop Python. The worker does not import ASGI or model providers and exits if
any child consumer stops. Its `--check` command checks PG admission and Redis.
Canonical-memory provider outbox delivery runs separately as
`python -m fork.memory_maintenance_worker`. It may page only the existing bounded
  maintenance registry and must call the existing leased outbox worker/side-effect
  owners; never add another projection ledger, queue, or direct source write. Its
  startup role loads only embedding, Qdrant, Typesense, and the shared provider
  write fence, keeping Redis/ASGI/Auth/model generation outside this process.
  Discovery pagination is process-local; outbox documents and their existing
  leases remain the only durable delivery state.

Run `python -m fork.migrate migrate` once before either serving process; use
`check` for read-only validation. New static collections require a new explicit
schema version, never edits to prior frozen collection sets. The image and
startup runner are `deploy/self-host/Dockerfile` and `build-images.sh`; upstream
`backend/Dockerfile` remains the dependency/source base. See `fork/README.md`.
Dynamic document paths must also be admitted: execute their business owners
through `fork/tests/schema_firestore.py` in `test_pg_owner_inventory.py`, already
registered in the startup local/CI lane. A literal collection scan alone misses
the onboarding and legal-hold paths that caused the recorded local incidents.
The backend/profile Docker context filters exclude local `.openapi-venv` and
`backend/_temp` state as well as `.venv`; the thin model filter admits only its
entrypoint. The existing Fork Checks workflow runs
`deploy/self-host/ci/build_context.py` locally and in CI using offline scratch
Docker builds; keep the intended runtime sources in each context.

The CSAT singleton and per-platform ratings require schema v9. Keep earlier
inventories frozen, and exercise `database.csat` through the schema-admitted
fixture; constant-based collection names are invisible to a literal scan.
The shared local/CI product contract exercises the public config/rating routes.

- Self-host auth consumers are patched before importing upstream routers. Preserve
  the shim's authority-unavailable classification: HTTP dependencies return 503
  with `auth_service_unavailable`/`retryable`, and WebSocket auth closes 1013.
  Invalid credentials remain 401; expired JWTs request WebSocket refresh (4001).
  First-message WebSocket auth uses the same outage classification. The self-host
  app accepts then closes only a retryable 1013 handshake so real clients receive
  the close code instead of Uvicorn HTTP 403. `fork/tests/test_auth_consumers.py`
  exercises the real dependencies through ASGI.


- Self-host deletion changes use `fork/account_deletion.py` and
  `firestore_pg/erasure.py`; the patch registry binds existing public users seams.
  Keep receipt publication, access gating and retry mutations under one owner.
  The startup local/CI gate runs `fork/tests/test_account_deletion.py`; exercise
  `firestore_pg/tests/test_transaction_semantics.py` separately against disposable
  PG for actual serialization/worker evidence. Never interpret provider stubs or
  a minimal completion receipt as proof of complete external-account erasure.


- The self-host Auth image's `self-host-runtime.mjs` selects NODE_ENV from the
  same SELF_HOST_STAGE used by profile generation; beta/production cannot be
  relaxed by ambient NODE_ENV. Compose and migration gates invoke this owner
  for both serve and migrate. Its tests run under the existing Auth contracts
  lane (`auth-server/test/self-host-runtime.test.js`).

- Provider changes use `fork/vector_qdrant.py`, `fork/storage_minio*.py` and
  `fork/provider_guard.py`. Keep receipt lookup, all captured write-fence imports,
  wipe lock and provider completion proof together. The existing startup local/CI
  lane runs vector/MinIO/provider hermetic contracts. Real PG/Qdrant/MinIO/Redis
  evidence remains separate; successful /ready is not account-erasure attestation.

- Model changes belong to the shared `fork/model_contract.py` owner and target
  profile, including both artifact identity and dimensions. Migrate fresh Qdrant
  collections with identical metadata before serving; no in-place rebind or
  independent EMBEDDING_DIMENSION is permitted. The startup lane includes model
  and disabled-capability behavior; real CPU/HTTP evidence remains separate.

- Standard Server canonical-memory projection is supervised by the dedicated
  memory-maintenance worker. A successful source transaction is not searchable
  until its projection and vector outbox events are delivered. Keep scheduling
  bounded by the neutral registry, retain existing lease/retry/dead-letter
  semantics, and make provider failures visible without acknowledging them.
  Full TTL/consolidation maintenance and retrieval qualification remain separate.

- PG write admission uses `firestore_pg/write_policy.py`, bound to the existing
  deletion authority by the self-host registry. Keep every set/create/update,
  transaction and batch behind that boundary, including old and proposed owners.
  External fences use `fork/deletion_read.py`, never a caller's stale transaction
  snapshot. The startup lane runs the hermetic write-policy contracts; actual
  PostgreSQL locks/snapshots require `firestore_pg/tests/test_deletion_write_fence.py`
  against a disposable database. Do not weaken control-collection identity or
  release writer locks before SQL commit.
  Nested mutation values are normalized by that same document owner before
  serialization; preserve merge siblings and reject forbidden delete placements.
  The startup lane includes `fork/tests/test_pg_nested_transforms.py`; live usage
  and contention evidence stays in the disposable PostgreSQL fence suite.

- Local speech uses the same `fork/model_contract.py` and profile owner as
  embeddings. Run `deploy/self-host/prepare-speech.py` before deployment; mount
  the verified bundle read-only via `SPEECH_MODEL_STORE`. Bootstrap pins model,
  thread, ITN, window and local-VAD configuration before imports. Do not restore
  vendor fallbacks or infer readiness from file existence. Existing startup
  local/CI tests cover normalized PCM, artifact faults, actual HTTP/WS consumers
  and disabled mode; native recordings remain separately verified evidence.

- `deploy/self-host/ci/product.sh` runs shared HTTP identity/onboarding/task
  cases through a fresh real Compose target in the existing fork local/CI lane.
  Its explicit speech-disabled profile and controlled embedding HTTP fixture
  qualify product state only; they do not qualify model inference, recording,
  supported client platforms or release. Read `contracts/deployment/README.md`.
  PTT terminal drain owns usage on finalize, disconnect, idle, limit and task
  cancellation. Charge accepted bytes once after a healthy drain; provider
  rejection/failure is not billable. Cancellation must not discard that tail.

- Finalization queue admission, canonical/captured dispatch helpers and replay
  belong to `fork/finalization_queue.py`. Reuse the finalizer entry in `QUEUES`;
  do not require fabricated GCP binding values for Redis. Publish job id and
  dispatch generation with one Redis command; PostgreSQL's existing lease and
  generation own duplicate handling. Never add a permanent Redis name set that
  can swallow a later replay. The startup lane covers old jobs, duplicate/new
  generations and rejected publication retaining a durable queued intent.

- Redis deliveries carry an authenticated, route-scoped `X-Omi-Queue-Retry-Count`.
  Missing counts on existing envelopes mean the first attempt. Worker and handler
  share `Queue.max_attempts()` and the existing task-attempt environment keys
  (1–100); finalizers reach the existing final-attempt/dead-letter policy.
  Exhausted transport failures and malformed envelopes remain in the same Redis
  queue's `:dead-letter` list for operator inspection; they are not successful
  business completions. The PG reconciler retains its independent recovery role.
  The startup lane executes the real finalizer route and all four authenticated
  queue consumers through controlled transport/storage seams.

- Local text generation uses the profile's sole `llm` contract and
  `fork/local_llm.py`; every upstream feature resolves through the patched
  captured factories. Keep native context, serving window and output limit as
  separate fields. `LLM_ENDPOINT` is an internal origin only. The runtime
  refuses BYOK, vendor/model fallback, implicit truncation and context shifting.
  Model HTTP responses and streams stay byte-bounded; synchronous socket
  operations share one monotonic deadline, but standard-library DNS resolution
  is not a cancellable absolute-deadline proof. Model usage is charged only from
  a completed terminal envelope through the existing usage callback.

  `fork/llm_runtime.py` derives the thin model image and API/worker budgets from
  this same profile. Keep the original memory extraction schema in the selected
  route-options factory and bind both canonical and captured consumers; never
  loosen the parser to fit a model response. The startup lane covers that real
  parser and finalization's terminal-status acceptance through controlled seams.

- The opt-in `self_hosted.local` MiMo profile replaces local `llm`/`speech`
  contracts with the sole `operator_ai` owner; embedding remains local.
  `fork/operator_ai.py` fixes model identities and the exact CN endpoint, while
  server-only credentials come from `MIMO_API_KEY` or `MIMO_SECRET_FILE`.
  `fork/mimo_listen.py` drains accepted ASR and its existing persistence owner
  before normal disconnect finalization. Keep default prompts and extraction
  policies unchanged. Setup and live-verification limits:
  [`deploy/self-host/mimo-local.md`](../deploy/self-host/mimo-local.md).
