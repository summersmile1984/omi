# Self-host production profile

This is the production entry point for a deployment that keeps identity, data,
queues, object storage, vectors, LLM routing, embeddings, and pre-recorded STT
behind operator-owned boundaries. It is separate from `dev/docker-compose.dev.yml`:
the dev file remains the emulator harness and is reused by the migration gate.

## What runs

`compose.production.yml` runs the backend, Better Auth server, PostgreSQL,
password-protected Redis plus its durable queue worker, MinIO, and Qdrant. Every
service has a health check. PostgreSQL, Redis, MinIO, Qdrant, and backend sync
staging use named persistent volumes. The SenseVoice model directory is an
explicit read-only host mount. Remote base/state images are pinned by immutable
multi-architecture digest as well as a human-readable version tag.

`auth-migrate` is an explicit one-shot schema owner. It runs Better Auth's
Kysely migration plan, re-reads the schema, and fails unless the plan converges
to zero pending tables/columns. `auth-server` is admitted only after that
container exits successfully. This avoids relying on application startup to
silently create or update identity tables.

The profile deliberately does not ship a default inference vendor. Set
`GENERIC_OPENAI_BASE_URL` to an operator-selected OpenAI-compatible endpoint and
set its explicit model/key. Embeddings use that same generic provider boundary.
Both incremental live STT and pre-recorded STT are pinned to the mounted
SenseVoice model. The live adapter decodes bounded five-second PCM windows (and
VAD utterance boundaries) in the sync executor, so it emits before a recording
ends without blocking the WebSocket loop. `model.int8.onnx`, `tokens.txt`, and
the locked `sherpa-onnx` runtime are all required before a session is admitted.
`STT_ROUTE_FALLBACK_TO_DEFAULT=false` prevents a missing local model from
falling through to any managed STT policy default.
Realtime multimodal model sessions are separately disabled with
`REALTIME_PROVIDER=disabled`; requests
return a typed unavailable response before any OpenAI/Gemini credential or
official endpoint is considered. Optional integrations require separately
configured services; the core profile does not silently reach an official
endpoint for them.

Web search is explicitly disabled in the core profile. The agent's search tool
returns a typed unavailable result and never falls back to Omi's LLM gateway or
Perplexity. A deployment that needs live search must add and verify an explicit
search-provider capability instead of relying on a hidden default.

The checked-in example sets `BACKEND_PLATFORM=linux/amd64` because the pinned
runtime lock includes `onnxruntime==1.19.0`, which has no Linux ARM64 wheel.
Apple Silicon Docker can build/run it through amd64 emulation; native ARM64
production requires a separately reviewed dependency-lock upgrade rather than
silently resolving a different package set.

## TLS and public origins are prerequisites

Put an HTTPS reverse proxy/load balancer in front of the three bound ports:

- `PUBLIC_BACKEND_URL` -> `${SELF_HOST_BIND_ADDRESS}:${BACKEND_PORT}`
- `PUBLIC_AUTH_URL` -> `${SELF_HOST_BIND_ADDRESS}:${AUTH_SERVER_PORT}`
- `PUBLIC_OBJECTS_URL` -> `${SELF_HOST_BIND_ADDRESS}:${MINIO_API_PORT}`

All three URLs must be explicit HTTPS URLs. Better Auth tokens keep
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
docker compose \
  --env-file deploy/self-host/.env.production \
  --file deploy/self-host/compose.production.yml \
  config --quiet
docker compose \
  --env-file deploy/self-host/.env.production \
  --file deploy/self-host/compose.production.yml \
  up --detach --build --wait
```

Do not use the checked-in example as a runtime secret file. Back up the four
state-service volumes and the backend syncing volume before upgrades. Preserve
`ENCRYPTION_SECRET`, Better Auth secrets, and queue worker secrets across
restarts; changing them without a planned rotation breaks stored data or live
sessions/jobs. Completed-deletion access barriers are addressed by an HMAC of
the deleted UID under `ENCRYPTION_SECRET`, so losing or rotating that secret
without migrating those opaque receipt keys would also reopen old tokens; the
backend therefore fails closed when the secret is missing or too short.

Runtime validation rejects every `REPLACE_*` value, reserved `example.com`
public origins, and a SenseVoice host directory missing `model.int8.onnx` or
`tokens.txt`. `operations.sh` refuses the checked-in example file outright and
reruns this validation before every state or health operation.

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
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
  deploy/self-host/operations.sh metrics > /var/lib/node_exporter/textfile_collector/omi-self-host.prom
```

Every managed `operations.sh start`, including the post-restore start, stops
application callers and runs a fresh disposable `auth-migrate` container before
Auth, backend, or worker admission. An old successful one-shot container is
never treated as migration evidence for a newly restored database.

Backups quiesce backend/auth/worker traffic, create a PostgreSQL custom-format
logical dump, issue a synchronous Redis save, and archive the stopped
Redis/MinIO/Qdrant/backend-syncing volumes. A SHA-256 manifest binds every
artifact to the source Git revision. Store the runtime env/secrets separately:
they are deliberately never copied into a backup directory.

```bash
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
  deploy/self-host/operations.sh backup /srv/backups/omi/2026-08-20T120000Z
deploy/self-host/operations.sh verify-backup /srv/backups/omi/2026-08-20T120000Z
```

Restore overwrites live state and is therefore fail-closed behind an explicit
acknowledgement. PostgreSQL is dropped and recreated before `pg_restore`, so
objects created after the backup cannot survive merely because they were absent
from the archive. Drain the three public routes at the reverse proxy first,
keep the previous images/env file, then run:

