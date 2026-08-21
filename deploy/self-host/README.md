# Self-host production profile

This is the production entry point for a deployment that keeps identity, data,
queues, object storage, vectors, LLM routing, embeddings, and pre-recorded STT
behind operator-owned boundaries. It is separate from `dev/docker-compose.dev.yml`:
the dev file remains the emulator harness and is reused by the migration gate.

## What runs

`compose.production.yml` runs the backend, Better Auth server, PostgreSQL,
password-protected Redis plus its durable queue worker, MinIO, Qdrant, and a
reviewed SearXNG search boundary. Every service has a health check. PostgreSQL,
Redis, MinIO, Qdrant, and backend sync staging use named persistent volumes. The
SenseVoice model directory is an explicit read-only host mount. Remote
base/state images are pinned by immutable multi-architecture digest as well as
a human-readable version tag. Every live acceptance start rebuilds backend and
Auth from the attributed checkout, embeds the exact Git commit/tree in each
image, binds a hash of the reviewed environment file to the running containers,
and rejects stale image/config identity after the full run.

`auth-migrate` is an explicit one-shot schema owner. It runs Better Auth's
Kysely migration plan, re-reads the schema, and fails unless the plan converges
to zero pending tables/columns. `auth-server` is admitted only after that
container exits successfully. This avoids relying on application startup to
silently create or update identity tables.

`firestore-pg-migrate` independently owns the forward-only Firestore shim
schema. It takes the PostgreSQL advisory migration lock, applies the version
ledger and collection registry, and performs a read-only current-schema check
before exit. Backend and queue-worker are admitted only after it succeeds;
their runtime Firestore clients contain no lazy DDL path.

The profile selects Qdrant explicitly for vector projections. The backend also
resolves an omitted `VECTOR_STORE_PROVIDER` to a typed unavailable vector
authority in neutral/self-hosted direct launches, rather than inheriting the
managed Pinecone default; an explicit Qdrant binding is still required for
normal self-host operation.

The profile deliberately does not ship a default inference vendor. Set
`GENERIC_OPENAI_BASE_URL` to an operator-selected OpenAI-compatible endpoint and
set its explicit model/key. Embeddings use that same generic provider boundary.
Incremental live STT is pinned to the mounted SenseVoice model. Its adapter
decodes bounded five-second PCM windows (and VAD utterance boundaries) in the
sync executor, so it emits before a recording ends without blocking the
WebSocket loop. `model.int8.onnx`, `tokens.txt`, and the locked `sherpa-onnx`
runtime are all required before a session is admitted. Pre-recorded
transcription and diarization are independently selected with
`STT_PRERECORDED_MODEL=mlx_moss_diarize`. The required
`MLX_MOSS_DIARIZE_ENDPOINT` is an operator-owned mlx-audio
`/v1/audio/transcriptions` route and `MLX_MOSS_DIARIZE_MODEL` is its exact model
id. Private targets may use HTTP; public targets require HTTPS plus
`MLX_MOSS_DIARIZE_API_KEY`. Official hosted MOSS and Omi hosts are rejected,
and there is no default URL, model, download, or hosted-MOSS fallback.
`STT_ROUTE_FALLBACK_TO_DEFAULT=false` prevents a missing local model from
falling through to any managed STT policy default.
Realtime multimodal sessions use the authenticated provider-neutral relay.
`REALTIME_PROVIDER=relay` requires an explicit compatible WebSocket URL,
server-only credential, provider id, model and exact target-host allowlist.
`REALTIME_RELAY_WIRE_PROTOCOL` is also mandatory (currently only
`openai_realtime_v1` is supported): the relay is byte-opaque, so this field tells signed clients
which upstream event dialect to speak while `REALTIME_RELAY_PROVIDER_ID` remains
descriptive metadata.
There is no official endpoint default. The profile validator rejects official
vendor hosts, and the relay limits each frame and session duration. Optional
integrations require separately configured services; the core profile does not
silently reach an official endpoint for them.

The backend also exposes two authenticated desktop model boundaries:

- `POST /v1/model-capabilities/embeddings` accepts at most 32 OCR, task, or
  Rewind inputs. OCR/Rewind must declare projection namespace `ns3`; task must
  declare `ns4`. The response carries provider, model, actual dimension, schema
  and active namespace version so consumers cannot persist an unversioned
  vector.
- `POST /v1/model-capabilities/tool-completions` accepts bounded messages,
  function schemas and inline PNG/JPEG/WebP data URLs. It returns either an
  assistant message or tool calls; the backend never executes client tools.
  Direct mode and gateway mode use the same configured feature route and
  bounded 429/5xx/timeout fallback policy. A missing optional direct fallback
  is reported but cannot borrow another provider's key or block a configured
  primary.

Push is an explicit optional capability. The checked-in self-host profile sets
`PUSH_PROVIDER=disabled`; an omitted value in a neutral/self-hosted profile has
the same result, even when Firebase credentials happen to be present in the
process environment. The FCM token-registration and notification endpoints
return HTTP 503 with the stable
`deployment_capability_unavailable/push_notifications/disabled_by_deployment`
payload. Background notification paths use the same gate before token lookup,
including data-only reminders, important-conversation updates, bulk/daily
notifications, and BYOK error alerts; they record the unavailable outcome and
do not read tokens, initialize or call Firebase, or mark a notification as
delivered. No generic webhook provider is implied by this profile. An operator
may opt into the separate
`PUSH_PROVIDER=webhook` bridge in an unmodified deployment overlay. It
requires an HTTPS `PUSH_WEBHOOK_URL`, a regular non-symlink mode-0600
`PUSH_WEBHOOK_SECRET_FILE` containing at least 32 printable bytes, and the
`omi.push.webhook.v1` request/`omi.push.receipt.v1` response contract. The
receiver owns the opaque device-token mapping and final mobile adapter; a
2xx response without a matching receipt is not success. Requests are
DNS-pinned, HMAC signed, bounded by the shared webhook semaphore/circuit
breaker, and retried only for transport/429/5xx failures with a stable
idempotency key. No Firebase credential or vendor endpoint is consulted on
this path. The checked-in profile remains disabled until the operator has
deployed and exercised that receiver.

An operator-owned webhook may be enabled only after the receiver has adopted
the following reviewed provider contract:

- public targets use HTTPS (private/loopback, metadata, CGNAT and arbitrary
  private targets are rejected by the runtime); the URL has no userinfo,
  query, or fragment;
- a secret-manager supplied file is regular, non-symlink, mode 0600, and is
  used for `HMAC-SHA256(timestamp + "." + body)`; it never appears in URLs,
  payloads, exceptions, or logs;
- requests use the shared bounded webhook client/semaphore and circuit breaker,
  a short fixed timeout, and retries only for transport/429/5xx failures with
  an idempotency key; permanent failures must not be reported as delivered;
- the receiver accepts `schema=omi.push.webhook.v1`, `event_id`, `user_id`,
  opaque `device.token`, notification/data fields, and idempotency headers;
  it returns JSON `schema=omi.push.receipt.v1`, the same `event_id`, a
  non-empty `receipt_id`, and `status=accepted|delivered`;
- the receiver maps user/device identities and owns final mobile delivery;
  `accepted` means durably admitted, not that a handset displayed it. Until
  this receiver contract is deployed and exercised, the stable typed
  `deployment_capability_unavailable` response remains the valid outcome.

