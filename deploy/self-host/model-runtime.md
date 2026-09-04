# Current local model runtime

The self-host reference enables BGE-M3 embeddings. STT, TTS and push remain
explicitly disabled in this intermediate package. The older full-cutover
sections of README.md and their scripts are not evidence of implemented audio
providers. An enabled recording/transcription/playback path remains SH3 work;
full provider/cutover attestation remains SH4 work.

`deploy/profiles/self_hosted.yaml` is the public model owner. The renderer derives
`capabilities.embedding_dims` from its embedding contract; clients, backend,
model-store validator and Qdrant migration consume the same result. There is
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

Disabled speech requests return HTTP 503 with
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