```bash
SELF_HOST_ENV=$PWD/deploy/self-host/.env.production \
SELF_HOST_RESTORE_ACK=I_ACKNOWLEDGE_THIS_OVERWRITES_STATE \
  deploy/self-host/operations.sh restore /srv/backups/omi/2026-08-20T120000Z
make self-host-migration-gate
```

For rollback, first move traffic back to the retained release. Verify the
pre-upgrade backup with `operations.sh rollback-plan BACKUP_DIR`; restore it
only if the old release cannot read the upgraded state. After restore, the
one-shot Better Auth migrator runs before Auth admission, and all services must
pass `status`, the auth smoke, and the migration gate before traffic returns.

## Vector projection backfill and cutover

Vectors are rebuildable projections, never authoritative records. Create one
immutable JSONL export per logical namespace from the product store/service that
owns the source documents. Each non-empty line must contain exactly `id`,
`content`, and optional `metadata`; do not export vectors or any
`projection_*` metadata:

```json
{"id":"memory-01","content":"authoritative text to embed","metadata":{"uid":"user-01"}}
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
namespace, and target version:

```bash
docker compose --env-file deploy/self-host/.env.production \
  --file deploy/self-host/compose.production.yml run --rm \
  --volume "$PWD/migration:/migration:rw" backend \
  python scripts/vector_projection_migration.py validate \
  --records /migration/ns2.jsonl

docker compose --env-file deploy/self-host/.env.production \
  --file deploy/self-host/compose.production.yml run --rm \
  --volume "$PWD/migration:/migration:rw" backend \
  python scripts/vector_projection_migration.py backfill \
  --records /migration/ns2.jsonl \
  --receipt /migration/ns2-v2.receipt.jsonl \
  --namespace ns2 --target-version v2

docker compose --env-file deploy/self-host/.env.production \
  --file deploy/self-host/compose.production.yml run --rm \
  --volume "$PWD/migration:/migration:rw" backend \
  python scripts/vector_projection_migration.py verify \
  --records /migration/ns2.jsonl \
  --receipt /migration/ns2-v2.receipt.jsonl \
  --report-output /migration/ns2-v2.verify.json
```

Repeat validate, backfill, and verify for every namespace in
`VECTOR_PROJECTION_REQUIRED_NAMESPACES`. An authority-confirmed empty namespace
uses `--allow-empty` on validate/backfill; the acknowledgement is recorded in
the receipt. Before switch, drain projection writes, take final exports, and
rerun backfill/verify so the target count exactly matches authority—unexpected
target vectors fail verification too.

Create `/migration/switch-plan.json` with all required namespaces. Paths are
resolved relative to the plan file:

```json
{
  "format": "omi-vector-projection-switch-plan-v1",
  "projections": [
    {"namespace":"ns1","records":"ns1.jsonl","receipt":"ns1-v2.receipt.jsonl"},
    {"namespace":"ns2","records":"ns2.jsonl","receipt":"ns2-v2.receipt.jsonl"},
    {"namespace":"workstream-association-v1","records":"workstream.jsonl","receipt":"workstream-v2.receipt.jsonl"},
    {"namespace":"ns_x","records":"ns_x.jsonl","receipt":"ns_x-v2.receipt.jsonl"},
    {"namespace":"ns3","records":"ns3.jsonl","receipt":"ns3-v2.receipt.jsonl"},
    {"namespace":"ns4","records":"ns4.jsonl","receipt":"ns4-v2.receipt.jsonl"},
    {"namespace":"ns_tchunks","records":"ns_tchunks.jsonl","receipt":"ns_tchunks-v2.receipt.jsonl"}
  ]
}
```

Only the all-namespace plan can emit the global switch overlay:

```bash
docker compose --env-file deploy/self-host/.env.production \
  --file deploy/self-host/compose.production.yml run --rm \
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
docker compose --env-file deploy/self-host/.env.production \
  --file deploy/self-host/compose.production.yml run --rm \
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
providers. It rejects Firebase, OpenAI, Pinecone, GCP/Google credentials and
official endpoint defaults. It does not make an availability claim about the
operator-provided generic inference endpoint. The profile explicitly disables
the unbundled Typesense keyword projection; canonical memory retrieval remains
on PostgreSQL/Qdrant, and account deletion treats that explicit absence as a
verified zero rather than attempting an undeclared Typesense connection.

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
dirty the repository. The evidence deliberately does not authorize a
production traffic switch. The live replacement smoke proves identity,
PostgreSQL, Redis, MinIO, Qdrant, account deletion, and an actual SenseVoice PCM
decode. It also invokes the production generic chat and embedding adapters,
requiring a bounded chat marker plus a nonzero vector of the configured
dimension. The full Capture → Understand → Remember → Retrieve → Act assertions
remain a separately-labelled hermetic contract using fake provider boundaries;
these service/adaptor probes do not send one captured artifact through that
whole live product path. Keep the epic's full assembled-loop gate open until
that assembled path and the reverse-proxy change record are exercised on the
intended release host.

## Firestore-to-PostgreSQL cutover gate

The gate is a pre-cutover proof, not a traffic switch. By default it starts the
existing dev PostgreSQL + Firestore emulator definitions under an isolated
Compose project and new volume, runs all live `firestore_pg` transaction/index
integration tests, runs the Better Auth migration twice plus a no-drift check,
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

Only after this gate is green should the operator execute their independently
reviewed export/import, reconcile application-level document counts and backup
IDs, deploy with `FIRESTORE_PG_DSN`, and move traffic. Rollback means restoring
the pre-cutover traffic route and retained source-of-truth backup; this script
does neither operation automatically.