`GET /v1/model-capabilities/realtime` and `POST /v2/realtime/session` report the
same relay selection. The client then connects to
`/v1/model-capabilities/realtime/relay` with its Bearer token and WebSocket
subprotocol `omi.realtime.v1`. The backend authenticates before opening the
allowlisted upstream, injects `REALTIME_RELAY_API_KEY` only upstream, and pipes
text/binary frames in both directions. `REALTIME_RELAY_WIRE_PROTOCOL` explicitly
declares that those opaque frames use the currently implemented
`openai_realtime_v1` dialect; clients must select their adapter from the returned
`wire_protocol`, not from `provider_id`. Configure the public reverse proxy to
support WebSocket upgrades on that path.
The assembled cutover probe also requires the minted session's `provider_id`
and `model` to equal `REALTIME_PROVIDER`/`REALTIME_MODEL`, and records the
configured relay origin, transport, and wire protocol. Acceptance binds those
fields to the runtime provider attestation, so a successful WebSocket marker
cannot authorize a relay or model different from the tested configuration.

App-icon image generation is deployment-selected:
the checked-in profile uses deterministic `local_template`, while
`openai_compatible` requires the operator-owned
`IMAGE_GENERATION_OPENAI_COMPATIBLE_{BASE_URL,API_KEY,MODEL}` contract. Public
endpoints require HTTPS, private endpoints may use HTTP, and official vendor
hosts are rejected. `FILE_CHAT_TRANSPORT=local_extraction` keeps originals in
the configured UID-scoped object store, extracts bounded supported documents
locally, and routes the answer through the generic chat capability.
`DESKTOP_VENDOR_PROXY_TRANSPORT=disabled` remains fail closed.

Web search uses `WEB_SEARCH_TRANSPORT=searxng` and the private
`http://searxng:8080` service origin. `searxng-settings.yml` enables JSON output
and keeps only the reviewed Wikipedia engine; it does not inherit the image's
default engine set. `SEARXNG_SECRET` is mandatory and injected at runtime. The
health check uses `/healthz`, so readiness causes no search or public traffic.
Only an explicit agent search during acceptance exercises the declared
Wikipedia egress. The backend never falls back to Omi's gateway or Perplexity.
For direct generic desktop chat, the backend searches only the bounded trusted
current-user instruction, labels returned snippets as untrusted public context,
and refuses search when private tool output is present or user authorization is
denied.

Speaker embeddings run locally with
`SPEAKER_EMBEDDING_PROVIDER=sherpa_onnx`. The operator must provision
`${SPEAKER_MODEL_HOST_DIR}/speaker.onnx`; Compose mounts only that file read-only
and the backend never downloads a model or constructs an HTTP request. The
cutover lane decodes the real checked-in PCM WAV with this mounted model and
requires one finite, nonzero, L2-normalized embedding before authorization.
This proves the local embedding capability, not full speaker enrollment/match
product parity; the evidence keeps that stronger claim false.

Speaker diarization is a separate hard cutover capability. Docker Desktop can
reach a host mlx-audio service at `host.docker.internal`; Compose also binds
that name to `host-gateway` for Linux compatibility. Production may instead
point at private HTTP or public HTTPS. The cutover gate mounts the
operator-provided `MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH` read-only, calls
`/v1/models`, requires the exact configured model id, then invokes the selected
production pre-recorded adapter on that real WAV. Authorization requires at
least two speaker IDs and at least two label transitions, in addition to the
independent enrollment embedding probe above. The mlx-audio API exposes no
model revision/cache provenance, so evidence explicitly does not source-attest
the external service. Pinning the model revision and maintaining its offline
cache are operator responsibilities; a production operator may bind those to
a separately reviewed policy artifact rather than infer them from this gate.
The same evidence records SHA-256 plus byte length (never host paths) for the
mounted SenseVoice model/tokens, Sherpa speaker model, and TTS model/tokens so
the local artifacts in the exact tested configuration remain independently
reproducible.

TTS is also deployment-selected. The checked-in example keeps
`TTS_PROVIDER=sherpa_onnx` and requires an explicitly provisioned, read-only
model/tokens/espeak data directory; no model is downloaded. The cutover gate
requires a real WAV synthesis through the public route.
`TTS_PROVIDER=openai_compatible` requires the operator-owned
`TTS_OPENAI_COMPATIBLE_{BASE_URL,API_KEY,MODEL,VOICE}` contract. Public targets
must use HTTPS, private HTTP is allowed, and official vendor hosts are rejected;
there is no implicit endpoint or credential fallback.
The assembled acceptance evidence records the effective TTS provider, model,
transport, and endpoint origin and refuses cutover if the public synthesis probe
does not match that reviewed runtime route.

Desktop update feeds are pointer-only in this profile. The fixed
`DESKTOP_UPDATE_LEGACY_FALLBACK=disabled` binding prevents a missing operator
pointer from triggering the legacy vendor release scan; until the operator
publishes a valid pointer/manifest for a channel, that channel remains
unavailable rather than serving a managed release. The backend also defaults
this fallback to disabled whenever `OMI_DEPLOYMENT_PROFILE` is neutral or
self-hosted, so a direct launch or stale container cannot turn an omitted
binding into a GitHub release request; managed profiles retain their
historical enabled default. The update-policy endpoint follows the same
boundary: `DESKTOP_UPDATE_DOWNLOAD_URL` is an optional operator-owned HTTPS
repair/installer page, and an active `desktop_update_policy/current` document
may provide an explicit operator-owned `download_url`. If neither is present,
the endpoint returns typed `availability=disabled` with `download_url=null`;
it never returns the managed `api.omi.me` URL in a neutral profile. Firmware
follows the same boundary: neutral
profiles default to typed `firmware_updates` unavailability until an explicit
operator manifest transport is configured.

The checked-in example sets `BACKEND_PLATFORM=linux/amd64` because the pinned
runtime lock includes `onnxruntime==1.19.0`, which has no Linux ARM64 wheel.
Apple Silicon Docker can build/run it through amd64 emulation; native ARM64
production requires a separately reviewed dependency-lock upgrade rather than
silently resolving a different package set.

## TLS and public origins are prerequisites

Put an HTTPS reverse proxy/load balancer in front of the bound ports:

- `PUBLIC_BACKEND_URL` -> `${SELF_HOST_BIND_ADDRESS}:${BACKEND_PORT}`
- `PUBLIC_AUTH_URL` -> `${SELF_HOST_BIND_ADDRESS}:${AUTH_SERVER_PORT}`
- `PUBLIC_MCP_URL` -> the backend MCP routes, with streaming/buffering disabled
- `PUBLIC_OBJECTS_URL` -> `${SELF_HOST_BIND_ADDRESS}:${MINIO_API_PORT}`
- `OMI_SHARE_BASE_URL` -> the operator's public share-link application

`PUBLIC_MCP_URL` is the operator-owned MCP origin. Compose derives
`MCP_RESOURCE_URL=${PUBLIC_MCP_URL}/v1/mcp/sse`, but a direct/non-Compose launch
must set either `MCP_RESOURCE_URL` or `PUBLIC_MCP_URL` explicitly. Neutral and
self-hosted profiles reject missing, malformed, or Omi-operated authorities;
the OAuth discovery, authorize, and token endpoints return typed HTTP 503
`deployment_capability_unavailable` instead of advertising or contacting
`api.omi.me`. Managed deployments retain their historical resource default.

