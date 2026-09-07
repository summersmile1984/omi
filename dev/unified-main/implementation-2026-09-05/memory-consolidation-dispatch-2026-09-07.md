# Cloudflare automatic consolidation dispatch — 2026-09-07

## Contract

The preceding hosted retry trial needed the private driver to invoke every batch.
This change connects the actual account scan to the existing Jobs cron, Queue
consumer, signed Core boundary and original leased batch runner. It is one part
of Eddy qualification, not a claim that the full product can be released.

Migration 0181 atomically marks memory writes dirty and seeds existing accounts.
D1 stores the cycle's starting sequence, original captured-time/ID cursor, retry
due time and account lease. Each invocation reads at most 21 IDs and hydrates at
most 20 sources. Completing an earlier cycle cannot acknowledge backdated intake
that arrived behind its cursor. Only a complete unblocked cycle acknowledges the
captured sequence; the original maintenance watermark also requires an apply.
An expired/replaced account lease cannot commit cursor or watermark changes.
Legacy controls use the existing genesis constructor without invented commits.

Jobs discovers due accounts from D1 on the existing five-minute cron. Its signed
Core request binds UID, method, path and audience; the body contains only account
generation. Normal continuation sends a fresh delayed Queue message before ack,
rather than spending Queue retries on every page. A lost send remains recoverable
through the delivery and durable cron discovery. Independent memory calls overlap
within the configured ten-message batch; other job kinds remain sequential. The
existing DLQ parser/replay owner now recognizes the memory job kind.

Malformed source or retry-state data is retained and prevents a successful cycle
watermark, while healthy sources continue. Database outages do not acknowledge
scan progress. Source attempts and model-failure counts remain with the original
bounded retry/terminal-review owner; Queue deliveries cannot reset their budget.
No upstream source, default prompt or model selection changed.

## Local verification

- Core: **1,007 passed**, one existing Starlette warning, 319.94 seconds.
- API-AI: **151 passed**, 8.89 seconds.
- Focused dispatch/retry: **20 passed**, 10.69 seconds.
- Workers: **1,006 passed**, 127 files, 12.38 seconds.
- Six new Jobs cases execute actual SQLite migration SQL and the production
  consumer/reconciler, with controlled Queue/Core service boundaries.
- Nine new Core cases execute native HTTP intake, actual migration/transaction
  SQL, original context/parser/apply and the actual source-attempt runner. Only
  provider output and clock are controlled. Cases cover multi-page progression,
  backdated intake during a cycle, malformed rows, overlapping account deliveries,
  lease expiry/replacement, failed final acknowledgement, legacy genesis and
  the signed internal request boundary.
- TypeScript typecheck, manifests and pinned Python formatting pass.

The initial tests had fixture defects: missing isolated component import path,
invalid corruption that violated an existing SQL constraint, a corruption of an
ignored field, and a DLQ name outside the existing infrastructure naming contract.
Those failures are not claimed as production regressions. A malformed canonical
source did reproduce whole-batch failure before correction; the independent-account
Queue test timed out under the prior sequential consumer and passed after the fix.
After correcting the fixtures, a test-only replay of the previous committed
runner makes the malformed-retry case fail at the original schema validation.
Removing the new DLQ registration through a test-only transform makes the actual
Queue/SQL test persist `invalid` instead of `captured`. The initial runner replay
replaced the module symbol but not the dispatcher's imported function; that
passing run was not treated as regression evidence.
The first full Worker run also exposed an old asset-cleanup fake that returned
asset rows for unrelated SQL. It now returns those rows only for its owning table;
its original behavioral assertions are unchanged.

Tests remain in the existing `fork-cloudflare-routes` local/CI/deploy lane. No
on-demand repository verifier, runtime storage facade or new rollout cohort was
introduced. Logs and hosted evidence are retained privately under
`$CODEX_HOME/eddy-production/memory-consolidation-dispatch-20260907/`.

## Hosted verification and remaining scope

