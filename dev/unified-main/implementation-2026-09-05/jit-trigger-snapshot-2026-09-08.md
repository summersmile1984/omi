# Authoritative JIT trigger watchlist — 2026-09-08

Source base: `dc10852d5b`, plus the snapshot adapter in this change.
`GET /v1/jit/trigger-snapshot` now reaches the ordinary Core entrypoint through
authenticated Edge forwarding. This implementation does not create triggers,
record feedback, change prompts, run matching or invoke a provider.

## Source and state ownership

`jit_snapshot_sources.py` stages the original
`backend/utils/memory/jit_trigger_snapshot.py` reader, dataclasses and revision
hashing, and the wire models/response projection from
`backend/routers/jit_rollout.py`. Its AST transformation only relocates database
reads. The paid-work compiler and policy are the same original models already
used by reservations. Storage-boundary shape changes fail the ordinary builder
instead of silently retaining a stale projection.

The D1 store reads generation independently from `cf_account_cutover`. Canonical
control must identify the same owner and generation, and its physical head,
sequence and source-generation columns must agree with its model. A complete
empty watchlist requires proven head/memory absence or an exhausted valid scan;
missing control with existing memory remains unavailable.

The store rechecks both the head and actual bounded trigger row set after the
scan. This covers existing writers that do not yet advance the canonical head.
The original reader rejects the entire watchlist for a malformed active row,
mixed generation, unavailable authority, changed state or more than 500 trigger
records. Hidden/inactive records participate in the revision fingerprint without
becoming action rows. Snoozed rows retain their expiry in the snapshot. A final
uncached rollout read can still remove authority before the response.

## Local verification

- Actual Core ASGI entrypoint and all App migration SQL: **14 new tests passed**
  in the final 11.19-second run. These cover empty/disabled distinction, owner binding,
  original action/revision behavior, invalid rows, secret-labelled triggers,
  snooze expiry, concurrent metadata and kill/deletion changes, unavailable
  control/query and the real SQL boundary at 500/501 records.
- Full Core regression: **1184 passed**, one existing dependency deprecation
  warning, 620.30 seconds.
- Full Workers regression: **1031 passed**, 128 files, 18.33 seconds. The new
  Edge test verifies signed owner/path identity and stripped forged credentials.
- Original `backend/tests/unit/test_jit_trigger_snapshot.py`: **16 passed** in
  0.28 seconds through the backend's selected-file runner. This uses its existing
  Firestore seam, not a hosted Server deployment.
- TypeScript, pinned Python formatting, manifest validation and
  `git diff --check` passed. New Python tests are discovered by the existing
  `deploy/cloudflare/ci/routes.sh` local/CI lane; no separate test runner was added.

Commands, from the repository root unless stated otherwise:

```bash
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests
BACKEND_UNIT_TEST_FILE_LIST=/private/tmp/eddy-jit-snapshot-upstream-selection.txt bash backend/test.sh
```

The backend selection contains only `tests/unit/test_jit_trigger_snapshot.py`.
In `deploy/cloudflare`, Node 22 executes `node_modules/vitest/vitest.mjs run`,
`node_modules/typescript/bin/tsc --noEmit` and `scripts/validate-manifests.mjs`.

Two initial test inputs were corrected against the upstream wire/model contract:
`MemoryItemStatus` has `hidden`, not `archived`; and the privacy fixture now uses
`secret`, which actually belongs to `RESTRICTED_SENSITIVITY_LABELS`. The made-up
label `restricted` is not in that original set. The production policy was not
changed to satisfy either expectation.

## Hosted public verification

The first isolated run proved real public signup, opaque session restore, JWT
exchange, unauthenticated denial and an authenticated actionable snapshot through
the original Auth, Rate Limit, Edge and Core Workers. Its reservation request
then received 409: the driver hard-coded generation 0, while D1's independent
account and memory authorities both held generation 1. The corrected driver
uses the generation returned by the actionable snapshot. No runtime policy was
changed for this correction.