`OMI_SHARE_BASE_URL` is a required operator-owned HTTPS origin (for example,
`https://share.example.net`). It must be an exact origin with no path,
credentials, query, or fragment, and must not be an Omi-operated host. The
backend refuses to mint share links when the self-hosted value is missing or
invalid. It also does not accept `h.omi.me` as an inbound share authority in a
neutral/self-hosted profile; this prevents a stale managed link from silently
crossing the deployment boundary. Managed deployments retain their historical
`https://h.omi.me` default.

All five URLs must be explicit HTTPS URLs. Better Auth tokens keep
`AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` on `PUBLIC_AUTH_URL`, so the client
contract never changes. JWKS fetches and privileged lifecycle calls use
`http://auth-server:3000` only inside the private Compose network, with the
production-explicit `AUTH_INTERNAL_ALLOW_HTTP=true` guard. The validator accepts
that opt-in only for single-label service names, loopback/RFC1918 addresses, or
`.internal`/`.svc` names; public HTTP remains fail-closed. Keep the bound address
private/loopback unless the reverse proxy host firewall supplies equivalent
isolation. Only client-facing DNS and TLS need hairpin reachability.

## Configure and start

```bash
cp deploy/self-host/.env.production.example deploy/self-host/.env.production
# Replace every REPLACE_* value. URL-encode the PostgreSQL password separately.
make self-host-config-check SELF_HOST_ENV=deploy/self-host/.env.production
deploy/self-host/compose-clean-env.sh \
  deploy/self-host/.env.production deploy/self-host/compose.production.yml \
  config --quiet
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
  deploy/self-host/operations.sh start
```

Every operational and acceptance Compose command goes through
`compose-clean-env.sh`. It removes every key declared by the reviewed env file
from the ambient host environment before invoking Compose, because shell values
otherwise override `--env-file`. Only the attributed source commit/tree/config
hash and the disposable cutover TLS overlay controls are explicitly preserved.
This prevents a host-exported model endpoint, model id, model mount, LLM setting,
or secret from making the assembled acceptance exercise a different effective
configuration than the final long-running containers.

Do not use the checked-in example as a runtime secret file. Back up the four
state-service volumes and the backend syncing volume before upgrades. Preserve
`ENCRYPTION_SECRET`, Better Auth secrets, and queue worker secrets across
restarts; changing them without a planned rotation breaks stored data or live
sessions/jobs. Completed-deletion access barriers are addressed by an HMAC of
the deleted UID under `ENCRYPTION_SECRET`, so losing or rotating that secret
without migrating those opaque receipt keys would also reopen old tokens; the
backend therefore fails closed when the secret is missing or too short.

Runtime validation rejects every `REPLACE_*` value, reserved `example.com`
public origins, a SenseVoice host directory missing `model.int8.onnx` or
`tokens.txt`, an unsafe/official mlx-audio endpoint, a missing MOSS model id,
and a missing real diarization acceptance WAV. `operations.sh` refuses the
checked-in example file outright and reruns this validation before every state
or health operation.
Cutover acceptance additionally sets `SELF_HOST_REQUIRE_ATTESTED_BUILD=true`:
it cannot start from an existing mutable application-image tag. The final
`runtime-evidence` check reads the content-addressed image ID, embedded source
labels, config-hash label, state, and health from each exact running container,
and records a schema-v1 provider attestation for the actual backend container.
That attestation binds the effective generic LLM model/origin, embedding
model/origin/dimension, realtime model/origin/wire protocol, and mlx-audio
model/origin/path to the reviewed Compose configuration. It requires a
content-addressed image plus exact Git commit/tree/config source identity;
`--provider-attestation` emits just this redacted record for change evidence.
Managed official hosts, missing models/origins, injected runtime bindings, and
credential-bearing fields fail closed. Operator-owned model/realtime/STT
services do not expose a signed revision through this gate, so the attestation
records `external_service_revision=null`, `external_model_revision=null`, and
`external_revision_attested=false`; it never turns a Git revision or model tag
into a fabricated service revision. The JSON evidence schema (v3) requires
this attestation rather than accepting only a MOSS health claim. Authorization
also requires the assembled diarization endpoint and model to equal that final
effective provider configuration. Before emitting evidence it also inspects
each running container's actual environment and rejects injected managed-
provider bindings (including Firebase, Google, Anthropic, OpenAI-compatible
vendor, Deepgram, Modulate, Pinecone, and Stripe/Twilio integrations) or
official vendor endpoint values, so a neutral Compose file cannot hide a
non-neutral workload.

## Operations: health, metrics, backup, restore and rollback

`operations.sh` is the single operational companion to this Compose profile.
It fails if the env/Compose configuration is invalid. `status` requires every
long-running service, including `queue-worker`, to be healthy. The worker's PID
1 supervisor now exits when any of its four queue loops returns or crashes, so
Compose restart policy can recover a partial consumer outage instead of the
old false-healthy state. `metrics` emits Prometheus text for container
up/health/restart state, per-queue ready/pending/DLQ depth, and PostgreSQL size.

```bash
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
  deploy/self-host/operations.sh status
OMI_SOURCE_GIT_COMMIT=<40-hex> OMI_SOURCE_GIT_TREE=<40-hex> \
OMI_RUNTIME_CONFIG_SHA256=<64-hex> \
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
  deploy/self-host/operations.sh runtime-evidence
# Emit only the redacted provider attestation (same live-container checks).
export OMI_SOURCE_GIT_COMMIT=<40-hex> OMI_SOURCE_GIT_TREE=<40-hex> \
  OMI_RUNTIME_CONFIG_SHA256=<64-hex> \
  SELF_HOST_ENV="$PWD/deploy/self-host/.env.production"
deploy/self-host/runtime-evidence.py \
  --compose-file deploy/self-host/compose.production.yml \
  --env-file "$SELF_HOST_ENV" \
  --expected-git-commit "$OMI_SOURCE_GIT_COMMIT" \
  --expected-git-tree "$OMI_SOURCE_GIT_TREE" \
  --expected-config-sha256 "$OMI_RUNTIME_CONFIG_SHA256" \
  --provider-attestation
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
  deploy/self-host/operations.sh metrics > /var/lib/node_exporter/textfile_collector/omi-self-host.prom
```

Every managed `operations.sh start`, including the post-restore start, stops
application callers and runs fresh disposable `auth-migrate` and
`firestore-pg-migrate` containers before Auth, backend, or worker admission. An
old successful one-shot container is never treated as migration evidence for a
newly restored database.

Backups quiesce backend/auth/worker traffic, create a PostgreSQL custom-format
logical dump, issue a synchronous Redis save, and archive the stopped
Redis/MinIO/Qdrant/backend-syncing volumes. Each artifact is streamed through
the checked-in `omi-backup-aead-v1` envelope (AES-GCM chunks with a random
per-backup salt and nonce); the operator must provide a separate, mode-`0600`
file containing exactly 32 random bytes through
`SELF_HOST_BACKUP_KEY_FILE`. There is no generated, default, environment-value,
or repository-held key. The schema-v3 SHA-256 manifest binds encrypted
artifacts to the source Git revision (and `verify-backup` rejects a manifest
from any other current checkout) plus stable fingerprints of the effective
backend/auth image strings, effective Compose configuration, and the Better
Auth/Firestore migration owners. The manifest contains ciphertext checksums and
non-secret envelope format metadata only; key bytes are never copied into the
backup directory, manifest, or logs. Verification authenticates every envelope
before it succeeds and rejects missing, malformed, changed, or non-private
artifacts. Store the runtime env/secrets separately: they are deliberately
never copied into a backup directory.

