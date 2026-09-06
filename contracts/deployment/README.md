# Shared deployment product contracts

`core.py` executes the same HTTP cases against either real target: two public
signups, opaque session restore and JWT exchange, protected admission,
onboarding persistence, calendar capture-gap query/auth/disconnected admission, public invalid-email-link
neutral HTML, CSAT configuration/validation/create-only ratings,
manual and batch memory intake into Short-term despite caller durability hints,
content/visibility/review/read/baseline persistence,
cross-account mutation denial and deletion,
task creation/completion, cross-account denial, branded referral capture,
fresh-account trial grant, self/referral retry rejection and subscription reread, refresh
and logout revocation. It imports no backend handlers and seeds no business
records. The task request contract comes from the actual FastAPI
`ActionItemCreateRequest`: an empty description returns **422**. Cloudflare's
observed 400 is a failing divergence, not an accepted alternative.

Run an already-owned disposable target with its runner-generated metadata:

```bash
python3 contracts/deployment/core.py --metadata /absolute/fixture/metadata.json
```

Metadata contains `api_origin`, `auth_origin`, `target` (`self_hosted` or
`cloudflare`), `brand_id`, and `trace_dir`. Origins must be explicit loopback
HTTP origins. The client follows no redirects, retains no cookie jar, writes
only route/status/timing traces, and never writes credentials or response
bodies. Every run needs a fresh trace directory; it reports each case and exits
nonzero on failure. Target runners own the disposable accounts and state.

The Server runner is `deploy/self-host/ci/product.py`; its local/CI command is
`bash deploy/self-host/ci/product.sh`. It builds the unchanged upstream Dockerfile
and the actual Auth image, uses the same Compose service declarations and
forward-only migration commands, and starts actual Auth/PG/Redis/MinIO/Qdrant,
API and four queue consumers. Application containers use an internal-only
Docker network; a fixed two-port HTTP proxy exposes Auth/API on loopback. A
fresh random Compose project owns all state and removes its own containers,
volumes and networks on exit. No production container is selected or reused.

The core slice explicitly generates a **speech-disabled test profile** from the
real profile renderer and validates it through the normal capability owner.
An isolated HTTP embedding fixture returns deterministic vectors through the
real model-identity/dimension adapter. This is controlled inference for product
state tests; it is not model quality, speech, external egress, or production
configuration evidence. LLM routes on this fixture remain unavailable.

For real speech and text-model verification, supply **all three** existing,
absolute model-store directories. The runner preserves the complete rendered
Server profile, mounts stores read-only, builds the normal `Dockerfile.llm`,
runs both production Ollama artifact checks, and starts actual BGE-M3/Qwen
services. Backend admission verifies the speech bundle and executes real
Kokoro/SenseVoice and Qwen readiness. This mode does not start the controlled
embedding provider. Incomplete store selections are rejected.

```bash
python3 deploy/self-host/ci/product.py \
  --output /absolute/new-server-fixture --brand-id eddy \
  --embedding-store /absolute/bge-m3-store \
  --llm-store /absolute/qwen-store \
  --speech-store /absolute/speech-store
```

The same local/CI shell entry accepts `SELF_HOST_CI_EMBEDDING_STORE`,
`SELF_HOST_CI_LLM_STORE` and `SELF_HOST_CI_SPEECH_STORE` together. Provision the
models with the normal Server commands beforehand; startup/tests never download
them. Before building or starting application containers, real-model mode
checks Docker's memory allocation against the two Compose model limits plus
4 GiB application/engine headroom (currently 12 GiB required).
`model-capacity.json` records the decision. This rejects the actual 2026-09-05
8 GiB VM that killed `llama-server` during finalization; it is an admission
floor, not a throughput or concurrent-workload guarantee. Existing core mode
keeps its original requirements.

Both modes expose actual WebSocket Upgrade through the same Auth/API proxy.
The upstream server owns authentication and the handshake response. The tunnel
preserves parser-buffered first frames and binary PCM in both directions, and
closes both sockets on disconnect, 30-second inactivity, its 15-minute deadline
or its 64 MiB per-direction bound. It never synthesizes transcripts. Real socket
tests run in the existing `product.sh` lane alongside HTTP-framing and process
ownership tests.

`--self-test` executes thirteen common core cases, including when
real models are enabled. Enabling this runtime is not evidence that recording,
finalization, canonical-memory retrieval or the complete CI-1 qualifier passed.
See the [real-model execution record](../../dev/unified-main/implementation-2026-09-05/server-real-model-product.md).

For local iteration, `SELF_HOST_CI_RUNTIME_IMAGE` may name a previously built
standard runtime image. The runner hashes every tracked backend Python source
inside that actual image and refuses missing or different bytes before serving.
It still builds the fresh Auth/profile layers. Omit the variable for a standard
build. `SELF_HOST_CI_PORT` changes the loopback port group (default 34800).
Private command logs, profile hash, source hashes and `core-results.json` remain
under the printed trace directory. They contain only disposable fixture state.

The CSAT cases execute the existing upstream `/v1/csat/config` and
`/v1/csat/ratings` wire contract on both targets: missing state defaults, reserved
platform input, 422/400 validation, 201 creation, 409 resubmission and account
isolation. Cloudflare also checks rating export and actual account deletion in
its recording/privacy suite. These are synthetic ratings in disposable accounts.

This is the identity/onboarding/calendar/email/CSAT/tasks slice of **CI-1**. It deliberately does
not implement `qualify-dual-target.mjs`, which owns the complete candidate and
platform/brand product qualification. Recording finalization, conversation and
memory retrieval, process restart, full error shapes, client UI, supported OSes
and actual releases require their corresponding evidence. A core result always
sets `release_qualified` to false.
