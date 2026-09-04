# Fork runtime ownership

`python -m fork.migrate migrate` owns PostgreSQL schema changes.
It requires an explicit `FIRESTORE_PG_DSN`; `check` performs read-only admission.
Schema v4 adds legal-hold/deletion-gate authorities; v3 registers frame requests/keyframe jobs and chat-first dead letters;
v1/v2 physical table mappings remain immutable.

`uvicorn fork.main:app` calls `bootstrap(Role.API)` before importing upstream's
`main.app`. `python -m fork.worker` calls `bootstrap(Role.WORKER)` without loading
ASGI/model modules, validates all four queue destinations and credentials, and
supervises one process per queue. Any unexpectedly completed child fails the
supervisor. `--check` validates admission and Redis connectivity without consuming.

`profile.py` reads the image's generated `deployment_profiles.generated.json`.
Build with `render.py --target self_hosted --manifest ... --stage ... --emit-json`;
select an exact row with `OMI_DEPLOYMENT_PROFILE=self_hosted.production` (or
`self_hosted.beta`/`self_hosted.local`). Conflicting target, brand or adapter
settings fail before workload import. `bootstrap.py` projects this choice into
the environment switches still consumed by upstream seams and checks the PG
schema. Omi-cloud API mode applies no patches or self-host configuration.

Do not use `sitecustomize` for admission: Python can continue after its import
fails. New process types must call the same explicit bootstrap before importing
or executing their workload. Tests execute real subprocess entrypoints, including
bad profile/dependency/patch cases; do not substitute source-order assertions.

`queue_config.py` owns the four handler paths and separate credentials. Producer
and authentication patches live in `patches/queue.py`; a worker receives only its
queue's selected credential through the internal adapter setting. This package
also owns MinIO/storage and speaker provider patches. It does not claim the
remaining model or push adapters are complete; refer to the dated audit.

Run fork tests through `backend/test.sh` with an explicit file list; the fork
manifest runs startup and source-closure contracts in both local and CI lanes.
Run live PostgreSQL tests only against a disposable target and record their
results separately from the hermetic lane.

`patches/auth.py` preserves Better Auth's invalid versus unavailable outcomes
through upstream HTTP dependencies and WebSocket close-code selection. It only
activates for self_hosted; omi_cloud keeps the upstream Firebase/admin behavior.
Self-host identity always comes from the validated shim, never ADMIN_KEY prefix
impersonation. AUTH-1 owns cryptography and the session-revocation authority.

`auth_identity.py` replaces the existing worker's Firebase deletion call for
self_hosted only. It shares `auth_shim.internal_authority()` with live-session
verification. A successful deletion or explicit user-not-found response must
be followed by exact zero `users`/`sessions`/`accounts` counts. The final
`provider_guard.complete` wrapper repeats identity proof before the PG receipt
transaction. Provider purge remains its own phase; it does not delete identity.
Lost responses, malformed counts, wrong credentials and outages raise a bounded
retryable error. The unchanged worker records failure and retries its durable
intent. This does not certify other provider families or every PG writer.

`auth_transport.py` ensures self-host retryable WebSocket errors (1013) cross the
actual upgrade boundary: accept then immediately close, with no application data.
Other close classifications and the upstream mode retain their existing policy.


Self-host account deletion is owned by `account_deletion.py` and
`firestore_pg/erasure.py`, attached through `patches/account_deletion.py` before
upstream routers/services import. The marker-to-receipt transaction, receipt-aware
status and retries must evolve together; do not add missing historical aliases
to upstream modules. See `../firestore_pg/README.md` for ownership, key retention,
control-state exclusions and live-versus-hermetic verification boundaries.


`vector_qdrant.py` implements the existing `database.vector_db.index` boundary.
Compose runs its `migrate` CLI before serving; `check` and API admission require
all seven collections to have the selected dimension and Cosine distance. A
changed dimension requires a reviewed new prefix and backfill, never destructive
in-place recreation. No Pinecone fallback is allowed. `vector_filter.py` maps
only the filter operators used by this upstream revision and rejects unknown
syntax. UUID points retain their upstream string IDs as payloads.

`storage_minio.py` / `storage_minio_blob.py` implement the actual audio/cache
surface. `MINIO_ENDPOINT` is the internal origin; `MINIO_PUBLIC_ENDPOINT` is the
public S3 origin used when signing (scheme controls TLS). Credentials are required.
Only authoritative 404 means missing; access/transport failures propagate. Private
links use path-style SigV4 GET and a bucket/origin/path/duration-scoped Redis key.
Unsigned `public_url` is for operator-provisioned public assets, not a permission
grant; the adapter never makes private buckets public.

`provider_guard.py` patches the canonical external-write fence and all five
captured imports, plus the real MinIO write gate even in local stage. They consult
the same completion receipt owner as HTTP. Shared PostgreSQL advisory transaction
locks cover admitted writes; the full wipe and its final provider proof take an
exclusive lock. Contention fails for queue retry. The same-thread nested wipe /
completion path reuses that ownership. `provider_objects.py` owns the declared
UID-prefix inventory for purge and verification; Qdrant sweeps all namespaces by
metadata.uid, including records absent from PG inventories. A failed proof retains
the recoverable marker. This is not a distributed transaction after a lost PG
connection or unknown remote write outcome. Direct PG writers and unowned/global
object paths remain separately tracked; see the dated SH2 provider evidence.
