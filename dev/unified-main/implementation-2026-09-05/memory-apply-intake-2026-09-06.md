# Cloudflare native memory intake transaction — 2026-09-06

Native single and batch POSTs now use `memory_apply_intake.py` to persist the
staged upstream apply engine's result in one guarded App D1 batch. The account
and source generation, current control JSON, deletion state and target identity
are rechecked inside that transaction. It writes the existing memory rows,
operation receipts, commit chain, control head and pending projection/vector
events together with usage and review work. Failure rolls back the complete
batch. An exact whole internal retry does not repeat any side effect; public
POST still allocates fresh server IDs and has no client idempotency contract.

Migration 0172 adds the journal and transient admission guard. `cf_memories`
remains the physical item authority; content, evidence, lifecycle, revision and
privacy fields are not duplicated in a shadow document. Only upstream model
fields without existing physical columns use `canonical_metadata_json`.
Original Short-term rules and TTL are imported from the upstream kernel.

The owner's export includes operation and commit records in
`memory_ledger_data`, matching the upstream portability section. Internal
delivery outboxes and control leases remain excluded. The five new tables are
registered with account erasure and have INSERT/UPDATE deletion fences. The
existing deletion workflow regression seeds both a deleted account and another
account, exercises the actual erasure owner, and checks removal/isolation.

## Verification scope

`test_memory_apply_intake.py` executes the real native handlers and full App
migrations. It verifies the item/receipt/commit/outbox relationship, Short-term
intake, an unmigrated principal's generation zero, exact replay, changed or
mixed replay, UID isolation, concurrent control/source/account changes, early
and late deletion fences, late receipt/outbox/usage failures, target collision,
removed-source replay, stale-generation rejection and populated owner export.

The existing memory and review fixtures now execute the complete migration
chain. The review fixture also fails at unsuccessful setup creation rather than
misreporting the later empty queue. Existing review behavior expectations remain
unchanged. The original batch tests retained the 100-item, 8 MB request and
50,000-character content contracts. On 2026-09-07 the user requested a smaller
Cloudflare batch limit after reviewing the native memory failure. The current
limit is 1,000,000 UTF-8 request bytes; 100 items and 50,000 characters per item
remain separate limits. Both the actual request body and its JSON envelope
count toward the byte limit. Edge enforces it before the Python ASGI bridge;
Core independently checks it. HTTP 413 reports both limits and writes nothing.
The boundary tests exercise exact-limit ASCII, Chinese and emoji, a one-byte
overflow, chunk boundaries splitting UTF-8, and a missing or dishonest length
header. This expected value comes from the user-directed limit change, not a
claim that the original 8 MB envelope passed.

