# Current local model runtime

For the explicit hosted LLM/ASR/TTS option with local embedding, see
[Local Server OS with Xiaomi MiMo](mimo-local.md). The pinned offline reference
below remains the default when that option is not selected.

The self-host reference selects pinned BGE-M3 embeddings, Qwen3 1.7B text inference,
and the SenseVoice/Kokoro speech bundle described in [speech-runtime.md](speech-runtime.md).
Push and speaker identification remain disabled. The older full-cutover sections
of README.md and their scripts are not deployment attestation.

`deploy/profiles/self_hosted.yaml` is the public model owner. The renderer derives
`capabilities.embedding_dims` from its embedding contract and `llm_provider`
from the optional text contract; clients, backend, model-store validators and
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


Qwen3 1.7B is the sole text owner for every admitted upstream feature. The
profile records its Ollama manifest and GGUF SHA-256, native 40960-token context,
8192-token serving window and 2048-token output ceiling separately. The lower
serving values are measured resource admission, not changed artifact metadata.
The adapter uses Ollama's native chat/schema/tool protocol, disables thinking
and context shifting, refuses implicit truncation, and unloads after each
request. It retains the upstream chat/tool loop and its real user-scoped
PostgreSQL/Qdrant tools, while excluding web search, hosted apps and vision.
BYOK, another model name, an unknown feature or a vendor credential cannot
change this owner.

The same model contract fixes serving KV-cache precision to `q8_0`. This halves
the cache budget relative to F16 with a small precision tradeoff; it does not
change the Q4_K_M model artifact. `Dockerfile.llm` takes the generated backend
image, compiles its sole stage through `fork.llm_runtime`, and copies only that
public environment into the pinned Ollama runtime. Its entrypoint applies the
compiled context, flash-attention, KV type, no-cloud and unload settings before
serving, so an unrelated shell variable cannot silently change them. Set
`LLM_IMAGE` to the resulting image name. Actual runtime logs must establish flash
attention and Q8 cache use; profile validation alone is not that evidence.

Run `prepare-model.py --kind embedding --output ...` and `--kind llm --output ...`
before deployment, then set `EMBEDDING_MODEL_STORE` and `LLM_MODEL_STORE` to
separate operator-owned directories. Provisioning downloads only from
`registry.ollama.ai`, bounds manifest, layer count and layer bytes, verifies all
SHA-256 digests in a temporary directory, then atomically publishes the complete
store. A failed download leaves no accepted store. Runtime mounts are read-only
and never download. Apache-2.0 Qwen source/model references and the reviewed
artifact digests are recorded in the dated verification evidence.

Compose starts separate pinned Ollama services because embedding and generation
have different memory budgets. API admission checks version, exact inventory,
manifest, sole GGUF source, native context and completion/tool capabilities,
then runs a real completion. Each generation repeats identity validation. JSON
responses are limited to 1 MiB and streams to 4 MiB; socket connect/read/write/TLS
operations share a monotonic deadline. Standard-library DNS resolution cannot be
interrupted by that socket deadline and is not claimed as an absolute DNS bound.
Transport failure never becomes an empty successful answer. HTTP requests that
fail before streaming return typed 422/503; an SSE model failure after HTTP 200
remains an in-band `error:` terminal condition.

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

The admitted CPU budget is four threads and one concurrent generation. The
measured 2-CPU fixture processed about 15 prompt tokens/second and exceeded the
upstream structure feature's 60-second cloud-provider default. Four CPUs yielded
about 28–31 prompt tokens/second. The profile therefore owns a 300-second model
request deadline; bootstrap projects 300 seconds to the chat first-event wait,
600 to the whole chat stream and 1200 to finalizer HTTP dispatch (below its 1500-
second PG lease). CPU generation can take minutes. These limits are finite and
source-selected, and no cloud-client timeout option may silently replace them.

The `memory_l1` route passes the original `WorkingObservationBatch` JSON schema
through the same canonical/captured feature-options owner to native generation.
It retains the existing prompt, Pydantic parser, attribution and memory writes.
A native HTTP 200 response that violates that parser remains an extraction
failure; no parser relaxation or fabricated memory is used to certify the model.
