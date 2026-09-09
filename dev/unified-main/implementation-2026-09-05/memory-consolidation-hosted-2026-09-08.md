# D1 transaction boundary and hosted verification — 2026-09-08

Source revision: `9d89be9bcb5dbf9a4cb351d07015d9923a7c07e7`.

## Transaction contract

[D1 `batch()`](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch)
executes the submitted SQL statements transactionally. A failing statement rolls
back the sequence. The memory adapter gathers candidates and invokes the model
before entering the batch; the batch rechecks account, head, source and read-only
dependency snapshots, then commits domain rows, journal and outbox together.
R2, Vectorize and Queue operations do not share that database transaction.

The read-set fix in migration 0182 repairs application admission: hidden/locked
owner feedback is readable without becoming a writable target. The prior
active/unlocked write guard correctly refused such a target; D1 rolled back as
designed. This was not evidence that D1 lacks transactions or a service outage.

## Direct hosted rollback evidence

Trial B used real Cloudflare D1 with all App migrations through 0182:

- Successful control: two inserts produced two rows, then were removed.
- Fault: two valid inserts followed by a duplicate primary key in one batch.
- The asserted failure was `UNIQUE constraint failed: probe_atomic.uid,
  probe_atomic.value`; the post-failure row count was zero.
- This passed both through a Python Worker `APP_DB.batch()` binding and through
  one D1 HTTP multi-statement request. The HTTP proof was recorded at
  `2026-09-07T15:36:37.701Z`; its fault response was HTTP 400.

## Test setup corrections

Trial A first rejected a source through controlled consolidation, making it
hidden, then attempted the native user review endpoint. It returned 404 because
ordinary native user mutation requires an active target. That is an invalid
fixture ordering, not a transaction failure.

Trial B submitted a native negative review while required processing was pending,
then incorrectly waited for the source to become hidden. Upstream
`canonical_required_processing.is_pending_required_processing` excludes an
explicitly rejected source. The native adapter correctly retained pending state
with `processing_status=processing_rejected`; the dispatcher completed the cycle
without invoking the model for that source. This matches upstream policy and the
existing product-mutation regression. No runtime rule was relaxed.

Both trials observed five owned Workers and five owned resources absent after
cleanup. Trial B completed cleanup at `2026-09-07T15:48:49.988Z`.

The corrected trial C separates native rejection-policy verification from a
synthetic historical feedback fixture: after native rejection and an idle cycle,
it explicitly seeds hidden/locked state in its owned D1 row. This does not verify
a public hide/lock operation. Its consolidation outputs/usage are also synthetic;
BGE-M3 embeddings, Vectorize, D1, native intake, Auth, Cron and Queue are live.
The private Cron wrapper runs the actual memory/vector reconcilers every minute;
unrelated scheduled work is excluded, and the production five-minute schedule
is unchanged. There is no manual consolidation or Queue kickoff.

All selected evidence and private fixture source are retained under
`~/.codex/eddy-production/memory-consolidation-planning-hosted-20260907/`.
Credentials and raw private command logs are excluded. Hosted fixtures are not
CF-4/CI-1 qualification, real Qwen generation evidence, or Eddy production delivery.

## Corrected hosted batch result

Trial C passed the eight-source Unicode batch at `2026-09-07T16:01:15.685Z`:
three model payloads contained 3, 3 and 2 sources, respectively, measuring
108,695, 108,694 and 76,044 UTF-8 bytes. Every source appeared exactly once;
all selected context content hashes, hidden owner feedback and default system
prompt hash matched. The attempt table was empty after the cycle.
At `2026-09-07T16:07:49.716Z`, the complete trial passed:

- Seven additional native sources became Long-term memories automatically;
  the final healthy source retrieved all eight expected candidates.
- One oversized source spent exactly three bounded `input_too_large` attempts,
  reached the original Archive/review route and made zero consolidation-model
  calls. The healthy later source completed through one synthetic model call.
- The public review queue contained exactly the oversized source; another
  authenticated account's review queue was empty. Default reads returned exactly
  the eight Long-term seeds. Apply guards and the durable DLQ were empty.
- Nine actual Cron events and 33 memory Queue deliveries were observed. Every
  memory delivery had `attempts=1`; application continuation did not consume
  transport retry attempts. Index-visibility waits retained a zero failure count.
- The Worker binding repeated the strict duplicate-key batch rollback proof.
  Frozen consolidation modules and migration 0182 matched source revision above.
- All five owned Workers and five owned resources were observed absent. Cleanup
  finished at `2026-09-07T16:08:21.000Z` (2026-09-08 00:08:21 Asia/Shanghai).

The current production source was unchanged during this hosted trial. Only the
private provider, historical-state fixture and scoped Cron wrapper differed.
The full local Core/Workers suites remain the previously recorded 1024/1006
passes; they were not rerun for this documentation-only follow-up.

Commands/evidence: the retained `c/driver.mjs` ran under Node 22 against owned
Cloudflare resources and exited 0; `c/result.json` records
`automatic_cycle_passed=true` and `release_qualified=false`, and
`c/automatic-events.json` contains the raw synthetic observation events.
`b/atomic-api-proof.json` is the separate strict D1 HTTP rollback result.
`git diff --check` and HTML parsing/link checks passed for the documentation.
No production resources, default prompts, application model settings or macOS
bundles were changed; no push, PR or merge occurred.
