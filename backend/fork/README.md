# Fork runtime ownership

`python -m fork.migrate migrate` owns PostgreSQL schema changes.
It requires an explicit `FIRESTORE_PG_DSN`; `check` performs read-only admission.
Schema v8 admits retained frame-vision receipts for complete export; v7 adds feedback ledgers. Schema v6 adds canonical-memory collection admission; v5 adds backend onboarding admission; v4 adds legal-hold/deletion-gate
authorities; v3 registers frame requests/keyframe jobs and chat-first dead letters.
Earlier schema ledgers and physical table mappings remain immutable. Dynamic
onboarding and legal-hold owners run against the shared strict admitted-schema
fixture in `tests/test_pg_owner_inventory.py`, included in the startup lane.

`uvicorn fork.main:app` calls `bootstrap(Role.API)` before importing upstream's
`main.app`. `python -m fork.worker` calls `bootstrap(Role.WORKER)` without loading
ASGI/model modules, validates all four queue destinations and credentials, and
supervises one process per queue. Any unexpectedly completed child fails the
supervisor. `--check` validates admission and Redis connectivity without consuming.

`python -m fork.memory_maintenance_worker` is the separate standard Server OS
owner for canonical-memory projection delivery. It pages only the existing
content-free canonical maintenance registry, then calls the existing leased
outbox drain for each UID. PostgreSQL source replacement, outbox status, retry,
dead-letter, Typesense writes and Qdrant writes retain their existing owners;
this process adds only bounded scheduling and Compose supervision. It applies
only the embedding/vector and captured provider-fence seams, with no Redis,
ASGI, Auth, storage, TTL, consolidation, or chat-model dependency. `--once`
fails when a bounded pass reports a provider or acknowledgement failure.
The registry page cursor is process-local and wraps in UID order; restarting
the process restarts discovery at the first page but cannot lose or acknowledge
the durable outbox. The standard deployment runs one supervised instance.

`profile.py` reads the image's generated `deployment_profiles.generated.json`.
Build with `render.py --target self_hosted --manifest ... --stage ... --emit-json`;
select an exact row with `OMI_DEPLOYMENT_PROFILE=self_hosted.production` (or
`self_hosted.beta`/`self_hosted.local`). Conflicting target, brand or adapter
settings fail before workload import. `bootstrap.py` projects this choice into
the environment switches still consumed by upstream seams and checks the PG
schema. Omi-cloud API mode applies no patches or self-host configuration.

`brand_transport.py` brands successful account-export download metadata from the
generated image identity (`eddy-export.json` for Eddy). It resolves the actual
upstream export route at startup and leaves authentication, export spooling,
response status and body delivery with their existing owners. Omi-cloud mode
retains upstream download behavior; request headers cannot select a brand.

Do not use `sitecustomize` for admission: Python can continue after its import
fails. New process types must call the same explicit bootstrap before importing
or executing their workload. Tests execute real subprocess entrypoints, including
bad profile/dependency/patch cases; do not substitute source-order assertions.

`queue_config.py` owns the four handler paths and separate credentials. Producer
and authentication patches live in `patches/queue.py`; a worker receives only its
queue's selected credential through the internal adapter setting. This package
also owns MinIO/storage and speaker provider patches. It does not claim the
remaining model or push adapters are complete; refer to the dated audit.

Server OS images install `backend/requirements-fork.txt` over the unchanged upstream runtime lock. It owns the hash-pinned PostgreSQL, MinIO and local-speech wheels; Cloudflare does not consume it. Run fork tests through `backend/test.sh` with an explicit file list; the fork
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
all seven collections to have the exact public embedding contract metadata,
its derived dimension and Cosine distance. An unbound collection or any changed
model identity (even at the same dimension) requires a reviewed new prefix and backfill, never destructive
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


`model_contract.py` is the dependency-free owner shared by the renderer, model
store admission and vector migration. `deploy/profiles/self_hosted.yaml` pins
BGE-M3's manifest/GGUF SHA-256, model, 1024 dimensions and 8192-token context.
`embedding.py` binds both canonical/captured consumers, verifies the runtime
model inventory and metadata, and uses native Ollama `/api/embed` without
truncation. It requests four CPU threads, a 128-token evaluation batch, and
read-only mmap; there is no model download, BYOK or vendor fallback. Async
consumers use the repository's bounded `llm_executor`.

`model_store.py` verifies the pinned manifest and every referenced blob before
Compose starts Ollama. Collection creation stores the complete model identity
atomically. Operators must use a reviewed new prefix and backfill for identity
changes; never bind old vectors by patching metadata in place.

`speech.py` / `speech_assets.py` admit the complete pinned SenseVoice/Kokoro
bundle before serving. The shared `model_contract.py` includes the runtime,
both archive hashes, full unpacked inventory hash and existing local VAD hash.
`patches/speech.py` binds canonical and captured STT consumers; the existing
SenseVoice adapter now normalizes PCM, bounds input and reports failed drains.
`speech_transport.py` preserves actual FastAPI dependencies while adapting both
TTS routes and the PTT WebSocket. Models run on shared executors with two CPU
threads, no runtime download, vendor credentials or provider failover.

`capabilities.py` / `capability_transport.py` derive enabled speech from that
bundle. A profile without it retains nonretryable HTTP 503 and WS 1008
(`stt_disabled`) before audio acceptance; background selectors also reject.
Push remains disabled: public token/send routes refuse; internal delivery counts
return zero with shared telemetry, preserving committed Tasks with `due_at`.
See `deploy/self-host/model-runtime.md` and `speech-runtime.md` for deployment,
wire limits and the distinction between model inference and full recording E2E.

The BYOK error handler also dispatches through the disabled notification owner,
including requests with an existing enrolled key. `allow_byok: false` is not
proof that this context is unreachable. Disabled errors neither look up/prune
FCM tokens nor acquire a cooldown, and retain the upstream void return without
logging a successful delivery. Shared telemetry uses its registered `pusher`
component. The real sync/async error handlers are covered by the startup suite.

The startup lane also executes the older fork-owned cloud-neutral storage/queue
fixture. MinIO fixtures declare internal/public origins, credentials and region
explicitly and check both transfer and signer cache refresh; inherited ambient
configuration is never a substitute for the current runtime contract.


`local_llm.py`, `local_llm_chat.py` and `patches/llm.py` bind every admitted
text feature to the profile-selected Ollama artifact and retain the upstream
user-scoped chat/tool loop. Startup and each generation verify the exact runtime,
manifest, GGUF source, native context and capabilities. The serving context/output
limits remain distinct from artifact metadata. Requests are byte-bounded, disable
implicit truncation/context shifting, and fail typed rather than yielding empty
success. `llm_http.py` bounds socket operations with one deadline; it does not
claim to interrupt standard-library DNS lookup. `llm_usage.py` delegates completed
native token counts to the existing usage owner and never charges failed streams.

`llm_runtime.py` compiles the generated profile into the thin Ollama image's
precision, context and concurrency settings. The same owner projects model,
chat and worker budgets before imports; four CPU threads and one generation
are the admitted reference. The memory feature uses the unchanged upstream
`WorkingObservationBatch` schema through the canonical/captured route-options
factory, so native structured output and the production parser agree.
