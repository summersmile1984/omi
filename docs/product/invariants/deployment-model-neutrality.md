# INV-DEPLOY-1: Deployment and model neutrality

**Status:** proposed
**Statement:** A self-hosted Omi release must preserve the complete authenticated product loop without requiring Omi-operated infrastructure or a specific model, identity, data, queue, object-storage, embedding, vector, speech, or search vendor.

## MUST

- One signed deployment profile selects every public API, WebSocket, auth, MCP,
  analytics, update, and object-download origin used by a client release.
- The backend selects identity, document storage, object storage, durable work,
  chat/completion, embedding, vector, STT, TTS, and search providers through
  typed capability boundaries. Product code routes by workload/capability, not
  by vendor name.
- Direct and gateway model modes resolve the same workload route and bounded
  fallback policy. A gateway must never silently replace the selected provider.
- Provider failure is explicit. A required feature may fail closed with a typed
  unavailable response; it must not silently return empty memory, search,
  knowledge, task, or conversation results.
- Provider-backed projections carry provider, model, dimension, schema, and
  namespace versions so a migration can dual-write, backfill, verify, switch,
  and roll back without changing product authority.
- PostgreSQL, Redis, and S3-compatible implementations preserve the production
  contracts they replace: document paths and isolation, atomic/CAS writes,
  complete privacy deletion, at-least-once work with bounded retry/DLQ, and
  externally reachable object URLs.
- A production self-hosted deployment fails fast when required secrets,
  migrations, origins, or provider capabilities are absent. Development
  defaults and arbitrary-UID token issuers are forbidden in that profile.

## MUST NOT

- A release-selected self-hosted profile must not contact `api.omi.me`, Google
  Identity Toolkit/Secure Token, Omi PostHog, Omi update feeds, GCS, Firestore,
  Cloud Tasks, or a model-vendor host unless that exact external provider is
  explicitly selected in the deployment manifest.
- Authentication entry points must not bypass the configured identity verifier.
- A queue adapter must not acknowledge work before durable completion or lose
  the ability to redeliver an idempotent task generation.
- A storage adapter must not return an origin belonging to a different provider.
- Provider selection must not change subscription, quota, input-validation,
  privacy, deletion, or rate-limit policy.

## Surfaces

- Backend data, queue, storage, identity and provider adapters
- LLM gateway, embeddings, vector search, STT, TTS and web search
- Flutter, macOS Desktop, Windows Desktop, Context for Claude, MCP helper and release packaging
- Self-hosted deployment manifests, migrations, readiness and observability

## Guard tests

- `.github/scripts/test_check_self_host_deployment.py`
- `.github/scripts/test_self_host_client_build_entrypoint.py`
- `.github/scripts/test_self_host_operations.py`
- `backend/tests/unit/test_cutover_live_smoke.py`
- `backend/tests/unit/test_mobile_tts_provider_policy_parity.py`
- `backend/tests/unit/test_canonical_consolidation.py`
- `auth-server/test/http.test.js`
- `backend/tests/unit/test_firestore_pg_contract.py`
- `backend/tests/unit/test_cloud_tasks_redis_contract.py`
- `backend/tests/integration/test_cloud_tasks_redis_contract.py`
- `backend/tests/unit/test_storage_minio_contract.py`
- `backend/tests/integration/test_storage_minio_contract.py`
- `backend/tests/unit/test_identity_provider_boundary.py`
- `backend/tests/unit/test_model_neutral_routing.py`
- `backend/firestore_pg/tests/`
- `app/test/unit/auth_identity_configuration_test.dart`
- `app/test/unit/env_test.dart`
- `app/test/unit/stt_deployment_policy_test.dart`
- `app/test/unit/onboarding_identity_test.dart`
- `app/test/unit/intercom_deployment_policy_test.dart`
- `desktop/macos/Desktop/Tests/DeploymentProfileTests.swift`
- `desktop/macos/Desktop/Tests/EmbeddingCapabilityProjectionTests.swift`
- `desktop/windows/src/shared/deploymentProfile.test.ts`
- `desktop/windows/src/main/auth/betterAuthClient.test.ts`
- `desktop/windows/src/main/rewind/embeddingClient.capability.test.ts`
- `desktop/windows/src/renderer/src/lib/identity.selfhost.test.ts`
- `desktop/windows/scripts/check-self-host-artifact.test.mjs`
- `desktop/context-for-claude/Tests/ContextCoreTests/DeploymentProfileTests.swift`
- `desktop/context-for-claude/Tests/ContextAppTests/ContextBetterAuthClientTests.swift`

These paths are the required target guard surfaces. The invariant remains
proposed until the implementation and guards are green and unchanged for seven
days.

## Path globs

- `backend/firestore_pg/**`
- `backend/utils/{auth_shim.py,cloud_tasks.py,cloud_tasks_redis.py}`
- `backend/utils/other/storage*.py`
- `backend/utils/llm/**`
- `backend/llm_gateway/**`
- `backend/database/vector_db.py`
- `backend/utils/stt/**`
- `backend/routers/{tts.py,desktop_tts_updates.py}`
- `auth-server/**`
- `app/lib/{env,providers,services,backend}/**`
- `desktop/macos/Desktop/Sources/**`
- `desktop/windows/{src,scripts}/**`
- `desktop/windows/{.env.example,.env.selfhost.example,electron.vite.config.ts,package.json}`
- `desktop/context-for-claude/Sources/**`
- `deploy/self-host/**`

## PR rule

Name `INV-DEPLOY-1` in the PR body when touching a path glob above. Until the
invariant is locked, every PR must state which neutrality capability it proves,
which remains incomplete, and the exact zero-vendor or provider-conformance
evidence it ran.
