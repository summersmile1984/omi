# Current local model runtime

Text inference is hosted: the brand-declared operator AI (openrouter,
siliconflow, cloudflare-gateway or mimo-cn) owns chat, ASR and TTS, and
[Local Server OS with Xiaomi MiMo](mimo-local.md) documents that variant. A
profile row that still carries a local `llm` is refused by `fork.bootstrap`
before any workload import, so there is no Ollama generation image, no
`Dockerfile.llm` and no `LLM_IMAGE`/`LLM_MODEL_STORE`.

The local model surface is pinned BGE-M3 embeddings and, when the profile
carries it, the SenseVoice/Kokoro speech bundle described in
[speech-runtime.md](speech-runtime.md).
Push and speaker identification remain disabled. The older full-cutover sections
of README.md and their scripts are not deployment attestation.

`deploy/profiles/self_hosted.yaml` is the public model owner. The renderer derives
`capabilities.embedding_dims` from its embedding contract and `llm_provider`
from the selected operator, falling back to the optional local text contract
when one exists; clients, backend, model-store validators and
Qdrant migration consume the same result. There is
no independent model/dimension environment default. BGE-M3 is F16, 1024
dimensions and 8192 context tokens. The manifest and GGUF SHA-256 digests are
both pinned; the `latest` string is only the concrete Ollama model identifier.
Changing a tag upstream cannot change an admitted model.

Provision an isolated Ollama model store outside this repository containing the
pinned manifest under `manifests/registry.ollama.ai/library/bge-m3/latest` and
all referenced `blobs/sha256-*` files, including config/license. Set
`EMBEDDING_MODEL_STORE` to that absolute path. Do not mount a store that another
Ollama process may modify. No model downloads occur during API requests.

Build through `build-images.sh`. Compose runs `embedding-artifact-check` using
that generated backend image, verifies manifest and every referenced blob, then
starts the digest-pinned Ollama 0.33.3 amd64 service with a read-only model mount.
It has no host port or GPU binding. API requests use `http://embedding:11434`,
four CPU threads, a 128-token evaluation batch and mmap; async callers use the
bounded upstream provider executor. Budget sufficient RAM alongside the other
services: a model file size alone is not runtime memory sizing. `/api/tags`
health only proves the daemon; API admission additionally performs real model
inference and fails on load/identity/shape errors.


Text identity is the profile's `operator_ai` selection. `fork/operator_ai.py`
freezes each vendor's chat, ASR and TTS models, endpoints, credential
environment variables and exact per-capability egress grants; credentials never
enter the profile and there is no vendor or model fallback. `operator_chat.py`
and `mimo_chat.py` build the chat client through the existing LangChain tool and
usage owners. Admission binds the agent stream first-event, stream maximum and
queue finalization budgets from the selected vendor's
`request_timeout_seconds`.

Run `prepare-model.py --kind embedding --output ...` before deployment, then
set `EMBEDDING_MODEL_STORE` to an operator-owned directory. Provisioning
downloads only from `registry.ollama.ai`, bounds manifest, layer count and
layer bytes, verifies all SHA-256 digests in a temporary directory, then
atomically publishes the complete store. A failed download leaves no accepted
store. Runtime mounts are read-only and never download.


Compose runs one pinned Ollama service (embeddings). There is no local
generation service: text, ASR and TTS requests leave through the selected
vendor's exact endpoint grant, and `fork.egress_policy` refuses any other
authority before DNS resolution. Transport failure never becomes an empty
successful answer; a hosted stream failure stays an in-band error rather than
a silent empty response.

The dated verification includes a reusable local recording command and its
input contract: a synthetic account JWT and real 16 kHz mono PCM WAV. It exercises
`/v4/listen`, durable recognized segments, public speaker assignment, explicit
finalization, canonical memory and retrieval through `/v2/messages`. No transcript
or memory is directly seeded. A generated spoken fixture proves TTS-to-ASR
software flow, not a microphone or hardware. The temporary evidence runner is
not yet the shared dual-target CI or release acceptance gate; SH4 owns that
integration. Hermetic inference seams do not qualify actual model quality.

Before API startup run the Compose `qdrant-migrate` service. A new collection
atomically receives the complete embedding contract and its vector schema.
Existing collections with no identity metadata, a different model, a different
artifact, or different dimensions are refused. Use a reviewed new collection
prefix, backfill from source text, compare retrieval, then explicitly cut over.
Never modify the metadata of old vectors to make admission pass.

The adapter verifies runtime `/api/tags` and `/api/show` before each inference.
`/api/embed` sets `truncate: false`; empty, zero, malformed, wrong-size or
wrong-model results fail rather than returning a successful empty search.
Upstream Tasks still preserve their already-committed row if later indexing
fails; that existing best-effort behavior does not prove indexing succeeded.

When the optional speech bundle is absent, speech requests return HTTP 503 with
`deployment_capability_disabled`, the capability and `retryable: false`.
Streaming speech completes the handshake then closes 1008 / `stt_disabled`.
Background STT selection throws the same typed disabled condition. Public
push/token APIs refuse; internal reminder sends return actual zero deliveries
with shared telemetry, including Tasks with a due date. Omi-cloud and Cloudflare
retain their own profile behavior; no Firebase/vendor credential enables a
self-host disabled capability.

Hermetic tests run in the existing `fork-selfhost-startup` lane. CPU inference,
actual PG/Qdrant/Redis/Auth and wire HTTP/WebSocket evidence are recorded in the
dated SH3 verification document; they are not replaced by controlled vectors.

The selected vendor's `request_timeout_seconds` owns the model deadline:
admission binds it to the chat first-event wait (x1), the whole chat stream
(x2) and finalizer HTTP dispatch (x4). The frozen vendors report 120 seconds,
so a Server OS profile binds 120/240/480 seconds. These limits are
source-selected, and no cloud-client timeout option may silently replace
them.

The `memory_l1` route keeps the original `WorkingObservationBatch` JSON schema,
prompt, Pydantic parser, attribution and memory writes. A response that
violates that parser remains an extraction failure; no parser relaxation or
fabricated memory is used to certify a model