The first isolated hosted Cron/Queue trial was interrupted after an observation
fixture was found to change execution. The corrected fresh B trial passed. Its driver submits native
intake and reads results; it does not invoke consolidation or publish vectors by
hand. The private provider seam replays the retained invalid Qwen answer, changing
only source/evidence IDs, and forwards embedding requests to actual Workers AI.
This can prove automatic recovery, not fresh model semantic quality. The result and cleanup observations are recorded below.

Provider-window batch sizing and recurrence-to-workflow handoff remain incomplete.
The current input byte bound defers oversized contexts; it is not a complete batch
planner. Recurrence-bearing batches still fail before writes. Other canonical
writer/read convergence and full production qualification remain outstanding.

### Hosted A: observation interference, preserved as a failed trial

Actual Cron discovered the native memory and actual Queue deliveries reached the
signed Core route. The private fixture had added AFTER INSERT/UPDATE triggers to
record every source-attempt transition. On real D1, those trigger writes increase
`meta.changes`: a minimal isolated counter returned 1 without the trigger and 2
with it. The unchanged source-attempt owner consequently treated its successful
claim as not claimed. No model response replay occurred in this trial.

The event table was copied, then renamed deliberately to interrupt the driver's
read-only wait and enter its existing cleanup. Both Queue consumers were removed;
all five Workers, both Queues, both D1 databases and the Vectorize index were
observed absent. Cleanup completed at `2026-09-07T14:16:22.045Z`.

The B fixture removes all attempt-observation triggers. Its middleware observes
retry state only after the real Core response; it does not change the business
write count. The production source files are unchanged between A and B. The
minimal counter, interrupted event history and original frozen A payload remain
in `/private/tmp/eddy-dispatch-live-hosted-20260907-a/`.

### Hosted B: automatic discovery, delivery and failure recovery passed

Five fresh Workers, two D1 databases through migration 0181, one Vectorize index
with the publication filter, and two actual Queues were used. The temporary Jobs
entry uses the production Queue consumer and the production memory/vector
reconcilers. Its actual Cron interval is one minute to bound the trial; the
production configuration remains five minutes. Unrelated Jobs schedules are not
qualified by this isolated entry. The driver never invokes consolidation or
vector publication; after native intake it only reads results.

- Native intake at `14:21:29Z` became Long-term revision 3 at `14:26:03Z` after
  actual Cron discovery and Queue/Core processing.
- Duplicate intake deferred while the index changed; at `14:27:03Z` its durable
  attempt count remained zero. The retained invalid duplicate-create response
  then produced counts 1 and 2, followed by original terminal review at count 3
  (`14:28:08Z`). Each inference saw the actual existing candidate above score 0.9.
- The scan became idle with `wake_sequence=handled_sequence=12` and no account
  lease at `14:28:19Z`. Nine consolidation deliveries each reported Queue attempt
  1; normal continuation did not consume delivery retries. Three Cron events
  were recorded, and all observed Core processor responses were HTTP 200.
- Public review exposed only the duplicate's committed source revision. Default
  reads retained only the original preference; the other authenticated account
  saw no review. Apply guards and DLQ records were empty.
- Exactly four provider responses were replayed: one seed and three duplicate
  failures. No new Qwen sampling was performed. Every request used
  `@cf/qwen/qwen3.8-27b` and the unchanged 10,491-byte original system prefix,
  SHA-256 `be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa`.
  Embedding, retrieval, Queue, Cron, Auth, Core and D1 were real hosted services.
- Both Queue consumers were removed; all five Workers and five data resources
  were observed absent. The trial finished at `2026-09-07T14:28:56.111Z`.
  Result journal SHA-256:
  `4459ea7b8f484386ea649240866bc007334c9c1382ec3b7de68b2f0e441dd60a`.

This is automatic-recovery evidence with a retained provider failure. It does
not qualify fresh Qwen semantic reliability, all Jobs schedules, remaining
workflow integration, the complete production release or macOS connectivity.