Create and protect the key outside the repository. An operator may use a
secret manager to materialize this file for the duration of the operation; the
repository does not claim that this is a production KMS integration or a
completed restore drill:

```bash
umask 077
openssl rand -out /secure/omi-self-host-backup.key 32
chmod 600 /secure/omi-self-host-backup.key
export SELF_HOST_BACKUP_KEY_FILE=/secure/omi-self-host-backup.key
```

```bash
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
SELF_HOST_BACKUP_KEY_FILE=/secure/omi-self-host-backup.key \
  deploy/self-host/operations.sh backup /srv/backups/omi/2026-08-20T120000Z
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
SELF_HOST_BACKUP_KEY_FILE=/secure/omi-self-host-backup.key \
  deploy/self-host/operations.sh verify-backup /srv/backups/omi/2026-08-20T120000Z
```

Restore overwrites live state and is therefore fail-closed behind an explicit
acknowledgement. PostgreSQL is dropped and recreated before `pg_restore`, so
objects created after the backup cannot survive merely because they were absent
from the archive. Drain the three public routes at the reverse proxy first,
keep the previous images/env file, then run:

```bash
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
SELF_HOST_BACKUP_KEY_FILE=/secure/omi-self-host-backup.key \
SELF_HOST_RESTORE_ACK=I_ACKNOWLEDGE_THIS_OVERWRITES_STATE \
  deploy/self-host/operations.sh restore /srv/backups/omi/2026-08-20T120000Z
make self-host-migration-gate
```

For rollback, first move traffic back to the retained release. Verify the
pre-upgrade backup with `operations.sh rollback-plan BACKUP_DIR`; restore it
only if the old release cannot read the upgraded state. After restore, the
one-shot Better Auth migrator runs before Auth admission, and all services must
pass `status`, the auth smoke, and the migration gate before traffic returns.

### Recovery-drill evidence (operator runbook)

The repository tests the envelope, manifest, wrong-key/tamper rejection, staging
restore, and destructive-operation acknowledgement. They cannot prove that a
particular host can recreate its real volumes or that its operator key custody
works. Before declaring a production release recoverable, run the following on
an isolated restore host (never against the serving deployment):

1. Record the backup directory, source revision, manifest fingerprints, key
   custody/change-ticket reference, and the intended restore target. Keep the
   key outside the backup directory and do not put it in shell history.
2. Run `operations.sh verify-backup BACKUP_DIR` with the same key file. Stop if
   any fingerprint, ciphertext checksum, envelope authentication, or file
   permission check fails.
3. Restore into the isolated Compose volumes with
   `SELF_HOST_RESTORE_ACK=I_ACKNOWLEDGE_THIS_OVERWRITES_STATE`, then run
   `make self-host-migration-gate`, `operations.sh status`, and the auth smoke
   against the isolated public origins. Do not route production traffic yet.
4. Check application sentinels: a known PostgreSQL row, Redis queue state,
   MinIO object, Qdrant projection, Typesense projection, and backend syncing
   file must all match the recorded pre-backup evidence. Also exercise one
   authenticated read/write/delete path and confirm no post-backup sentinel
   remains where the restore contract says it must be absent.
5. Save command output, service/image/config identity, manifest SHA, backup id,
   restore duration, and pass/fail disposition in the change record. Destroy
   temporary plaintext extracts and the materialized key according to the
   operator secret-manager policy.

   A successful local unit/contract lane is not a successful production drill;
   retain this external evidence before closing the backup/restore prerequisite.

## Vector projection backfill and cutover

Vectors are rebuildable projections, never authoritative records. Create one
immutable JSONL export per logical namespace from the product store/service that
owns the source documents. Each non-empty line must contain exactly `id`,
`content`, and optional `metadata`; non-memory namespaces do not export vectors
or any `projection_*` metadata. Canonical ns2 exports include the explicit
memory lineage fields required by the canonical search parser:

The repository supplies the read-only authority exporter used for both the
managed Firestore source and the self-host PostgreSQL-backed Firestore facade.
It enumerates `users/{uid}` and reads only the seven source collections; it
never imports Pinecone/Qdrant records and has no write operation. Use explicit
UIDs for a bounded migration, or `--all-users` for a complete source inventory:

```bash
mkdir -m 700 migration/authority
FIRESTORE_PG_DSN="$FIRESTORE_PG_DSN" \
  backend/.venv/bin/python backend/scripts/export_authoritative_vectors.py \
  --source-project SOURCE_PROJECT \
  --source-database '(default)' \
  --source-endpoint https://firestore.googleapis.com \
  --freeze-lease /secure/firestore-migration/source-write-freeze.json \
  --all-users \
  --memory-mode canonical \
  --output-dir migration/authority \
  --allow-empty
```

The default is fail-closed when any selected namespace has no rows. Keep
`--allow-empty` only when the operator has recorded that the namespace is
intentionally unused; the resulting `manifest.json` records the explicit
acknowledgement. The exporter writes `ns1.jsonl`, `ns2.jsonl`,
`workstream_association_v1.jsonl`, `ns_x.jsonl`, `ns3.jsonl`, `ns4.jsonl`, and
`ns_tchunks.jsonl`, plus a SHA-256/count sidecar. The exporter re-verifies the
same mode-0600 source-write freeze lease before every lazy source read and
binds its source authority and lease id into the manifest. Every JSONL line is strict
and has no vector values. The manifest is required by the projection CLI; a
hand-written JSONL file cannot pass its authority preflight. The default canonical ns2 mode requires schema,
revision, source/content hashes, a ledger projection fence, and a timezone-aware
update timestamp; it fails closed rather than creating rows that canonical
memory search would reject. Use `--memory-mode legacy` only for an explicitly
isolated legacy ns2 migration. Transcript rows are decoded at the authority
boundary; encrypted or malformed transcript data fails the export instead of
silently producing an incomplete transcript projection.

Conversation keyword search is likewise a rebuildable projection, but it has a
separate, backend-owned Typesense schema (`omi_conversations`). The self-host
profile sets `CONVERSATION_KEYWORD_INDEX_PROVIDER=typesense`; it never waits for
or invokes the Firebase Typesense extension. If a neutral/self-hosted process
is launched outside Compose and omits that provider, it resolves to typed
`disabled` even when ambient Typesense credentials exist. Create/update/finalization/delete
paths synchronously maintain this projection when selected. A selected but
unreachable Typesense service is surfaced as an unavailable search capability
rather than an empty result set. The canonical memory keyword projection uses
the same explicit-provider rule through `MEMORY_KEYWORD_INDEX_PROVIDER`.

After restore or before cutover, rebuild and then independently reconcile the
authoritative Firestore-shim rows against Typesense count and content hashes:

```bash
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm backend \
  python scripts/rebuild_conversation_typesense.py rebuild

deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm backend \
  python scripts/rebuild_conversation_typesense.py reconcile
```

Both commands print JSON. Reconciliation exits `2` on missing, unexpected, or
content-mismatched documents; E2EE conversations are intentionally absent from
both the expected and actual searchable set.

