# Deployment and model neutrality convergence

**Status:** replacement services and a local assembled HTTPS loop are exercised; production cutover remains blocked

**Decision date:** 2026-08-20

**Proposed invariant:** [`INV-DEPLOY-1`](../product/invariants/deployment-model-neutrality.md)

## Outcome

Omi can be built and operated as a production self-hosted release without Omi's
Firebase/GCP plane and without a mandatory AI vendor. The same source tree also
continues to support the upstream managed deployment. Provider choice changes
infrastructure, cost, and quality; it does not change product policy or data
correctness.

“Starts with no cloud credentials” is not completion. Completion means a fresh
signed client can sign up, restore and revoke a session; capture, finalize,
remember, retrieve and act; upload and download objects; execute durable jobs;
and delete the account completely in the self-hosted profile.

## Target architecture

```text
signed DeploymentProfile
  api/auth/ws/mcp/object/update/analytics origins
                     |
                     v
              backend capability ports
   +-----------+--------+-------+--------+----------------+
   | identity  | docs   | blob  | work   | model/search   |
   +-----------+--------+-------+--------+----------------+
 managed: Firebase/Firestore/GCS/Cloud Tasks/vendor routes
self-host: BetterAuth/Postgres/MinIO/Redis/generic routes
                     |
      authoritative product services and invariants
```

## Completion gates

### Data correctness

- [x] Nested document paths retain every parent identity.
- [x] Batch and transaction writes are atomic; update-time preconditions are real CAS.
- [x] Firestore query/cursor/collection-group behavior used by production has
      emulator-to-PostgreSQL conformance coverage.
- [x] Account deletion proves zero UID-keyed/content rows, objects, vectors, and
      auth sessions after reconciliation; the sole retained row is one keyed
      pseudonymous denial/idempotency receipt with no direct UID or feedback.
      The Compose `--live` acceptance seeds and reconciles PostgreSQL, MinIO,
      Qdrant, Better Auth and the exact Redis task before recording this proof.

### Durable work and objects

- [x] Redis enqueue is atomic and consumers use lease/ack with crash recovery.
- [x] Retryable status policy, bounded backoff, attempt count, task generation,
      DLQ and per-queue authentication match handler contracts.
- [x] MinIO supports every object operation used by product code and returns an
      externally reachable configured origin; provider errors are not “missing”.
      Cutover acceptance requires signed PUT/GET/DELETE through that exact
      public HTTPS origin and verifies authoritative absence after deletion.

### Identity and clients

- [x] Every HTTP, WebSocket, MCP, OAuth and ownership flow uses one configured verifier.
- [x] Identity unavailability is retryable; invalid credentials remain 401.
- [x] BetterAuth production config fails fast and supports migration, readiness,
      session revoke and complete account deletion.
- [x] Flutter, macOS and Context release entrypoints use one deployment profile
      for login, refresh, API, WebSocket and MCP; no Firebase session is present.
- [ ] Fresh operator-signed Flutter artifacts have been exercised on both
      mobile platforms. The self-host build, Firebase-runtime boundary and
      unsigned/local artifact scans are green; a real operator certificate,
      device install, sign-in, capture and MCP session are still external
      evidence requirements.

### Models and projections

- [x] Every model workload resolves through one route manifest in direct and gateway modes.
- [x] Generic OpenAI-compatible completion/embedding providers have configurable origins.
- [x] Embedding/vector records are versioned and have dual-write/backfill/switch/rollback tooling.
- [x] Agent chat, Notes, memory/KG, task recommendations, proactive, screen,
      realtime and web search have an explicit selected route or typed unavailable result.
- [x] STT sockets honor the production segment callback and never block the event loop;
      TTS applies identical validation/subscription/rate limits across providers.

### Production operations

- [x] `deploy/self-host` pins images and includes backend, auth, worker,
      PostgreSQL, Redis, MinIO and Qdrant; the generic model endpoint remains an
      explicit operator-provided prerequisite rather than a bundled service.
- [ ] Migrations, backup/restore, secrets, TLS/public origins, readiness,
      metrics, worker supervision and rollback are documented and exercised.
      Migration, real volume backup/restore, readiness and worker supervision
      passed locally. A temporary-CA HTTPS reverse proxy can exercise exact public
      backend/Auth/MCP/object origins and the container hairpin path; the intended
      host's certificate, DNS and edge have not been exercised.
- [ ] A zero-vendor egress gate starts with all managed-vendor credentials
      removed, denies undeclared public origins, and exercises the complete
      Capture → Understand → Remember → Retrieve → Act loop plus account deletion.

