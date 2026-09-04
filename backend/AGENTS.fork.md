# Backend — Fork Rules (cloud-neutral / self-hosted)

Upstream rules live in [`AGENTS.md`](./AGENTS.md); this file adds only what is
true for this fork. It is fork-owned: upstream never touches it, so it never
conflicts on an upstream sync.

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

Run `python -m fork.migrate migrate` once before either serving process; use
`check` for read-only validation. New static collections require a new explicit
schema version, never edits to prior frozen collection sets. The image and
startup runner are `deploy/self-host/Dockerfile` and `build-images.sh`; upstream
`backend/Dockerfile` remains the dependency/source base. See `fork/README.md`.

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