Self-host Compose also sets `AGENT_VM_PROVIDER=disabled`. The backend applies
the same default whenever `OMI_DEPLOYMENT_PROFILE` is neutral or self-hosted,
so a direct launch or stale container never discovers GCP ADC or calls
`compute.googleapis.com`. Account deletion fails closed when an
imported account still has an `agent_vm` pointer or migration journal; reconcile
retired GCE resources before cutover and rerun the inventory rather than treating
missing GCP credentials as a successful deletion.

Use the checked-in Agent VM reconciliation workflow for that hand-off. It is
deliberately split into a local, no-egress inventory and an operator-owned
managed-GCE observation. The local commands require `AGENT_VM_PROVIDER=disabled`
and either `FIRESTORE_PG_DSN` or `FIRESTORE_EMULATOR_HOST`; they never discover
ADC, import a Compute client, or call a GCE endpoint. The inventory contains
only UID/resource identities, never `authToken`, IP addresses, or arbitrary user
fields, and is mode `0600`:

```bash
export AGENT_VM_PROVIDER=disabled
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm --no-deps \
  --volume /secure/agent-vm:/migration:rw backend \
  python scripts/agent_vm_reconcile.py inventory \
  --output /migration/agent-vm-inventory.json
```

In the managed GCE project, use an explicitly selected project and operator
credential to check every `key` in the private inventory. Do not let the
operator shell fall back to ambient ADC. Save only a typed report of the form
`{"resources":[{"key":"instance:ZONE:NAME:NUMERIC_ID","status":"absent"}]}`
after the read-only `gcloud compute ... describe` checks and any required,
identity-fenced deletes have completed. Back on the self-host operator host,
bind that report to the exact inventory with the reconciliation secret:

```bash
export OMI_AGENT_VM_RECONCILE_SECRET='from-operator-secret-manager'
python3 backend/scripts/agent_vm_reconcile.py sign-proof \
  --inventory /secure/agent-vm/agent-vm-inventory.json \
  --resource-report /secure/agent-vm/gce-absent-report.json \
  --output /secure/agent-vm/gce-absent-proof.json \
  --source-project MANAGED_GCE_PROJECT \
  --operator CHANGE_TICKET
python3 backend/scripts/agent_vm_reconcile.py verify \
  --inventory /secure/agent-vm/agent-vm-inventory.json \
  --proof /secure/agent-vm/gce-absent-proof.json \
  --source-project MANAGED_GCE_PROJECT
```

`sign-proof` only signs the independently collected report; it is not a GCE
observation. `verify` is therefore the required change-record evidence. To
clear the exact stale local pointer/journals after the proof passes, require
both explicit flags; the command re-reads the local authority, aborts on any
identity drift, performs one transaction per UID, and verifies no state remains:

```bash
python3 backend/scripts/agent_vm_reconcile.py reconcile \
  --inventory /secure/agent-vm/agent-vm-inventory.json \
  --proof /secure/agent-vm/gce-absent-proof.json \
  --source-project MANAGED_GCE_PROJECT \
  --apply --confirm-agent-vm-state-clear
```

An ambiguous journal (especially a missing numeric provider ID), an expired or
wrong-project proof, a changed local state, or an inventory/proof mismatch
fails closed. No command in this workflow claims a provider resource was
deleted without the external identity-fenced report.

For non-ns2 namespaces the generic authority shape remains:

```json
{"id":"conversation-01","content":"authoritative text to embed","metadata":{"uid":"user-01"}}
```

Before backfill, back up the deployment and change the production env to an
explicit dual-write state. Retain both versions for privacy deletion and
rollback, run the config check, then recreate the backend:

```dotenv
VECTOR_PROJECTION_MODE=dual_write
VECTOR_PROJECTION_ACTIVE_VERSION=v1
VECTOR_PROJECTION_TARGET_VERSION=v2
VECTOR_PROJECTION_SCHEMA_VERSION=1
VECTOR_PROJECTION_DELETE_VERSIONS=v1,v2
VECTOR_PROJECTION_REQUIRED_NAMESPACES=ns1,ns2,workstream-association-v1,ns_x,ns3,ns4,ns_tchunks
```

Put the immutable export in an operator-controlled local `migration` directory.
The following commands run the same backend image and configured generic
embedding/Qdrant adapters as production. The receipt binds the exact source
bytes and count to the generated vectors, provider, model, dimension, schema,
namespace, target version, authority manifest SHA-256, and source kind:

```bash
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm \
  --volume "$PWD/migration:/migration:rw" backend \
  python scripts/vector_projection_migration.py validate \
  --records /migration/ns2.jsonl \
  --manifest /migration/manifest.json \
  --namespace ns2 --memory-mode canonical

deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm \
  --volume "$PWD/migration:/migration:rw" backend \
  python scripts/vector_projection_migration.py backfill \
  --records /migration/ns2.jsonl \
  --manifest /migration/manifest.json \
  --receipt /migration/ns2-v2.receipt.jsonl \
  --namespace ns2 --target-version v2 --memory-mode canonical

deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm \
  --volume "$PWD/migration:/migration:rw" backend \
  python scripts/vector_projection_migration.py verify \
  --records /migration/ns2.jsonl \
  --manifest /migration/manifest.json \
  --receipt /migration/ns2-v2.receipt.jsonl \
  --report-output /migration/ns2-v2.verify.json
```

Repeat validate, backfill, and verify for every namespace in
`VECTOR_PROJECTION_REQUIRED_NAMESPACES`. An authority-confirmed empty namespace
uses `--allow-empty` on validate/backfill; the acknowledgement is recorded in
the receipt. For non-ns2 namespaces pass `--memory-mode legacy`. Before switch, drain projection writes, take final exports, and
rerun backfill/verify so the target count exactly matches authority—unexpected
target vectors fail verification too.

Create `/migration/switch-plan.json` with all required namespaces. Paths are
resolved relative to the plan file:

```json
{
  "format": "omi-vector-projection-switch-plan-v2",
  "projections": [
    {"namespace":"ns1","records":"ns1.jsonl","receipt":"ns1-v2.receipt.jsonl","manifest":"manifest.json"},
    {"namespace":"ns2","records":"ns2.jsonl","receipt":"ns2-v2.receipt.jsonl","manifest":"manifest.json"},
    {"namespace":"workstream-association-v1","records":"workstream_association_v1.jsonl","receipt":"workstream-v2.receipt.jsonl","manifest":"manifest.json"},
    {"namespace":"ns_x","records":"ns_x.jsonl","receipt":"ns_x-v2.receipt.jsonl","manifest":"manifest.json"},
    {"namespace":"ns3","records":"ns3.jsonl","receipt":"ns3-v2.receipt.jsonl","manifest":"manifest.json"},
    {"namespace":"ns4","records":"ns4.jsonl","receipt":"ns4-v2.receipt.jsonl","manifest":"manifest.json"},
    {"namespace":"ns_tchunks","records":"ns_tchunks.jsonl","receipt":"ns_tchunks-v2.receipt.jsonl","manifest":"manifest.json"}
  ]
}
```

Only the all-namespace plan can emit the global switch overlay:

```bash
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm \
  --volume "$PWD/migration:/migration:rw" backend \
  python scripts/vector_projection_migration.py switch \
  --plan /migration/switch-plan.json \
  --env-output /migration/v2.switch.env
```

