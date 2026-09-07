# Canonical Candidate persistence draft — 2026-09-08

Continuation: the original WMNow engine is now wired locally; see the
[separate recommendation evidence](canonical-recommendations-2026-09-08.md).
The results below retain their earlier scope and source snapshots.

Status: local, uncommitted integration work. Public Candidate and released staged
routes are now wired to the canonical D1 owner. Workflow control remains closed;
WMNow convergence, recurrence and external integration drain remain incomplete.
This is not production delivery or hosted/macOS acceptance.

## Storage and decisions

The ordinary kernel projector stages upstream Candidate models, identity hashes,
semantic coalescing, and pure task/workstream storage policies. Creation saves the
record, idempotency alias and semantic claim in one D1 batch. The transaction
compares exact observed Candidate/alias/claim/integration JSON and physical task,
goal and workstream rows, plus account generation and deletion fences. An account
without control metadata uses upstream generation zero. Snapshot changes cause a
bounded complete reread/replan. Accepted active tasks can absorb new evidence and
confidence without another Candidate.

Task-change JSON preserves field presence: `due_at: null` clears a date; omission
leaves it unchanged. Storage retains the original payload's set fields. Aliases
compare those field names as well as the original value hash. No default prompts
or upstream source files change.

Reject/expire retains the first receipt, reason and timestamp on replay. A
competing terminal decision conflicts after rereading the winner. Replays still
pass account/deletion and row guards. These atomic D1 writers have no intermediate
resolution lease; historical staged records are not implicitly adopted.

Acceptance now handles task create/update/complete/cancel/supersede and workstream
create using the original pure storage builders. A workstream creates its anchor
task and original first system event. Acceptance, task/workstream/event records,
pending integration outbox and vector projection outbox commit together. A late
outbox failure rolls all of them back. The existing vector reconciler can consume
its outbox; the new integration outbox still needs its real drainer. No external
integration delivery is asserted by a pending row.

Final relationship checks match `backend/database/action_items.py` and
`backend/database/workstreams.py`: owner-scoped existence, exact goal/workstream
generation, matching goal/workstream links, and ended-goal admission. An unchanged
task relationship may remain under an ended goal when finishing existing work.
Concurrent changes to observed goals/workstreams abort and replan. Task physical
update/index timestamps advance by at least one second for multiple changes in a
second, as in the existing D1 task writer; Candidate receipt timestamps stay fixed.

## Goal creation, export and deletion

Both native goal create paths now persist the current account generation. The
canonical path checks the supplied generation before replay and again with the
creation receipt inside its D1 batch. The ordinary path resolves the generation
inside its insert. `backend/database/goals.py` owns the expected generation rule;
the old canonical-create fixture now seeds the generation-3 account it claims to
use, without changing its assertions. The new end-to-end local test creates a
goal through each original route, then accepts a workstream Candidate under it.
A generation race rolls back both the goal and its creation receipt. Other goal
mutation routes were not migrated by this slice.

User export returns owned canonical Candidate records directly in the existing
`task_data.candidates` field; aliases, claims and operational outboxes are not
exported. The existing Jobs deletion inventory/purge includes all five new owner
and guard tables from 0183 (plus attention overrides from 0184). Its real local
workflow test seeds records for two accounts,
deletes one through the ordinary job loop and proves the other account retains
its records. New tables use the shared INSERT/UPDATE account-deletion fences.

## Verification and unfinished integration

Focused commands use the existing component runners:

```sh
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests/test_candidate_create.py deploy/cloudflare/python/api-core/tests/test_candidate_resolve.py deploy/cloudflare/python/api-core/tests/test_candidate_accept.py deploy/cloudflare/python/api-core/tests/test_user_export_routes.py
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider deploy/cloudflare/python/api-core/tests/test_candidate_accept.py deploy/cloudflare/python/api-core/tests/test_goal_routes.py
cd deploy/cloudflare
node node_modules/vitest/vitest.mjs run tests/account-deletion.test.ts tests/account-deletion-residual.test.ts
node node_modules/typescript/bin/tsc --noEmit
```

- Initial create/terminal tests: 21 passed; acceptance tests: 19 passed.
- Candidate plus export tests before the goal addition: **46 passed, 25.25s**.
- Candidate/goal route tests after the goal addition: **30 passed, 16.35s**.
- Account deletion: **22 passed, 7.95s** after adding the shared deletion fences.
- Terminal-decision deletion-fence replay: **12 passed, 6.79s**. Each new test
  module can run independently; imports do not rely on another test's setup.
- Typecheck and manifests passed. Manifest ownership remains 654 CF routes / 619
  backend routes with 17 tracked blocked routes; it is not product qualification.
- Full Core: **1068 passed, 384.15s**, one existing Starlette/AnyIO deprecation
  warning. This run finished before the final, semantics-preserving parentheses
  correction to migration 0183; no production Python changed afterwards.
- Final full Workers: **1006 passed, 127 files, 13.80s**, including installed
  Wrangler statement splitting, migration execution and the transport tripwire.
- Final Candidate/goal/export tests with the corrected migration: **56 passed,
  33.10s**. `git diff --check` and pinned Python formatting passed.
- Four protected default-prompt files remain identical to `9b7e48dca2`.

