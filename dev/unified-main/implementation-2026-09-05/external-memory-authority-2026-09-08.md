# MCP and Developer canonical memory authority

The Cloudflare MCP and Developer memory creators previously wrote `cf_memories`
and usage directly. Those writes could appear in ordinary reads without the
canonical commit, operation, required-processing metadata and head change needed
by normalization and desktop snapshots. Their content editors similarly bypassed
the canonical mutation owner. This change joins the existing intake/mutation
transactions; it does not add a second store or change a default model prompt.

## Original behavior and implementation

The source contracts are `MemoryService.create_external_memory` and
`create_external_memory_batch`, `required_processing_payload`, and
`write_canonical_external_memory` / `_existing_identical_add_row` in the original
backend. The staged `document_id_from_seed` function now supplies exactly the
upstream content-derived ID, including Developer requests. Every new explicit
submission enters pending Short-term with its real source surface and processing
metadata. A category classifier result is not a normalization receipt.

The shared external intake helper distinguishes new records, identical active
records, and privacy-retired identities. Duplicate requests preserve existing
data and tier and create no additional usage or canonical commit, including
after another write has advanced the account head. Explicit resubmission after
privacy deletion receives new memory/evidence identities. A Developer batch
retains its original 25-item limit and response count; repeated inputs may refer
to the same committed record. Existing duplicate records join the observation
guard in migration 0182, so a concurrent source change rolls back the whole
mixed batch. Only known fully rolled-back CAS conflicts permit bounded retries.
Readback validates the original canonical model and owner/source/generation;
record and aggregate JSON limits bound retained duplicate/readback data.

MCP edits use the existing content-correction owner. Developer edits compose
content, visibility, tags and category into one canonical commit. Content changes
retain the original pending-processing, Short-term and graph-clear policy;
metadata-only changes retain tier. Null-only patches now return 422, as the
original Developer route requires. Authentication, scope and paid-lock admission
remain in their original route owners.

Failure-Class: FC-split-mutation-authority

Recent fixes in this subsystem include `b659d92a81` (atomic revisions/projection),
`cf7596dba4` (privacy-receipted creators) and `df1dfb1168` (privacy finalization).
The correction expands the existing behavioral guard in
`test_memory_intake_tier.py` across all five explicit intake routes. It checks
real persistence and rollback through the mounted production handlers and full
D1 migration chain, rather than introducing a source-string checker. The shared
canonical coordinator is the reusable primitive; no new guard framework or
failure-class lifecycle transition is introduced. The registry's existing
upstream incidents include PRs 9365 and 9597.

## Verification

The focused intake/lock/MCP/Developer/apply suite passed 109 tests in 91.60
seconds. After adding canonical readback validation and byte bounds, all 18
intake boundary cases passed in 14.24 seconds. Cases include legacy generation
zero, required-processing admission, unchanged duplicate Long-term data,
combined edits, null rejection, deleted-ID resubmission, mixed-batch source
changes, concurrent identical creates and final usage-write rollback.

The full Workers suite passed 1056 tests in 129 files (20.05 seconds).
TypeScript and manifest validation passed: 654 Cloudflare routes, 619 upstream
routes, zero legacy-owned and 17 routes with unfinished family qualification.
Black leaves all nine changed Python files unchanged.

The first hosted run exercised public signup/session/JWT, Jobs-issued API keys,
canonical writes/edits, duplicates, mixed batches, cross-account and paid-lock
denial, rollback, privacy resubmission and concurrent identical requests. It
then stopped because the verification driver incorrectly expected HTTP 200 for
key deletion. The unchanged key API correctly returns 204. All five Workers,
two databases and one Queue were removed and their absence observed. The second
run reached key revocation and exposed another driver mistake: a revoked API
key returns the established 403, not the assumed 401 for a missing credential.
Both MCP and Developer authentication implementations confirm that contract.
Its resources were also removed with absence observed. Run `20260908-c` corrects
these driver expectations; application authentication behavior is unchanged.

The first complete Core run finished with 1291 passing tests and one obsolete
expectation in the privacy-receipt suite (713.72 seconds). That older test required
an explicit resubmission to fail, whereas upstream `write_canonical_external_memory`
allocates a fresh ID after privacy retirement. The corrected test now verifies
fresh identity/evidence, unchanged retirement receipts, and continued SQL denial
of the original retired ID. All 18 privacy-receipt tests pass (11.51 seconds).
The final complete Core run passes all 1292 tests (704.31 seconds), with one
existing Starlette deprecation warning. No runtime source changed between runs.

Hosted run `20260908-c` passed all 31 public HTTP checks at
2026-09-08T07:44:29.341Z. It used five real Workers, two D1 databases and one
Queue, actual Auth signup/session/JWT and Jobs-issued keys. The selected Qwen
binding was present; the returned category was `system`, which alone does not
establish provider success or classification quality. Runtime default prompts
were unchanged. Immediate observation after deleting Auth briefly still reported
the owned version; a bounded follow-up confirmed all five Workers, both databases
and the Queue absent at 2026-09-08T07:46:39.817Z.

The actual local Wrangler product entry also passes all 49 business cases:
16 common HTTP, 20 recording/finalization/privacy, 11 chat and two share cases.
Application Workers, D1, R2, DO and Queue execute their production owners;
inference and transient Vectorize IO are controlled by the local fixture.

## Remaining delivery work

Integration intake, conversation-derived writes/reprocessing/deletion and the
Jobs X extractor still need canonical writer convergence. The ledger migration
and snapshot producer remains incomplete. This change does not establish queued
provider erasure, model quality, native client acceptance, CF-4 / CI-1 release
qualification or any production Worker deployment.

At 2026-09-08T07:27:19.452Z, direct Cloudflare observation found all nine Eddy
production Workers absent. `release.mjs` checks `pendingQualifiers` before its
first upload; the missing files are `deploy/cloudflare/contracts/qualify-product.mjs`
and `contracts/deployment/qualify-dual-target.mjs`. Deployment authorization is
already present. The missing release implementation is not a Cloudflare access
or D1 transaction failure. The subsequent regression-runner work is tracked separately.

All 50 staged upstream modules and the five changed runtime modules match the
bytes used by hosted run `20260908-c` (SHA-256 comparison). The comparison,
public results, cleanup observation and full Core/local product logs are retained
under `/Users/macstudio/.codex/eddy-production/external-memory-public-20260908/`.

## Commands

From the worktree root:

```bash
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest deploy/cloudflare/python/api-core/tests/test_memory_intake_tier.py deploy/cloudflare/python/api-core/tests/test_memory_mutation_lock.py deploy/cloudflare/python/api-core/tests/test_mcp_routes.py deploy/cloudflare/python/api-core/tests/test_developer_routes.py deploy/cloudflare/python/api-core/tests/test_memory_apply_intake.py -q
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest deploy/cloudflare/python/api-core/tests -q
```

From `deploy/cloudflare`, using installed Node 22:

```bash
node node_modules/vitest/vitest.mjs run
node node_modules/typescript/bin/tsc --noEmit
node scripts/validate-manifests.mjs
```

Hosted drivers and private logs are execution evidence, not replacement release
qualifiers. The expanded hermetic cases run in the existing Core and Worker
local/CI suites.