`switch` re-runs online verification and emits nothing unless every expected id,
vector, and projection identity matches. Output paths are create-only. Review
the generated overlay, copy its five values into `.env.production`, rerun
`make self-host-config-check`, and recreate the backend. Keep the old version in
`VECTOR_PROJECTION_DELETE_VERSIONS` until its data is purged so account deletion
continues to cover every retained copy.

Rollback generates another reviewed overlay; it does not mutate Compose or the
vector store automatically:

```bash
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm \
  --volume "$PWD/migration:/migration:rw" backend \
  python scripts/vector_projection_migration.py rollback \
  --previous-version v1 --abandoned-version v2 \
  --env-output /migration/memories-v2.rollback.env
```

An edited source export, changed embedding identity/schema, missing or altered
target record, incoherent version state, or existing output path exits nonzero
without a cutover artifact. Preserve the source export, receipt, verify report,
reviewed env overlay, backup id, and command output in the change record.

## Zero-vendor configuration gate

Run:

```bash
make self-host-config-check SELF_HOST_ENV=deploy/self-host/.env.production
```

The static gate verifies required services, pinned images, health checks,
persistent state, required secret/public URL interpolation, HTTPS Auth origin
consistency, and the selected PostgreSQL/Redis/MinIO/Qdrant/generic/SenseVoice
streaming/operator-owned mlx-audio MOSS batch providers. It rejects Firebase,
OpenAI, Pinecone, GCP/Google credentials and
official endpoint defaults. It does not make an availability claim about the
operator-provided generic inference or mlx-audio endpoints. Typesense is an
explicit pinned service for canonical memory and conversation keyword
projections; cutover acceptance requires real schema validation,
upsert/query/update/delete and count/hash reconciliation behavior in addition
to the PostgreSQL/Qdrant vector path.

The executable hermetic contract lane installs a hard DNS/socket denial in the
FastAPI E2E process and runs the selected Capture → Understand → Remember →
Retrieve → Act plus account-deletion tests. That E2E harness deliberately uses
fake Firebase/Firestore/storage/provider boundaries, so it proves application
contracts and egress denial only; it is not replacement-service evidence:

```bash
make self-host-zero-vendor-acceptance
```

For a configured production host, `--live` first starts/waits for this Compose
profile, requires backend/auth/worker and all state services healthy, and runs
one disposable account through the live Better Auth signup/session/JWT/JWKS
backend verifier, PostgreSQL document/subcollection/top-level rows, Redis
account-deletion queue, MinIO object prefixes, Qdrant projection, and the real
backend account-deletion worker. It requires user-owned PostgreSQL rows, queue
task state, objects, vectors, and Better Auth user/session/account rows to
reconcile to zero. It also imports the locked `sherpa-onnx` runtime and requires
the mounted SenseVoice `model.int8.onnx` plus `tokens.txt`, creates the real
recognizer, and executes a 120-second-bounded decode of a short synthetic PCM
window (an empty transcript is acceptable, but recognizer creation, waveform
acceptance, decode, and result access must all succeed). The UID-keyed active
deletion marker must also be absent. The only retained control record is an
HMAC-keyed minimal completion receipt containing an opaque job id, status,
schema version, and completion timestamp; the gate rejects any UID, deletion
reason, or reason details in it. This receipt keeps already-issued tokens fenced
and queue redelivery idempotent without retaining a direct UID or feedback. It
is a keyed pseudonymous control record—not a claim of anonymity or zero control
rows—and the disposable fixture removes it after collecting evidence. Only after that
replacement-service proof does the gate run the separately-labelled hermetic
contracts:

```bash
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
SELF_HOST_ACCEPTANCE_EVIDENCE=/secure/change-record/zero-vendor.json \
  deploy/self-host/zero-vendor-acceptance.sh --live
```

The default evidence path is the host temporary directory so a local run cannot
dirty the repository. Before starting any cutover-live services, the runner
requires a clean Git worktree and records the full `git_commit`, exact
`git_tree`, and `worktree_clean=true`. This binds the result to reviewable
source: evidence from an uncommitted tree cannot authorize either a tested
configuration or production cutover, and the gate must be rerun after the
tested changes are committed. Evidence keeps three lanes distinct:

- the default hermetic contract denies DNS and sockets but uses fake provider
  boundaries;
- `--live` proves the selected replacement services, real SenseVoice PCM
  decode, mlx-audio configuration presence, generic model/embedding adapters,
  and account-deletion reconciliation, but it is not an assembled product loop
  and does not claim a diarization provider call;
- `--cutover-live` additionally creates a one-day `.omi.test` CA and HTTPS
  proxy, then drives one disposable principal through public product routes.

The local cutover lane uses `https://api.omi.test`,
`https://auth.omi.test`, `https://mcp.omi.test`, and
`https://objects.omi.test` with no port. Inside the
Compose network those names resolve to the proxy's real TLS listener on 443;
`CUTOVER_HTTPS_PORT` only publishes an optional loopback diagnostic port on the
host. The lane proves public Better Auth signup/token/JWKS, exact JWT
issuer/audience, private JWKS backend verification, MCP metadata, public signed
object PUT/GET/DELETE with payload and authoritative-absence checks, and public
WSS Capture with the checked-in LibriSpeech fixture. Separately, it asks the
operator mlx-audio service for `/v1/models`, requires the configured model id,
and runs the production pre-recorded adapter against the real two-speaker WAV
mounted from `MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH`. The hard evidence
requires at least two speakers and at least two transitions. Audio duration is
computed from WAV frames/sample rate; mlx-audio `total_time` is processing time
and is never treated as media duration. It then uses authenticated
`/v2/messages` turns to understand that transcript and invoke
`web_search_tool`; the latter must emit the public SSE tool event and return a
Wikipedia source from SearXNG. Remember uses public `/v3/memories`, the normal
scheduled canonical-maintenance function processes and projects the record,
Retrieve uses public vector search, and Act writes/reads an action item with
canonical conversation and memory provenance. The maintenance tick is labelled
internal scheduler evidence, not a public product route or adapter probe. The
interactive loop can still report `status=passed` when a processed Short-term
memory is durably projected and retrieved while Long-term consolidation remains
retryable; that state is recorded as `remember.long_term_admission=retry_pending`
and can never authorize production cutover. Production authorization requires
that field to be `passed`.

```bash
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
SELF_HOST_ACCEPTANCE_EVIDENCE=/secure/change-record/zero-vendor-local.json \
  deploy/self-host/zero-vendor-acceptance.sh --cutover-live
```

The backend's shared `httpx` pools also enforce an application-level authority
boundary before DNS or transport: `SELF_HOST_EGRESS_ALLOWLIST` is a
comma-separated list of operator-owned external host names (an explicit
`*.example` suffix is supported), while reviewed Compose service authorities
such as `auth-server`, `searxng`, and `host.docker.internal` remain internal.
Neutral/self-hosted requests to official Omi/model/telemetry hosts, or to an
undeclared external host, raise the typed
`deployment_capability_unavailable/endpoint_not_allowlisted` boundary before
the request is sent. The checked-in example includes the generic LLM,
realtime, and object authorities; operators must add any optional push, TTS,
icon, maps, webhook, or user-fetch authority they intentionally enable.
This guard covers the backend shared HTTP clients only and is not a network
firewall or a claim of universal socket isolation.

