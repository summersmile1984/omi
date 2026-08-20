# Deployment and model neutrality convergence

**Status:** replacement-service implementation complete; assembled live product loop pending

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
 hosted:  BetterAuth/Postgres/MinIO/Redis/generic routes
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

### Identity and clients

- [x] Every HTTP, WebSocket, MCP, OAuth and ownership flow uses one configured verifier.
- [x] Identity unavailability is retryable; invalid credentials remain 401.
- [x] BetterAuth production config fails fast and supports migration, readiness,
      session revoke and complete account deletion.
- [x] Flutter, macOS and Context release entrypoints use one deployment profile
      for login, refresh, API, WebSocket and MCP; no Firebase session is present.
- [ ] Fresh signed Flutter artifacts have been exercised on both mobile
      platforms. The iOS Release artifact and native resource contract were
      exercised without codesigning; macOS and Context signed artifacts were
      exercised end-to-end.

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
      passed locally; the intended host's public HTTPS reverse proxy and
      hairpin path have not been exercised.
- [ ] A zero-vendor egress gate starts with all managed-vendor credentials
      removed, denies undeclared public origins, and exercises the complete
      Capture → Understand → Remember → Retrieve → Act loop plus account deletion.

The remaining unchecked gate is deliberately narrow. The live acceptance run
on 2026-08-20 exercised Better Auth, PostgreSQL, Redis, MinIO, Qdrant, generic
Ollama chat and 768-dimensional embedding, real SenseVoice PCM decode, and
account deletion with undeclared egress denied. The complete
Capture → Understand → Remember → Retrieve → Act behavior currently passes a
hermetic provider-boundary contract only; it has not yet been sent end-to-end
through the operator's generic inference endpoint on the intended release host.
Its evidence therefore records `authorizes_production_cutover=false`.

## Explicit self-hosted capability gaps

These paths are deployment-neutral because they fail closed before vendor
egress, but they do not yet have feature parity with the managed deployment:

- macOS OCR/task embeddings and Rewind screenshot semantic search need a typed
  backend embedding endpoint;
- proactive image/tool loops need a provider-neutral backend tool capability;
- realtime needs a signed backend relay selection; without it push-to-talk
  uses backend-selected prerecorded STT;
- macOS chat permits the backend `pi-mono` adapter only; direct vendor adapters
  and the local Claude task-agent process are unavailable;
- the self-host profile explicitly disables its unbundled Typesense keyword
  projection and retains PostgreSQL/Qdrant memory retrieval.

## Delivery order

1. Correctness boundaries: PostgreSQL, queue, identity and STT/TTS policy.
2. Complete storage, model routing, embedding/vector capability boundaries.
3. Release-client deployment profiles and production service packaging.
4. Migration tooling and full zero-vendor acceptance evidence.

No provider cutover is allowed merely because unit tests are green. Each
capability switches only after its conformance and fault suite passes against
the real replacement service.
