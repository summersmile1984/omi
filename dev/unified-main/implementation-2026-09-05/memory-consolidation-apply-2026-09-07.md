# Cloudflare canonical consolidation persistence — 2026-09-07

The previous review change could resolve an exact canonical review, but lacked
the consolidation transaction that produces that record. This change implements
the D1 adapter for the upstream planner's complete decision batch. It does not
add a replacement classifier, public promotion shortcut or default prompt.

## Policy and persistence

The ordinary kernel builder stages upstream `ConsolidationAgentDecision`,
`ConsolidationAgentBatch`, partition/reference validation, attribution rules,
normalization receipts, promotion audit and deterministic review construction.
The two platform patch builders are compared by executing the original upstream
`_apply_processed_result` and `apply_consolidation_decision` with only their
Firestore calls controlled. Their complete patches and stable operation fields
must match; wall-clock operation creation timestamps are not identities.

`apply_consolidation_batch` rehydrates the owner's pending items, candidates and
negative examples, checks the complete model partition, and plans the original
kernel applies. Required normalization precedes its terminal route, and promote
decisions precede dependent duplicate routes. A maximum of 20 decisions shares
one D1 batch, making the platform transaction stronger than the upstream's
per-decision persistence without changing any decision or admission policy.

The existing account/head guard captures every read item. Item changes, graph
assertions, deterministic reviews, operations, commit chain, control head and
outbox events commit together. A late review/graph/journal failure rolls back
normalization and all routes. Stale or repeated source snapshots require reload
and cannot create a second route. Queue publication occurs after durable commit.
The return value reflects stored JSON metadata and integer-second timestamps.

## Verification

- The focused full-migration SQLite tests cover all four routes for processed
  and required-pending sources, normalized receipt revisions, a public review
  acceptance after actual queue production, supersession and graph replacement,
  invalid model partitions/references/attribution, late transaction failures,
  and concurrent source/head/generation/payment-lock changes.
- Original upstream policy comparisons cover both normalization and terminal
  route patch construction. No upstream source or prompt is edited.
- `uvx uv==0.12.3 run --project deploy/cloudflare/python/api-core pytest -q
  --tb=short deploy/cloudflare/python/api-core/tests/test_memory_consolidation_apply.py`
  passed all 23 tests, including the final stronger required-normalization
  rollback fixtures. Pinned Python formatting and diff whitespace checks pass.
- `PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh` passed
  inventory/manifest/type checks, 995 Worker tests, 938 Core tests and 151 AI
  tests. Core retains one existing Starlette/AnyIO deprecation warning. The
  final stronger rollback fixtures were rerun in the focused suite after the
  full lane had collected its tests; the production source was unchanged.

## Hosted Python Worker and D1

The private `memory-consolidation-hosted-20260907-a` run built fresh current
Auth, Rate Limit, Python Core and Edge modules, created two isolated D1 databases,
and applied the ordinary complete migration chain. Native intake, content
correction and review read/resolve used the actual public routes and real Better
Auth accounts. A private Core fixture supplied candidate snapshots and controlled
model decisions to the unchanged production consolidation adapter. That fixture
required both a probe key and the ordinary request-bound Core HMAC assertion;
it was not committed to the repository or installed in production.

Four required-pending sources received the four distinct terminal routes in
one request: eight normalization/route commits, four completed sources, one
graph assertion, one exact-source review and zero residual admission guards.
The generated review was rejected for another account and accepted through the
owner's public route, returning its memory to pending Short-term. A subsequent
replace decision promoted a new source and superseded its old peer in the same
commit; only the new graph assertion remained. Injected graph and review INSERT
failures both returned 503 with every memory and journal table unchanged.
An incomplete model partition likewise returned 503 without writes.

The run finished at `2026-09-07T08:36:25.628Z`; all four Workers and both D1
databases were observed absent after cleanup. All 20 checked domain/adapter
module files match the final source byte-for-byte. Journal SHA-256:
`97ff018fb39a1ba04c0654111390e9ec314b26157ee52af7fdf13dce7bcbeb39`.
Private evidence is in `$CODEX_HOME/eddy-production/memory-consolidation-evidence-20260907.json`.
The actual model, candidate search, scheduler and user-interface loop were not
part of this test. This is not production-release qualification.

## Remaining delivery work

This module is not wired to a production scheduler or public mutation endpoint.
At this step candidate retrieval, real model calls, retry leases and the
recurrence workflow handoff still needed owners. The [model invocation follow-up](memory-consolidation-llm-2026-09-07.md)
connects the original messages to Workers AI and this apply transaction;
candidate retrieval, sizing, leases, scheduling and recurrence remain open. Recurrence-bearing batches currently fail before any
write; signals are not silently lost. At this step native POST still created
processed Short-term items without the required-normalization marker. The
[native intake follow-up](native-memory-normalization-2026-09-07.md) corrects that
boundary; existing processed snapshots and new pending inputs are both tested.
Other intake families and default list/read filtering remain required
before enabling automatic consolidation. Shared graph projection and complete
kernel-outbox consumption remain part of writer convergence. This work alone
does not qualify production deployment, history/revert or JIT.