Local Compose does not impose a network-level application egress policy, so its
evidence says `live_sentinel_egress_policy.enforcement=not_enforced_by_compose`
and never claims live DNS denial. It can authorize only the exact tested
configuration, and only when the assembled loop, replacement-service smoke,
signed object CRUD, a real local speaker embedding, Long-term admission, clean
source attribution, a real mlx-audio MOSS transcription with exact model-catalog
match and multi-speaker transitions, SHA-256 identity for every required mounted
SenseVoice/speaker/TTS model and token artifact, exact running image/config
identity, and a final all-service health snapshot all pass; it
cannot authorize production traffic. On the intended host, first isolate
backend, queue-worker, and auth-server from arbitrary public sockets while
allowing SearXNG's reviewed Wikipedia egress and the explicitly selected
model/realtime authorities (including an explicitly selected public HTTPS
provider when used). Save the applied network policy manifest or
firewall export as the non-secret `SELF_HOST_EGRESS_POLICY_ARTIFACT`, then run
the same public edge lane:

```bash
SELF_HOST_ENV=/etc/omi/self-host.production.env \
SELF_HOST_EGRESS_POLICY_ARTIFACT=/secure/change-record/application-egress-policy.json \
SELF_HOST_RECOVERY_EVIDENCE=/secure/change-record/isolated-restore-evidence.json \
SELF_HOST_ACCEPTANCE_EVIDENCE=/secure/change-record/zero-vendor-production.json \
  deploy/self-host/zero-vendor-acceptance.sh --external-cutover-live
```

External mode rejects reserved/local public origins, requires every public
origin to be HTTPS, uses system certificate trust, validates the supplied policy
artifact against the checked-in JSON contract, hashes its original bytes into
the evidence, and requires
socket attempts to OpenAI, Google, Anthropic, Omi, and an arbitrary public-IP
sentinel to fail from backend, queue-worker, and auth-server. The JSON calls
these `sentinel_targets_denied` and explicitly limits the claim to those
targets; it does not claim DNS denial or universal Internet isolation. Only an
assembled-loop pass through the intended public certificate/edge plus the
operator policy artifact and all sentinel denials sets
`authorizes_production_cutover=true`. Authorization additionally requires all
four public origins, the signed object CRUD proof, exact running image/config
identity, the hard diarization provider proof, and the final service-health snapshot. Every cutover live mode also loads
SearXNG's effective in-container settings, compares its secret hash to the
reviewed env-file value in constant time, and records only the resulting
booleans, rejecting an empty, known-default, or mismatched runtime secret. The
local Sherpa speaker embedding is exercised, while speaker enrollment/matching
functional equivalence remains explicitly false. mlx-audio model revision and
offline-cache provenance remain explicitly operator-owned and unattested by the
service response. The typed realtime relay is exercised through its authenticated
public relay route as a separate hard capability.

Production authorization also requires an operator-supplied recovery-drill
evidence file. Set `SELF_HOST_RECOVERY_EVIDENCE` to a non-symlink absolute JSON
file recorded from an isolated restore host. Its schema-v1 record must bind the
backup manifest SHA, source commit/tree, and runtime config SHA to the tested
stack; report backup verification, restore, post-restore migration, Auth smoke,
and projection checks as passed; and explicitly attest that key material stayed
outside the backup and that production KMS/secret-manager custody was used.
The JSON field is named `production_kms_attested`.
The gate accepts this as an operator attestation only—it is not a cryptographic
signature—but missing, partial, local-only, or mismatched recovery evidence can
never authorize production traffic. A local `--cutover-live` run therefore
remains a tested-configuration proof, not production authorization.

The policy artifact must be UTF-8 JSON with exactly this shape (the artifact is
review evidence, not a cryptographic signature):

```json
{
  "schema_version": 2,
  "enforcement": "network_default_deny",
  "workloads": ["auth-server", "backend", "queue-worker"],
  "denied_targets": [
    "1.1.1.1",
    "api.openai.com",
    "api.omi.me",
    "api.anthropic.com",
    "generativelanguage.googleapis.com"
  ],
  "source_git_commit": "<40-hex-tested-commit>",
  "source_git_tree": "<40-hex-tested-tree>",
  "runtime_config_sha256": "<64-hex-effective-config>"
}
```

The contract prevents an arbitrary non-empty file from being treated as a
reviewed policy and binds it to the exact source/config identity that started
the tested workloads. It does not prove that the host firewall applied the
policy; the per-workload socket probes are the behavioral corroboration, and
the artifact's original SHA-256 plus change record remain operator evidence.

## Firestore-to-PostgreSQL cutover gate

### Source-write freeze lease

The final Firestore and object-storage reconciliations read the source more
than once. Before starting either migration, the operator must pause source
writes through the source system's change-control mechanism and issue one
short-lived, HMAC-signed lease covering both migration scopes. The lease is a
mode-0600 JSON artifact; its signing secret is supplied only through
`OMI_SOURCE_WRITE_FREEZE_SECRET` and is never stored in the artifact. Issuing a
lease does not pause writes by itself.

```bash
# Set this from the operator secret manager; do not commit it or put it in the
# reviewed environment file.
export OMI_SOURCE_WRITE_FREEZE_SECRET='operator-secret-from-secret-manager'
python3 backend/scripts/source_write_freeze.py issue \
  --output /secure/firestore-migration/source-write-freeze.json \
  --source-project SOURCE_PROJECT \
  --source-database '(default)' \
  --source-endpoint https://firestore.googleapis.com \
  --scope firestore --scope storage \
  --holder CHANGE_TICKET \
  --ttl-seconds 3600
python3 backend/scripts/source_write_freeze.py verify \
  /secure/firestore-migration/source-write-freeze.json \
  --source-project SOURCE_PROJECT \
  --source-database '(default)' \
  --source-endpoint https://firestore.googleapis.com \
  --scope firestore --scope storage
```

The Firestore import and storage apply/verify commands require this lease and
verify its signature, exact source authority, scopes, permissions, and expiry
at invocation time. Firestore capture re-checks the lease before every source
iterator advance and every resumed target write, while storage apply/verify
re-check the lease before every source-object read and immediately before
recording passing cutover evidence. A lease expiry removes an incomplete
Firestore capture manifest and fails closed, so a long-running migration
cannot continue or authorize traffic after the bounded freeze expires.
External cutover acceptance (`--external` or
`--external-cutover-live`) also requires the same lease through
`SELF_HOST_SOURCE_WRITE_FREEZE_LEASE`, `SELF_HOST_SOURCE_PROJECT`,
`SELF_HOST_SOURCE_DATABASE`, and `SELF_HOST_SOURCE_ENDPOINT`; missing or
expired evidence fails closed.

The gate is a pre-cutover proof, not a traffic switch. By default it starts the
existing dev PostgreSQL + Firestore emulator definitions under an isolated
Compose project and new volume, runs all live `firestore_pg` transaction/index
integration tests, applies the Firestore PG migration twice and checks its
ledger, captures a real emulator fixture containing a nested collection below
a missing parent, resumes it through the executable importer, and requires
live-source/manifest/target count plus content-hash reconciliation. It also
runs the Better Auth migration twice plus a no-drift check,
runs a real Better Auth sign-up/sign-in/session-token/JWT/JWKS/backend-verifier
chain, seeds a legacy Ed25519 JWKS row, proves that it breaks ES256 signing,
migrates it into the verification grace window, proves both the legacy and new
tokens verify, proves user/session/account cascade deletion reaches zero, runs
the same scenario corpus against the emulator and shim,
and requires a byte-normalized shadow diff. It then writes a JSON evidence file
whose `authorizes_traffic_change` field is deliberately `false`.