External contracts for these expectations are Cloudflare's
[D1 batch transaction and rollback guarantee](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch)
and [2,000,000-byte string/blob/row limit](https://developers.cloudflare.com/d1/platform/limits/).
All tests run in the existing `bash deploy/cloudflare/ci/routes.sh` local/CI
lane; no network-dependent test was added to that lane.

## Native deployment findings

The first owned hosted run applied the previous App migrations, then failed on
0172 with `incomplete input: SQLITE_ERROR` (7500). SQLite had accepted the same
SQL. Parenthesizing the CASE expressions fixes the remote trigger parser's
known [CASE/END ambiguity](https://github.com/cloudflare/workers-sdk/issues/4727).
The existing labeled portability tripwire now covers both the prior 0168 frame
incident and this 0172 recurrence; it is not presented as behavioral SQL
coverage. No already-deployed production migration was rewritten.

The second owned run applied all 175 App migrations and completed actual Auth,
Edge and Python Core deployment and single-memory POST. Its probe then stopped
because it incorrectly expected account generation zero for a newly
bootstrapped account. `account_cutover_routes._initialize_isolated_account`
correctly initializes that account at generation one. The next probe compares
the memory control generation with the authoritative cutover row, while the
hermetic tests retain explicit coverage for unmigrated generation zero.

The complete `bash deploy/cloudflare/ci/routes.sh` lane passed inventories,
manifest validation, typecheck, **981 Worker tests in 123 files, 793 Core tests
and 150 AI tests**. Core reports the existing Starlette/AnyIO deprecation warning.
That log is `memory-apply-intake-routes-transport-20260906.log` in the private
operator evidence directory.

Hosted run C passed owner-scoped export, all three injected receipt/outbox/usage
rollback failures, and 100 small memories. Its accepted 100 × 50,000-character
ASCII request returned HTTP 500. That run cleaned up before reconciling whether
the failed response followed a commit, so it does not prove write failure or
qualify the long-text envelope. Run D captures both Edge and Core tails and
reconciles D1 counts before cleanup. Production remains unqualified while this
failure is investigated. Run D confirmed `exceededMemory` in API Core and the
matching Edge error. Post-failure counts remained at the prior 101 items,
101 operations, 101 commits, 202 kernel events and zero admission guards: the
large batch did not commit.

The native parser now consumes bounded request chunks without keeping a second
cached request body during apply. Usage retains only ID bindings, and completed
apply record lists are released before D1 transport. The same local 100 ×
50,000-character HTTP request still returns all 100 memories; Python tracemalloc
peak fell from 40.32 MiB to 30.23 MiB. This is Python allocation evidence, not
total Worker memory. Run E still exceeded the Worker limit before committing.
Run F adds private stage-only wrappers for diagnosis; its modified entrypoint
must not be represented as qualification of an unchanged release payload.
Its final log reached item-binding preparation and failed while preparing the
100 operation records. The next implementation therefore transports the intake
text once: the operation INSERT reconstructs `logical_payload.memory_text` from
the same transaction's uid-scoped item. Persisted operations remain complete
and validate against the unchanged upstream operation-ID/digest rules. A
missing owned item fails the entire batch. Local peak for the same HTTP request
fell to 20.82 MiB without changing response bytes or committed item count.

## Subsequent hosted findings and smaller request contract

Runs G–J passed the 100 × 50,000-character ASCII request (5,003,514 bytes).
The latest unchanged Core payload in run J passed single intake, owner export,
all three injected transaction failures, 100 small items and the large ASCII
batch. Persisted operation text matched its same-transaction owned memory for
every committed item. Per-item materialization retains only operation IDs and
digests for the initial lookup, instead of the full set of typed operations.

Run J still failed the approximately 8 MB Chinese request with HTTP 500.
Its Core tail recorded `exceededMemory`. Reconciliation found the prior 201
items/operations/commits, 402 events and zero guards: that failed batch did
not commit. The emoji case was not reached. Run H's earlier 502 lacks a Core
outcome and is not labeled as a proven D1 timeout or memory failure.
All ten owned runs A–J finished and their created Workers/D1 resources were
observed absent after cleanup. Their journals remain in the private operator
folder; they are not production deployments.

The smaller request contract is a deliberate user-directed product limit,
not an assertion that the 8 MB memory issue was repaired. Larger imports must
be divided into independent requests. Current macOS and Electron native batch
callers primarily split by item count; their byte-aware batching remains client
integration work. No content is silently truncated and a rejected request is
not partially committed. No automatic replay of successful earlier batches is
introduced.

## Hosted 1 MB acceptance — 2026-09-07

The owned `memory-apply-hosted-20260907-a` run deployed fresh Auth, Rate Limit,
Core and Edge with two isolated D1 databases (all 10 Auth and 175 App migrations).
It finished successfully at `2026-09-07T02:52:50.918Z`. Every owned Worker and
both databases were observed absent after cleanup. The journal SHA-256 is
`7bd3ad8b640d9009a21fa01ae0e57b7368ce5c928d6b47fa08ff1379d729281f`.

- Seven batches each sent exactly 1,000,000 wire bytes: 100 short items; 100
  long ASCII, Chinese and emoji items; then 19 ASCII, six Chinese and four
  emoji items with 50,000 characters per item. JSON trailing whitespace fills
  each remaining byte budget. All returned HTTP 200 and the complete unchanged
  content, matching item/operation/commit counts and zero receipt-text mismatch.
- A 1,000,001-byte request and the previous 7,950,809-byte Chinese request
  returned HTTP 413 and the documented limits. All database counts remained
  unchanged. The captured Core tail contains 29 `ok` events and no memory error.
- Actual single intake, owner-scoped populated export and all three injected
  receipt/outbox/usage transaction failures passed again.
- Core's three changed production modules match the frozen hosted modules
  byte for byte. Edge was subsequently restored to its original surrounding
  formatting to avoid unrelated diff churn. A fresh ordinary dry-run build
  differs only in whitespace: both bundles are identical after installed
  esbuild whitespace minification (`edge-source-comparison.json`). A future
  release still requires a new full candidate identity.

The current complete route lane passed inventory, manifest validation,
typecheck, 986 Worker tests in 124 files, 796 Core tests and 150 AI tests.
Command: `env PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh`;
private log: `memory-apply-intake-routes-1mb-b-20260907.log`. The first run stopped
at a TypeScript fixture type error; the corrected run completed, and the
existing Starlette/AnyIO warning remains. Focused native memory tests passed
32 cases; focused Edge tests passed 144 cases. These tests run in the existing
local/CI lanes; the live probe itself is operator evidence, not a network CI test.

## Remaining production boundary

This is native intake persistence, not complete canonical writer convergence.
MCP/developer/integration/conversation intake, edits/review, source deletion and
privacy, consolidation/promotion and graph assertion writes must still use the
same apply boundary. The existing vector publication outbox drives actual Jobs
work; the new kernel events remain pending and do not establish a delivered
projection watermark. Append-only lineage, history/revert and JIT snapshots
remain unqualified. Inventory remains 602 staging-owned / 17 blocked.

The 2026-09-07T02:52:23.128Z production observation found all nine intended Eddy
Workers absent. The local Eddy.app passed strict signature verification; live
production login/capture/memory/task/export remains unverified. No default
prompt changed: the four protected prompt/provider files still match
`9b7e48dca2` byte-for-byte. These results do not authorize claiming a completed
production deployment.