Full runs used `pytest ... deploy/cloudflare/python/api-core/tests` and
`node node_modules/vitest/vitest.mjs run`. Final logs and source hashes are retained
under `~/.codex/eddy-production/canonical-candidates-local-20260908/`. These are
local regression results from before public Candidate handlers were connected;
these hashes do not describe the later HTTP/attention/staged changes.

Tests execute the complete App migration SQL in local SQLite. The competing
terminal resolver generates its real production SQL, which is committed at the
fake D1's before-write seam. Auth/provider fixtures remain controlled. No result
here proves remote D1, public Candidate UI acceptance or actual integration sync.
The existing D1 migration transport tripwire caught unparenthesized `SELECT CASE`
in the draft; the final migration uses `SELECT (CASE ... END)`, following the
already-recorded remote migration failure in the consolidation retry work.

Still required: universal control; migration of the remaining old writers and
WMNow evaluation input/ranking, including real producer-to-feedback suppression;
recurrence receipt/drain ownership; integration outbox execution/retry ownership;
hosted/runtime and release qualification. The consolidation recurrence guard
remains until the complete handoff exists. No migration was applied remotely and
no commit, push, PR, merge or production deployment occurred for this draft.


## Public HTTP, attention and staged continuation

The closed Candidate compatibility router was removed. The entry point now loads
actual list/get/create/accept/reject/expire handlers with original models, account
generation checks and idempotency. Suggested projects original confidence and
semantic-equivalence policy. A same-second list keeps microsecond creation order.
The integration-drain route deliberately remains a 503, not a fake queue success.

Intervention and feedback handlers use the upstream wire models and stable IDs
(including intervention surface). Feedback and attention suppression persist in
one guarded batch. A replay retains the first expiry; two independent idempotency
keys with identical bodies retain separate occurrences. For physical schema 0109,
the unique fingerprint binds original request hash to record ID; the original
request hash is retained as private payload metadata. Candidate rejection and
already-handled task completion proposals follow the original service: a task
is completed only after explicit acceptance of the generated completion Candidate.
The active override scan keeps the indexed epoch bound, then compares the original
precise timestamp; it does not release suppression early within the expiry second.

Export preserves owned old request-only feedback payloads and current records;
internal request/index hashes and retry expiry metadata are excluded. Migration
0184's attention table participates in both the purge inventory and residual scan.
The deletion regression seeds both accounts and proves the other account survives.

Released staged routes no longer write new flat `cf_task_candidates` rows or
create action items directly. Upstream selected-node projection supplies input
models, read projection, score order and legacy proposal policy. Ordinary reads
merge active history with canonical precedence without adopting it. Explicit
scores materialize only their referenced historical row. Accept/delete/clear use
canonical decisions; cleanup runs afterwards under the generation guard. A
cleanup failure leaves the canonical receipt authoritative, hides the historical
row in subsequent reads and supports a retry without another task/outbox.
Historical already-accepted flat rows remain accessible as tasks; they are not
new staged intake. The old hand-coded query mock tests were replaced by HTTP
against the entire App migration chain. The workstream-detail test was corrected
to the envelope declared by `backend/models/workstream.py:WorkstreamDetailProjection`.

Focused results in this continuation:
- HTTP/feedback/export plus existing intelligence/queue checks: 22 passed, 11.35s.
- Attention deletion/residual workflow: 22 passed, 8.32s.
- Staged/HTTP/device boundary: 20 passed, 11.16s.

These tests use controlled Auth and local SQLite, not deployed Workers or the
macOS UI. They register WMNow-shaped interventions directly and do not prove the
existing WMNow evaluator produces canonical IDs or consumes suppression. That
producer still reads its earlier historical table and needs migration. Default
prompts are unchanged. No migration/deployment, commit, push, PR or merge occurred
in this continuation.


Final continuation gates against the formatted runtime source:

- Core full suite: **1083 passed, 1 existing Starlette/AnyIO warning, 411.83s**.
- Workers full suite: **1006 passed, 127 files, 14.26s**, including migration
  transport and deletion inventory checks.
- TypeScript typecheck, manifest validation, pinned Python format check and
  `git diff --check` passed. Manifest counts remain 654 CF / 619 backend routes,
  with 17 tracked blocked routes; ownership counts are not product qualification.
- Fixed builder command:
  `node scripts/python-worker.mjs api-core deploy --dry-run --outdir /private/tmp/eddy-candidate-core-bundle-20260908`.
  It exited 0, reporting 14629.92 KiB upload / 3811.05 KiB gzip. Eleven bundled
  runtime files are byte-identical to the worktree, and five projected Candidate
  model/policy modules are present. This is packaging evidence, not Worker startup
  or deployed HTTP evidence. The build did not change dependency lockfiles.
- Four protected default-prompt files remain byte-identical to `9b7e48dca2`.

New source snapshots, deletion manifest, test logs and non-secret bundle identity
are retained separately at
`~/.codex/eddy-production/canonical-candidates-http-local-20260908/` with SHA-256
checksums. The earlier `canonical-candidates-local-20260908` snapshot is unchanged.

At this snapshot, the next WMNow boundary was concrete: upstream `recommendations.evaluate` loads
canonical product state, includes the active override set in material version,
and publishes projections with their intervention records. The current CF
`_evaluate` reads historical candidates, hashes IDs without the new suppression
set and does not persist the returned interventions. These must converge together
using original eligibility, subject/ranking and identity policy; changing only a
feedback consumer or enabling workflow control would not close that boundary.
