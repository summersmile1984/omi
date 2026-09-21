# Shared deployment product contracts

`readiness.json` owns the two targets' readiness paths and response requirements.
The Server image boot/public deploy owner and Cloudflare frozen local/private
cloud/public deploy owners consume it; the public ingress simulation uses those
same paths. API/Auth readiness requires HTTP 200 and `status: ready` JSON. The
Server Web SSR login intentionally uses status-only verification. Unit tests run
in the existing fork CI control and Cloudflare contract lanes, including when
only this JSON changes.

`core.py` executes the same HTTP cases against either real target: two public
signups, opaque session restore and JWT exchange, protected admission,
onboarding persistence, authenticated JIT tri-state rollout decisions, calendar capture-gap query/auth/disconnected admission, public invalid-email-link
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
HTTP origins by default. CD passes `--remote` for explicit HTTPS deployment
origins and deletes only the accounts created by that invocation, using a fresh
sign-in after logout tests. Cleanup failure fails the report. The shared contract
still marks its slice `release_qualified: false`; the fixed qualification runner
combines it with candidate/version observations and the other product evidence.
An isolated boot test may supply `auth_public_origin` to send the real HTTPS
Origin header while connecting to a temporary loopback port. This preserves
beta/production Auth guards without adding localhost to trusted origins.
`api_public_origin` likewise checks referral URLs against the frozen public API
identity while requests use the isolated boot-test port. Both overrides must be
exact HTTPS origins.
The client follows no redirects, retains no cookie jar, writes
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

The fixture preserves the **complete canonical rendered profile**. It never
removes speech/LLM contracts or substitutes an embedding HTTP fake. Native
startup requires **all three** existing, absolute model-store directories.
Stores are mounted read-only, the normal `Dockerfile.llm` is built, both
production Ollama artifact checks run, and actual BGE-M3/Qwen services start.
Backend admission verifies the speech bundle and executes real
Kokoro/SenseVoice and Qwen readiness. Missing or incomplete stores are rejected
before fixture state is created.

```bash
python3 deploy/self-host/ci/product.py \
  --output /absolute/new-server-fixture --brand-id eddy \
  --embedding-store /absolute/bge-m3-store \
  --llm-store /absolute/qwen-store \
  --speech-store /absolute/speech-store
```

The same local/CI shell entry requires `SELF_HOST_CI_EMBEDDING_STORE`,
`SELF_HOST_CI_LLM_STORE` and `SELF_HOST_CI_SPEECH_STORE` together. Provision with
`deploy/self-host/prepare-model.py --kind embedding --output ...`,
`prepare-model.py --kind llm --output ...`, and
`deploy/self-host/prepare-speech.py --output ...` beforehand. The fork CI
workflow runs these existing digest-verifying provisioners when the product
check is selected; runtime startup never downloads or substitutes models.
CI therefore needs model-registry/release-download access, disk space for the
stores and images, and at least 12 GiB of Docker memory. Insufficient resources
fail the gate; no reduced-capability runner is selected instead.

An explicitly selected [MiMo fixture](../../deploy/self-host/mimo-local.md)
requires only the embedding store and a private `--mimo-secret-file` credential
file (shell entry: `SELF_HOST_CI_MIMO_SECRET_FILE`). Do not also select native
LLM/speech stores. Only the API and canonical-memory maintenance worker receive
that selected credential and outbound network access. CI uses native models and
needs no hosted-provider credential.

Before building or starting application containers, the fixture checks Docker's
memory against selected Compose model limits plus 4 GiB application/engine
headroom (currently 12 GiB native, 8 GiB MiMo).
`model-capacity.json` records the decision. This rejects the actual 2026-09-05
8 GiB native VM that killed `llama-server` during finalization; it is an admission
floor, not a throughput or concurrent-workload guarantee.

Both native and MiMo selections expose actual WebSocket Upgrade through the same Auth/API proxy.
The upstream server owns authentication and the handshake response. The tunnel
preserves parser-buffered first frames and binary PCM in both directions, and
closes both sockets on disconnect, 30-second inactivity, its 15-minute deadline
or its 64 MiB per-direction bound. It never synthesizes transcripts. Real socket
tests run in the existing `product.sh` lane alongside HTTP-framing and process
ownership tests.
HTTP OPTIONS is forwarded to the same upstream owner, preserving both CORS
approval and denial; the proxy never manufactures a permissive preflight.

`--self-test` executes sixteen common core cases on this complete runtime.
“Core” names that HTTP case inventory, not a runtime mode. Starting real models
is not evidence that recording, finalization, canonical-memory retrieval or the complete CI-1 qualifier passed.
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

This is the identity/JIT/onboarding/calendar/email/CSAT/memory/tasks slice of **CI-1**. It deliberately does
not implement `qualify-dual-target.mjs`, which owns the complete candidate and
platform/brand product qualification. Recording finalization, conversation and
memory retrieval, process restart, full error shapes, client UI, supported OSes
and actual releases require their corresponding evidence. A core result always
sets `release_qualified` to false.

## Run the frozen candidate on both local targets

`node contracts/deployment/regress.mjs --candidate /absolute/new-candidate`
runs all four existing Cloudflare suites against the candidate's frozen Worker,
Web and SQL bytes, alongside the Server runner from that verified source. It
compares the common HTTP case IDs, waits for both targets to clean up even when
one fails, and prints the private report directories. The combined JSON contains
the candidate digest, brand, individual cases and `release_qualified: false`.
The Server runner honors `SELF_HOST_CI_PORT` and the byte-verified
`SELF_HOST_CI_RUNTIME_IMAGE`; inference in this common slice remains controlled.

The normal `release.mjs prepare` CLI now runs this command's same implementation
after freezing the candidate and fails when either local target fails. Its
combined report is saved separately at the printed `product_report` path. This is a required local
release step; it does not replace the wider CF-4/CI-1 or deployed-provider gates.
A failed local run may retain an immutable candidate for diagnosis, but the
prepare command exits nonzero and never uploads a Worker.
