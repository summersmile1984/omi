# Native memory product mutations — 2026-09-07

Native visibility, review votes, read/dismiss and baseline writes now share the
canonical user-mutation owner with content correction. Each commits its item,
operation receipt, commit/control head and outbox together. Review feedback is
in the same transaction, immediately after the item UPDATE. Existing request
and success response shapes, paid locks and owner isolation remain in place.
A target removed concurrently returns 503 without a new feedback/history entry,
instead of acknowledging the old zero-row update.

## Implementation

`memory_apply_mutation.py` expresses the original upstream
`_apply_canonical_user_mutation` identity and patch flow against the existing
D1 item/head admission guard. `memory_apply_item.py` owns the shared historical
projection; all former imports were migrated and no compatibility alias remains.
Content correction delegates to this owner. Product policy builders preserve
content and tier, retain other physical product metadata during adoption, and
follow the upstream default-off belief policy. A positive vote does not grant
promotion or processor admission. Required pending processing moves between
`processing_rejected` and `pending_processing` when reviewed, as upstream defines.

Migration 0177 persists the original `MemoryGraphAssertion` emitted when an
ordinary user mutation refreshes a graph-backed Long-term item. The current
transaction guard must admit the exact uid, memory and account generation.
The assertion must match its new item revision, content hash, graph plan and
commit. Item changes revoke the preceding snapshot; its replacement is inserted
within the item transaction, so a graph failure restores both prior states.
The table participates in owner export and account erasure; privacy preparation
also retracts it. It does not introduce a public promotion path or replace
shared graph-query/consolidation work that remains unfinished.

After commit the existing Queue hint wakes vector projection. Queue failure
retains the authoritative outbox for scheduled reconciliation. No new service
binding, secret, default prompt or Server OS implementation was introduced.

## Local verification

Private evidence lives under `$CODEX_HOME/eddy-production/` with the
`memory-product-mutation-` prefix. These tests are discovered by existing lanes.

- `PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh` passed
  inventory, manifest validation, typecheck and **995 Worker tests**. Its Core
  stage found one obsolete assertion expecting 200 after concurrent deletion.
  The revised assertion requires rejection and retains explicit proof that no
  feedback, operation, commit or control update was written.
- The final `uvx uv==0.12.3 run pytest -q --tb=short` in api-core passed **900
  tests**, including **17 new mutation cases**. The same command in api-ai passed
  **150 tests**. The existing Starlette/AnyIO deprecation warning remains.
- New tests execute the original upstream visibility/review/product-field
  functions with only their persistence call controlled, comparing their actual
  patch policies. They also cover physical/canonical field agreement, preserved
  content/tier/legacy metadata, revision/head/receipt atomicity, review
  reacceptance, late journal/feedback failure, graph refresh/rollback/privacy
  erasure, and a missing or unrelated target guard.
- `local-target.mjs --output <private-new-dir> --brand-id eddy --run-core
  --run-recording` run B passed **16 core and 20 recording cases** and closed its
  runtime. The added case changes all four product surfaces on an actual
  recording-derived memory, verifies exported flags plus four new operations
  and commits, confirms rejection hides it from vector search, then reaccepts it.
  The following existing cases prove reindexing, content correction, public
  memory deletion and account erasure still work.
- This local run uses actual Auth/Edge/Python Core/Jobs/D1/R2/DO/Queue with
  controlled AI and Vectorize. It is not model-quality, microphone UI or
  production-service acceptance. Seven final production modules are AST-identical
  to the staged modules exercised by run B; only pinned formatting changed.

## Hosted D1 verification

Owned run `memory-product-mutation-hosted-20260907-b` applied all prior App
migrations to a fresh real D1, retained a legacy memory and projection task,
then upgraded through the ordinary Wrangler command. The prior row and task
were unchanged. A private Worker executed the exact production statement batches
captured from the CPython owner through native D1 `batch`.

Four synthetic accounts received valid graph-backed Long-term snapshots from
the original promotion engine. A visibility mutation refreshed its item,
assertion, operation and commit together. Three other accounts proved atomic
rejection for a late graph-write failure, a concurrent legacy item revision and
a paid lock. Independent D1 queries confirmed unchanged snapshots after each
rejection, including the preceding graph where one remained eligible.

The run completed at `2026-09-07T07:22:58.743Z`; its owned Worker and D1 were both
observed absent after cleanup. Journal SHA-256:
`a4a81c369b867d2f455899b7bad189a56e37d3907dfe62ae37cebdddb780ef28`.
Final migration SHA-256:
`4d36a7622eaee86b67e074349e9c45910ab54cf83ac033a5ed9cf95d4e0ed131`.
The migration bytes match the final worktree. This proves hosted SQL portability
and transaction behavior, not native Python HTTP or a production release.

Run A failed during remote migration with `incomplete input: SQLITE_ERROR`;
its owned D1 was removed and no Worker was deployed. Local SQLite and the
installed Wrangler splitter accepted that earlier SQL, so the exact remote
parser cause is not established. The final DDL uses the existing parenthesized
CASE form and a guard scoped to the selected item; run B successfully applied it.
An earlier operator-script duplicate-variable syntax error occurred before any
resource creation and is not deployment evidence.

## Remaining production work

The native product mutations now share canonical apply, but review-queue
resolution, non-native writers, source deletion, consolidation, shared graph
projections and kernel outbox delivery still need convergence. Complete
history/revert and JIT remain unqualified. The 17 blocked upstream identities
and missing CF-4/CI-1 qualification owners are unchanged. Full retained-candidate
upgrade/restore qualification and Eddy's signed app against deployed production
services remain required; these tests do not substitute for them.

No production deployment or new app build was performed. The preceding live
observation still found nine Eddy production Workers absent; this run created
only its isolated test resources. Four protected prompt files remain
byte-identical to `9b7e48dca2`. Pinned Python formatting, whitespace and
upstream-touch checks passed with zero upstream changes. No push, PR or merge
was performed.
