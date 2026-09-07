# Cloudflare Candidate lifecycle — local integration

The CF Core runtime now uses the original Candidate model and pure business
policies for creation, semantic coalescing, acceptance, rejection and expiry.
Public `/v1/candidates` handlers are wired locally. Acceptance stores tasks,
workstreams, original workflow events and pending sync work in one guarded D1
batch. Database conflicts roll back the whole batch.

Released `/v1/staged-tasks` routes use the same Candidate owner. New creates and
explicit scoring/accept/delete operations no longer write a parallel candidate
store. Active historical rows remain readable; ordinary reads never materialize
or mutate them. Canonical terminal decisions hide historical duplicates even
when later cleanup needs a retry.

`/v1/task-intelligence/interventions` and `/feedback` retain original models and
idempotency. Later/dismiss feedback saves suppression atomically; retries keep the
original expiry. “Already handled” on a task proposes completion for explicit
acceptance. Export includes these records, and account deletion purges the new
attention table without touching another account.

WMNow now evaluates canonical product state with the original eligibility,
shortlist, material-version and decision rules. Its real returned interventions
feed the same feedback handler: Later hides the recommendation from both lists,
invalidates the cache, and permits a fresh evaluation after expiry. Projection,
interventions, history and job completion commit in one D1 batch. Failed work
keeps a bounded durable retry; no substitute empty recommendation is invented.

The Worker build obtains upstream models and rules through
`deploy/cloudflare/scripts/candidate_kernel_sources.py` and the original WMNow
control flow/message constructor through `recommendation_sources.py`. Default
prompt text is unchanged. The Workers AI adapter defaults to
`@cf/qwen/qwen3.8-27b`. Local tests control the provider response; actual Qwen
inference and hosted runtime acceptance remain unverified for this new path.
Schema 0183 introduces Candidates and guarded task/workstream writes; 0184 adds
attention overrides; 0185 adds recommendation heads and job snapshot checks;
0186 preserves existing device snapshots while adding per-runtime/workstream
scope and durable request receipts. Both snapshot endpoints now accept original
payloads, apply the original one-hour window, reject stale updates, and feed the
same recommendation reader. Expiry and concurrent replacement share a guarded
owner. All four migrations are local drafts. Existing component runners discover the tests:

```sh
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q -p no:cacheprovider deploy/cloudflare/python/api-core/tests
cd deploy/cloudflare
node node_modules/vitest/vitest.mjs run
```

Workflow control remains closed. Outcome attribution, recurrence handoff and
external integration dispatch still need convergence; the integration drain API returns 503. This local work has not been deployed or verified through
Eddy's macOS UI and is not production release qualification. Detailed local
results are in the [Candidate evidence note](../../dev/unified-main/implementation-2026-09-05/canonical-candidates-2026-09-08.md)
[recommendation evidence note](../../dev/unified-main/implementation-2026-09-05/canonical-recommendations-2026-09-08.md),
and [snapshot evidence note](../../dev/unified-main/implementation-2026-09-05/canonical-device-snapshots-2026-09-08.md).