Cleanup observation encountered one Cloudflare control-plane HTTP 500. The
driver was confirmed terminal before recovery. Read-only inspection found Edge
and Core already absent and matched the remaining Auth/Rate Limit versions and
resource IDs. Recovery removed those exact owned resources and confirmed all
four Workers, both D1 databases and the Queue absent.

The second run stopped before business execution when the Rate Limit Worker's
subdomain configuration API returned code 10013 after its version was already
active. All owned resources were cleaned up. The diagnostic runner now inspects
the exact active version and desired subdomain state for both public Workers
and the intentionally non-public Rate Limit Worker; it does not replace a
version merely because the CLI's control-plane observation failed.

The third run, `eddy-jit-snapshot-20260908-c`, passed **38 HTTP assertions**
through the unchanged Auth, Rate Limit, Core and Edge handlers. Both accounts
used real public signup, opaque session restore and JWT exchange. The App D1
had all 192 migrations through 0189; Auth D1 had all ten migrations. Operator
flags and canonical trigger rows were seeded in those disposable databases.
Timezone and initial memory/control were created through the public APIs.

| Public behavior | Observed result |
| --- | --- |
| Unauthenticated snapshot/reservation | HTTP 401 |
| Disabled versus proven-empty snapshot | Incomplete disabled receipt versus complete empty watchlist |
| Actionable trigger with forged owner headers | Correct authenticated owner, trigger/action and current generation; deterministic reread |
| Planned reservation and replay | One budget charge; replay returned `reserved=false` |
| 12 concurrent ambient requests after that planned reservation | Two accepted, ten conflicted; shared notification budget stayed at three |
| Trigger labelled `secret` | Whole snapshot invalidated; old reservation replay rejected with 409 |
| 500 controlled trigger records | Complete response containing 500 unique records |
| 501 controlled trigger records | `trigger_limit_exceeded`; no partial rows |
| Kill switch and logout | Disabled watchlist / reservation 403; old JWT returned 401 after logout |

The second account stayed disabled in the hosted fixture and received no owner
content. A separate Core SQL test enables both accounts and initializes both
canonical heads before proving cross-owner isolation; the hosted case is not
claimed as two enabled owners. These are controlled trigger fixtures, not native
trigger creation or arbitrary maximum-size memory payloads.

Business verification passed at **2026-09-08T02:33:50.021Z**. All four Workers,
both D1 databases and the Queue were matched to their owned versions/IDs, removed
and observed absent by **02:34:05.132Z** (10:34 Asia/Shanghai). No Eddy production
resource changed. The final run made no provider call, notification send or
queued account-erasure request; default prompts remained unchanged.

| Worker | Verified version |
| --- | --- |
| Auth | `07d885bd-0cb3-40dc-a1a9-574914262aee` |
| Rate Limit | `f0baee85-ae3d-4c6a-9e6e-2cbd32dd70bb` |
| Core | `d39aeec1-949b-4c56-bb9b-9a7e15135ae1` |
| Edge | `7806f9a7-1ff5-49a9-912c-a66aec981545` |

Node 22 `/private/tmp/eddy-jit-snapshot-public-20260908-c.mjs` exited 0.
All four frozen module trees, configuration, source hashes, the three run
journals, driver, cleanup recovery and selected test logs are archived at
`/Users/macstudio/.codex/eddy-production/jit-trigger-snapshot-public-20260908/`.
Core's 223 module-directory files (222 Python files) match their recorded hashes;
current Core source and all migration bytes were checked against the deployed
payload. `archive-sha256.json` indexes the retained evidence. All three runs'
temporary secret files were deleted after cleanup verification. Credentials and
raw control-plane account metadata are excluded from the archive.

## Remaining acceptance

The common two-target snapshot/reservation HTTP contract, native trigger
creation and matching, feedback, queued account erasure and ledger prompt/mirror
snapshots remain separate required acceptance. The route inventory retains its
blocked classification under the migration completion gate. This record is not
CF-4/CI-1 qualification or production publication.
