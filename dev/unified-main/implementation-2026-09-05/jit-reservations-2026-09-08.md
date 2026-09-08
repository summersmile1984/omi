# JIT reservation transaction verification — 2026-09-08

Source base: `afa6ecb69c`, plus the reservation implementation in this commit.
The hosted build used the ordinary Core builder and unchanged entrypoint.
All 220 module-directory file hashes (219 Python files) and 192 App migrations
through 0189 match the archived payload; current Core source files and migration
bytes were checked against those hashes after the hosted run.

## Original business policy and D1 boundary

The projector stages the original `backend/models/jit_proactivity.py` models,
`backend/database/jit_proactivity_store.py` reservation policy,
`backend/routers/jit_rollout.py` wire models and original trigger compiler and
authority predicates. It relocates Firestore reads/writes into the existing
Candidate D1 transaction owner. Limits, request hashes, receipt identities,
parent/trigger eligibility and default prompts remain unchanged.

D1 supports atomic SQL batches, including whole-batch rollback on error:
[official batch API](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch).
It does not provide the upstream Firestore callback as a drop-in interface.
Core reads authority, evaluates the original policy, and submits exact-state
checks plus all writes in one D1 batch. Checks cover observed records, account
generation/deletion, rollout/kill flags, memory control, timezone and trigger
revision/metadata. They raise SQL errors on conflicts; an update affecting zero
rows is not treated as sufficient conflict detection. The full policy can retry
up to five times, retaining the original request timestamp. Replays also check
current authority and commit their read guards.

Timezone comes from the existing latest CF FCM registration, using the same
ordering as the daily-summary owner. Missing/invalid zones fail closed. The
Worker packages `tzdata==2024.1`, matching the upstream pin. This is a target
storage mapping, not a client-selected timezone or a UTC fallback.

## Hosted result

The disposable Worker `eddy-jit-live-20260908-b-core`, version
`385b31d5-b084-488e-9223-494b0d5451ad`, ran the normal Core entrypoint against
real D1 and an isolated producer Queue. The fixture used synthetic signed
principals; existing public FCM registration and memory intake created timezone
and memory control. Operator flags and the planned-trigger fixture were seeded
through SQL. No model call, notification send or Jobs consumer was involved.

| Exercised behavior | Observed result |
| --- | --- |
| 12 concurrent notification reservations | Three accepted, nine conflicted; one shared daily budget |
| Parent-bound full turns | Three accepted; wrong device and second turn for a candidate rejected |
| Receipt replay and changed input | Same receipt with `reserved=false`; changed input conflicted |
| 12 concurrent nano triages | Eight accepted, four conflicted |
| Planned trigger | One reservation/day; invalidated action rejected the old receipt |
| Final receipt write deliberately aborted in D1 | HTTP 503; zero partial business rows; same event succeeded after fault removal |
| Missing timezone and disabled rollout | HTTP 409 and 403 respectively |
| Owner export and isolation | Six owned events; other account saw zero; transaction guards empty |

The rollback observation was recorded at **2026-09-08T01:47:56.655Z**.
The complete hosted suite passed at **01:48:00.531Z**. The owned Worker, Queue
and D1 were then deleted and observed absent by **01:48:11.968Z**. No Eddy
production resource was changed. The earlier A fixture's resources were also
confirmed absent.

Initial upload reported a workers.dev enablement failure with API code 10007.
Inspection then found the expected tagged version already deployed at 100%,
with workers.dev disabled. After matching the recorded Worker version, Queue
producer and D1 ID, the driver enabled the owned subdomain and continued on the
same version/resources. It did not re-upload code or bypass version ownership.

## Local verification and reproducibility

- Core: `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests` — **1170 passed**, one dependency deprecation warning, 604.97 seconds. The new reservation file contains 22 cases and runs through the actual ASGI entrypoint and all migration SQL.
- Workers, from `deploy/cloudflare`: Node 22 `node_modules/vitest/vitest.mjs run` — **1030 passed**, 128 files, 18.16 seconds. Coverage includes actual Edge forwarding/rate policy and the existing deletion-registry guards.
- Original upstream store: `BACKEND_UNIT_TEST_FILE_LIST=/private/tmp/eddy-jit-upstream-tests.txt bash backend/test.sh`, with only `tests/unit/test_jit_proactivity_store.py` selected — **21 passed**, 0.40 seconds. This exercises its existing Firestore test seam, not a hosted Server deployment.
- Node 22 `node_modules/typescript/bin/tsc --noEmit` and `scripts/validate-manifests.mjs` passed; the inventory retains **17 blocked routes**.
- Node 22 `scripts/python-worker.mjs api-core sync` passed with the normalized lock. The only new dependency is upstream-pinned tzdata; pinned Worker tooling was retained.
- Node 22 `/private/tmp/eddy-jit-hosted-resume-b.mjs` exited 0 after real business assertions and cleanup. The initial driver and continuation, journals, hashes, projected modules and selected source/test logs are archived below.

An initial test intended to revoke trigger authority set `intent_backed=false`
while retaining `write_reason=standing_trigger`. That violates the original
`MemoryItem.validate_tier_invariants` model, so the strict read correctly returns
503 rather than the test's expected 409. The corrected fixture empties the
trigger action prompt while retaining a valid MemoryItem; the unchanged paid-work
predicate then rejects it with 409. A separate malformed-trigger test asserts
503. Production policy was not relaxed to satisfy a fixture.

Evidence archive:
`/Users/macstudio/.codex/eddy-production/jit-reservations-hosted-20260908/`.
`archive-sha256.json` records the retained files. Temporary assertion-key files
were removed after matching the archived proof and completed cleanup. Raw
control-plane account metadata and credentials are excluded.

## Acceptance still outstanding

This proves the reservation storage adapter on hosted Python Workers/D1.
It does not prove this route through public Auth/Edge, real queued account
erasure, the common two-target reservation HTTP contract, native trigger
creation/feedback or the complete JIT flow. Local Edge and original upstream
tests are distinct evidence, not substitutes for that product contract.

The route retains its blocked inventory/manifest classification under the
[migration completion gate](../09-cloudflare-route-migrations.md#completion-gate).
The four trigger/ledger snapshot and feedback routes still require their CF
owners. Full CI-1/CF-4 qualification and production deployment remain unfinished;
the hosted journal explicitly records `release_qualified=false`.

Subsequent evidence: the [public trigger-snapshot run](jit-trigger-snapshot-2026-09-08.md)
also passed this reservation route through real signup/session/JWT and the
ordinary Auth/Rate Limit/Edge/Core Workers. It used the snapshot's actual account
generation, preserved the shared daily budget under contention, rejected an old
receipt after privacy revocation and proved logout revocation. Shared two-target,
queued erasure and native workflow qualification still remain.