```bash
make self-host-migration-gate
# or choose an evidence path
SELF_HOST_GATE_EVIDENCE=/secure/change-record/omi-firestore-pg.json \
  deploy/self-host/migration-cutover-gate.sh
```

The managed lane never uses the production DSN. `--external` exists for a
separately provisioned disposable verification database/emulator and also
requires `AUTH_MIGRATION_DATABASE_URL`, a PostgreSQL URL reachable from the
migration container (`host.docker.internal` for a host database). Both lanes
run the explicit idempotency/drift proof. A remote database is refused unless
the operator additionally sets
`ALLOW_REMOTE_MIGRATION_TEST_TARGET=I_ACKNOWLEDGE_THIS_IS_DISPOSABLE`; the tests
write and delete fixed regression namespaces, so production is never an
acceptable target.

The production import uses the same image and explicit owner. Quiesce Firestore
writes for the final reconciliation and mount an encrypted operator directory;
the checkpoint and adjacent mode-0600 JSONL contain customer data and must be
retained together for resume:

```bash
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm \
  --volume /secure/firestore-migration:/migration:rw \
  firestore-pg-migrate python scripts/firestore_pg_migrate.py import \
  --source-project SOURCE_PROJECT \
  --source-database '(default)' \
  --source-endpoint https://firestore.googleapis.com \
  --source-credentials /migration/firestore-reader.json \
  --freeze-lease /migration/source-write-freeze.json \
  --checkpoint /migration/firestore-import.json
```

An import without its checkpoint refuses a non-empty target. Unsupported value
types or collection IDs, an edited/missing manifest, source drift, or any count
or content-hash mismatch exits nonzero and does not authorize cutover. Only
after this gate and the production import are green should the operator bind
the evidence to backup IDs and move traffic. Rollback means restoring the
pre-cutover traffic route and retained source-of-truth backup; this script does
neither operation automatically.

## GCS/Firebase Storage-to-MinIO cutover

Firestore documents do not contain the object bytes they reference. Migrate
every production GCS/Firebase Storage bucket before traffic changes, while the
same source-write freeze used for the final Firestore reconciliation remains in
force. The storage importer reads each source object at an exact GCS generation
and writes a mode-0600 inventory containing bucket/name, generation, byte size,
SHA-256, custom metadata, content type, and the deterministic target mapping.
The dry run therefore reads all source bytes once; apply streams them to MinIO,
and verify performs a fresh source scan plus an independent target read.

Create a mode-0600 `/secure/storage-migration/plan.json`. Prefixes are explicit
tenant boundaries: they are either empty for a whole bucket or end in `/`, and
the relative path below the source prefix is preserved below the target prefix.
Overlapping source or target scopes, `.`/`..`, repeated separators, control
characters, unsafe metadata, and target-key collisions are rejected.

```json
{
  "schema_version": 1,
  "scopes": [
    {
      "id": "speech-profiles",
      "source_bucket": "omi-production-speech-profiles",
      "source_prefix": "",
      "target_bucket": "omi-speech-profiles",
      "target_prefix": ""
    },
    {
      "id": "one-tenant-chat-files",
      "source_bucket": "omi-production-chat-files",
      "source_prefix": "TENANT_UID/",
      "target_bucket": "omi-chat-files",
      "target_prefix": "TENANT_UID/"
    }
  ]
}
```

Include every configured self-host object bucket as either a whole-bucket scope
or a set of non-overlapping tenant scopes: speech profiles, postprocessing,
memory recordings, private/temporal sync, plugin logos, app thumbnails, chat
files, and desktop updates. Source and target bucket names may differ; object
paths are never inferred from environment-variable names.

Mount an encrypted operator directory containing the plan and a read-only GCS
service account scoped to only the declared source buckets. First run the
source-only dry run to create the immutable inventory; this command does not
connect to or mutate MinIO:

```bash
chmod 600 /secure/storage-migration/plan.json \
  /secure/storage-migration/gcs-reader.json
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm --no-deps \
  --volume /secure/storage-migration:/migration:rw \
  backend python scripts/storage_gcs_minio_migration.py dry-run \
  --plan /migration/plan.json \
  --manifest /migration/gcs-inventory.jsonl \
  --source-project SOURCE_PROJECT \
  --source-credentials /migration/gcs-reader.json
```

Then start MinIO and apply the resumable streaming copy. The policy must be an
explicit operator choice and must remain identical for apply and verify:

- `create-only` requires empty target scopes on the first run and never
  overwrites an object. It is the preferred policy for a fresh MinIO volume.
- `same-hash` adopts or resumes only an object whose size, content type,
  metadata, migration receipt, and independently re-read bytes exactly match
  the inventory. It never treats an ETag as a content proof.

```bash
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml up --detach --wait minio
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm --no-deps \
  --volume /secure/storage-migration:/migration:rw \
  backend python scripts/storage_gcs_minio_migration.py apply \
  --plan /migration/plan.json \
  --manifest /migration/gcs-inventory.jsonl \
  --checkpoint /migration/gcs-minio-checkpoint.json \
  --source-project SOURCE_PROJECT \
  --source-credentials /migration/gcs-reader.json \
  --freeze-lease /migration/source-write-freeze.json \
  --target-endpoint http://minio:9000 \
  --existing-policy create-only
```

The checkpoint binds the exact plan bytes, manifest bytes, GCS project and
endpoint, MinIO endpoint, and existing-object policy. Resume with the identical
apply command until it reports `status=applied`. While the source-write freeze
is still active, run the independent cutover gate:

```bash
deploy/self-host/compose-clean-env.sh deploy/self-host/.env.production \
  deploy/self-host/compose.production.yml run --rm --no-deps \
  --volume /secure/storage-migration:/migration:rw \
  backend python scripts/storage_gcs_minio_migration.py verify \
  --plan /migration/plan.json \
  --manifest /migration/gcs-inventory.jsonl \
  --checkpoint /migration/gcs-minio-checkpoint.json \
  --source-project SOURCE_PROJECT \
  --source-credentials /migration/gcs-reader.json \
  --freeze-lease /migration/source-write-freeze.json \
  --target-endpoint http://minio:9000 \
  --existing-policy create-only
```

Only verify can emit `status=passed`. It requires the complete apply checkpoint
and authorizes cutover only when the immutable manifest, a fresh
generation-pinned GCS scan, and an independent MinIO list/head/byte scan have
the same count and order-independent content hash. Extra target objects, source
generation drift, metadata/content-type loss, corrupt bytes, an incomplete or
changed checkpoint, and authority changes fail closed. CLI output contains only
counts and hashes; object names and metadata remain in the private inventory.

Preserve the plan, inventory, checkpoint, source credential audit record,
command output, and the pre-cutover `operations.sh backup` ID in the change
record. The importer never deletes source objects or changes traffic. Rollback
uses the retained GCS route plus the pre-cutover MinIO backup; do not delete the
source buckets until the rollback window and restore drill are complete.