The unchecked gate is evidence-sensitive. A combined local cutover run on
2026-08-20 exercised Better Auth, PostgreSQL, Redis, MinIO, Qdrant, generic
Ollama chat and 768-dimensional embedding, real SenseVoice PCM decode, account
deletion, and the hermetic no-network contracts. The same run drove a
disposable Better Auth principal through public WSS
SenseVoice Capture, authenticated generic-model Understand, an authenticated
agent tool call to a Wikipedia-only SearXNG, public Remember, PostgreSQL/Qdrant
projection, public Retrieve, and public Act with conversation/memory
provenance. It also verified the effective SearXNG secret equals the reviewed
configuration without recording that secret or its hash. That functional run
used a dirty worktree while its artifact named only the older `HEAD`, so it is
not valid source-attributed cutover evidence. The gate now rejects dirty
cutover runs before service startup and records the full commit, tree, and
clean-worktree state. It now also rebuilds and verifies the application image
source labels/config hash against the exact running containers, requires signed
object CRUD through the public object edge, and records a final service-health
snapshot. A clean-tree combined rerun remains required before the exact tested
local configuration can be authorized.

That historical local run cannot authorize production: Compose does not deny
application egress and the intended public edge has not run. The current gate
also requires a real mounted Sherpa speaker-model decode/embedding and an
operator-owned mlx-audio MOSS pre-recorded call. The latter must expose the
exact configured model through `/v1/models` and return at least two speakers
with multiple transitions for the real operator-mounted fixture; a vector or
SenseVoice window-clustering check is insufficient. The mlx-audio service does
not expose revision/cache provenance, so those remain explicit operator
responsibilities rather than source-attested evidence. The gate still does not
claim full speaker enrollment/match parity. An initial local Qwen 7B run exposed an ambiguous primary-user
aboutness instruction and was correctly blocked by the unchanged subject-safety
validator. After the production prompt made the `self + primary_user` mapping
explicit, two consecutive full HTTPS runs reached validated Long-term admission,
projection and public retrieval. External mode now
requires an operator policy artifact plus exact-workload denials for OpenAI,
Google, Anthropic, Omi and an arbitrary public-IP sentinel; no such production
run has been claimed. Valid source-attributed evidence therefore continues to record
`authorizes_production_cutover=false`.

## Explicit self-hosted capability gaps

The backend now supplies typed OCR/task/Rewind embeddings, generic
completion-backed proactive image/tool calls, and an authenticated bounded
realtime relay to an operator-selected OpenAI-compatible WebSocket. The macOS
and Windows self-host profiles consume those contracts with projection identity
fences, and their direct vendor transports fail closed before URL construction.
The remaining paths are deployment-neutral because they fail closed before
vendor egress, but they do not yet have feature parity with the managed
deployment:

- app-icon image generation can use an explicit operator-owned
  OpenAI-compatible image endpoint, but remains disabled in the checked-in
  profile until the operator configures it;
- attached-file chat can use `FILE_CHAT_TRANSPORT=local_extraction`: originals
  remain private in the configured UID-scoped object store, bounded local
  extraction handles text/Markdown/JSON/CSV/PDF/DOCX and restricted inline
  images, and answers execute the `chat_responses` manifest route. Managed
  deployments may retain the explicit `openai_assistants` transport;
- legacy Gemini REST proxy and provider-native omni WebSocket relay are
  explicitly disabled by the self-host deployment authority before vendor URL
  or credential resolution; the primary `/v2/chat/completions` path and signed
  proactive endpoint execute the `chat_agent`/proactive manifest through the
  generic OpenAI-compatible target with bounded retryable-only fallback. Its
  explicit public-web lane uses SearXNG with only the trusted current-user
  instruction and injects results as untrusted context;
- macOS chat permits the backend `pi-mono` adapter only; direct vendor adapters
  and the local Claude task-agent process are unavailable;
- the self-host profile uses explicit, operator-owned Typesense projections for
  canonical memory and conversation keyword search. The projection schema,
  rebuild/reconcile commands and account-deletion ordering are independent of
  the retired Firebase Typesense extension; an unavailable selected service is
  a typed search failure, never an empty vector-only result.
- push delivery is explicitly `PUSH_PROVIDER=disabled` until an operator push
  service is configured; notification requests return a typed unavailable
  capability rather than silently calling Firebase.
- Agent VM cleanup is explicitly `AGENT_VM_PROVIDER=disabled`; legacy GCE
  state blocks deletion until it is imported/reconciled, so missing ADC cannot
  be mistaken for successful cleanup.
- Firebase Auth users and Firebase Storage objects have generation-pinned,
  checkpointed import tools. These are safe to run against production exports,
  but the repository contains no production credentials and therefore does not
  claim that an operator's historical export has been migrated.

## Delivery order

1. Correctness boundaries: PostgreSQL, queue, identity and STT/TTS policy.
2. Complete storage, model routing, embedding/vector capability boundaries.
3. Release-client deployment profiles and production service packaging.
4. Migration tooling and full zero-vendor acceptance evidence.

No provider cutover is allowed merely because unit tests are green. Each
capability switches only after its conformance and fault suite passes against
the real replacement service.
