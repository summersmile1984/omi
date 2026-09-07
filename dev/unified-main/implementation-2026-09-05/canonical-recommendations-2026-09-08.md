# Original WMNow evaluation on D1 — local verification, 2026-09-08

Continuation: [device snapshot integration](canonical-device-snapshots-2026-09-08.md)
now closes the snapshot boundary listed below; these earlier results retain
their original source identity.

Status: uncommitted local integration on `codex/unified-delivery`, based on
`eefe1d6a62`. Not deployed; no hosted Qwen or macOS acceptance claim. This follows
[canonical Candidate HTTP integration](canonical-candidates-2026-09-08.md).

## Business boundary

The former Cloudflare evaluator read the retired candidate table, used a different
shortlist and abbreviated model instructions, and did not publish attributable
interventions. The ordinary source projector now preserves the upstream
`recommendations.evaluate` / `get_debug_projection` control flow, facts,
eligibility, tier balancing, material hashes and original schemas. Only
persistence/provider calls become async. The default system/user message
constructor is projected verbatim from `live_recommendation_judgment.py`; a
behavioral test executes that original constructor with a controlled parser and
compares its actual messages. No upstream prompt file is modified.

D1 supplies the bounded canonical tasks, goals, workstreams, Candidates,
artifacts and events. Active suppression participates in the original material
identity. A recommendation's published intervention is accepted by the original
feedback policy, which changes Suggested and WMNow together. Expired suppression
allows reevaluation without making an old completed job block the request.

The job's execution identity includes the observed head as well as material
identity. The physical unique request fingerprint follows that execution identity;
otherwise a return to the same material would collide even after job IDs changed.
Retries against one execution keep the three-attempt budget and 300-second lease.
An expired third lease becomes failed without a fourth inference. Existing Queue
and Cron processing rehydrate current canonical state. Old fixed-input jobs are
marked terminal instead of being replayed by the retired evaluator.

Schema 0185 extends the existing exact-snapshot transaction to recommendation
heads and jobs. Publication stores the head, original decision history,
interventions, successful model receipt, usage and job completion together.
A late intervention failure rolls all of those back and retains retryable work.
The table joins account deletion inventory and purge; its UPDATE fence checks
both OLD and NEW owners, including writes admitted before deletion began.

The new adapter defaults to `@cf/qwen/qwen3.8-27b`, using unchanged upstream
messages plus the original structured-output schema. The model-version StableId
stores a digest because raw provider IDs contain forbidden characters; receipts
retain the exact model. Response and usage are validated before publication.
[Qwen's official contract](https://developers.cloudflare.com/workers-ai/models/qwen3.8-27b/)
was checked; actual hosted schema acceptance and generation quality are still
unverified for this new path. Published usage does not account for a billable
provider success whose subsequent publication transaction failed.

## Verification scope

The tests execute public FastAPI handlers, original projected rules and all App
migration SQL through local SQLite. The AI response/usage and clock are controlled;
this is not hosted D1 or live Qwen evidence. They cover generated feedback/cache
invalidation and expiry, ineligible/empty inputs, rollback, internal queue recovery,
third-attempt crash recovery, generation changes, task/workstream/artifact subjects,
owner isolation, and both sides of the account-deletion UPDATE boundary.

Commands and outcomes:

```sh
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests/test_recommendation_routes.py
cd deploy/cloudflare
node node_modules/vitest/vitest.mjs run
node node_modules/typescript/bin/tsc --noEmit
node scripts/validate-manifests.mjs
node scripts/python-worker.mjs api-core deploy --dry-run --outdir /private/tmp/eddy-recommendation-core-bundle-20260908
```

- Initial recommendation/Candidate/staged HTTP selection: **27 passed, 15.98s**.
- Core full suite: **1,090 passed, one existing Starlette/AnyIO deprecation,
  436.13s**. It collected before the last two deletion-fence cases were added;
  the final recommendation module separately passed all **10 cases, 5.94s**.
- The first Workers run exposed the new deletion trigger's missing shared
  declaration shape; review also found its missing OLD-owner fence. After fixing
  both, focused deletion tests passed **22/22**, and the final complete Workers
  suite passed **1,006 tests across 127 files, 14.87s**. No test was weakened.
- Typecheck and manifest validation exited zero: 654 CF routes, 619 backend
  routes, 17 tracked blocked routes and 30 staging resources.
- Pinned Python formatting and `git diff --check` passed. The dry-run build
  exited zero with upload 14,682.79 KiB / gzip 3,820.32 KiB. Seventeen runtime
  files match the worktree byte-for-byte; 29 runtime/projected modules are hashed.
  This validates packaging, not actual Python Worker execution.
- Four protected prompt files match `9b7e48dca2`; the original attention
  message-constructor source matches HEAD, and its behavioral parity test passes.

Source identities, deleted-file inventory and logs are retained separately at
`~/.codex/eddy-production/canonical-recommendations-local-20260908/` with
`release_qualified=false` and `hosted_verified=false`. The previous Candidate
evidence directories remain unchanged.

## Remaining work

Workflow control remains closed. Device context/outcome writers and the old
one-open-loop-per-device schema are not yet the complete original snapshot
lifecycle. Recurrence receipt/drain ownership and external integration execution
remain unfinished; the integration drain API remains unavailable. Schemas
0183–0185 are not applied remotely. Hosted runtime/model verification, CF-4/CI-1
qualification and Eddy macOS-to-production acceptance are still required.
