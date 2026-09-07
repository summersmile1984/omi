# Cloudflare consolidation retry ownership — 2026-09-07

## Contract and implementation

The preceding hosted Qwen trial produced an invalid exact-duplicate create after
retrieving the existing memory at score 1. The shared validator rejected it, but
the Cloudflare invocation path lacked the upstream bounded retry/terminal owner.
This change closes that batch-level lifecycle; it does not enable a scheduler.

The projector stages the original `ConsolidationRetryState`, three-attempt limit,
600-second lease, source eligibility, safe failure codes and terminal-review
decision from `backend/utils/memory/canonical_consolidation.py`. Upstream files
and default prompts remain unchanged. D1 SQL is the storage adapter; no runtime
Firestore transaction facade was added.

`memory_consolidation_leases.py` owns exact-revision/content-hash attempt identity
and compare-and-set. The original state JSON is retained verbatim for ownership
checks. Account/source generations fence the current lease without resetting the
same source version's attempt budget. An expired or replaced task cannot refund
an attempt or commit a route. Ownership is also checked before model invocation.
Migration 0180 adds the non-content state table, account-erasure fences and purge
inventory, physical-source deletion cleanup and a lease check inside the existing
canonical apply transaction.

`memory_consolidation_runner.py` accepts at most 20 ordered IDs and executes one
model batch. Retry work is isolated; unselected IDs are returned explicitly to the
dispatcher. Every invocation gets a fresh ownership token even when delivery IDs
repeat. Index waits refund the attempt and persist a five-second due time. A real
model/parser/apply failure spends one attempt. On the third failure the original
review decision is applied; a failed review write attempts the original quarantine
route. If both writes fail, count-three work remains retryable for settlement.
An abandoned third attempt can only lease terminal settlement, never a fourth
model call. Successful application deletes its attempt atomically; terminal
application records the terminal state in that same D1 batch.

The low-level invocation/apply adapters remain internal primitives. Their new
leased runner is not registered as an unleased public or background endpoint.
Fair scanning/cursors, provider-window planning, Queue/cron dispatch, account
watermark advancement and recurrence-to-workflow handoff remain unfinished.
Recurring-signal batches are still refused before writes. Other canonical writer
families and full Eddy production qualification also remain incomplete.

## Verification

- Core full suite: **998 passed**, one existing Starlette warning, 309.50 seconds.
- API-AI full suite: **151 passed**, 9.76 seconds.
- Final TypeScript full suite: **1,000 passed** across 126 files, 12.07 seconds.
- Core focused retry/apply/invocation after the final SQL syntax correction:
  **58 passed**, 29.44 seconds; the new retry file contains 11 scenarios.
- Typecheck, manifest validation, pinned Python formatting and diff hygiene pass.
- The retry tests execute original upstream claim, transition and deferred-release
  transaction bodies as a test-only oracle. They also execute real SQLite/D1 SQL
  and native HTTP routes for three invalid decisions → review, index deferral,
  expired/replaced ownership at the write boundary, abandoned third attempts,
  review/quarantine write failures, isolated retry batches, source edits/deletion
  and generation changes that must not reset the budget.

The first TypeScript run found that the new account fences lacked the existing
`IF NOT EXISTS` declaration convention; the declarations were aligned and the
full suite passed. An initial all-SELECT-CASE static check was too broad for valid
nested CTE queries and was narrowed to standalone trigger statements. These
failed runs are retained alongside final results.

## Hosted migration incident and regression scope

Hosted attempt A stopped during a read-only Cloudflare observation, before any
resource creation. Attempt B created only its two fresh D1 databases and index,
then failed migration 0180 with `incomplete input: SQLITE_ERROR`. All three owned
resources were subsequently observed absent; no Worker had been deployed.

A separate minimal real D1 probe showed that a trigger containing standalone
`SELECT CASE ... END` was rejected by the REST query path, while
`SELECT (CASE ... END)` succeeded. SQLite and the installed Wrangler local SQL
splitter accepted the rejected form, so those checks alone did not reproduce the
remote failure. The migration now uses the existing parenthesized expression
convention. The separate probe database was also observed absent after cleanup.

`tests/d1-migration-transport.test.mjs` is discovered by the existing Cloudflare
CI/deploy route lane. It executes the installed CLI's split statements against
SQLite and separately labels the remote-parser syntax check as a **static
tripwire**, not behavioral remote-D1 coverage. The failed B run and minimal D1
comparison are its concrete incident evidence. This concerns immutable migration
DDL sent through a vendor parser, not a runtime helper that could be shared by
business callers; both auth/app transport execution and the app trigger convention
are checked in one test surface.

## Hosted retry cycle C: passed, with a retained provider response

Five fresh Workers (Auth, Rate Limit, Core, Jobs and Edge), two D1 databases
through migration 0180 and one 1,024-dimensional Vectorize index were used.
Authentication, native HTTP intake, BGE-M3 embedding, Jobs publication, candidate
retrieval, retry ownership, original parser/apply and public review APIs were real.
The private provider seam replayed the previously retained invalid Qwen response,
remapping only source/evidence IDs. No new Qwen sample was requested. The replay
was also used to seed the first memory before an existing duplicate was present.
Its usage records are synthetic replay accounting, not additional Qwen charges.

- Actual Jobs publication completed at `13:19:43.208Z`.
- At `13:19:47.738Z` and `13:19:55.174Z`, the batch deferred with attempt count 0,
  no response replay and an unchanged source row.
- At `13:20:04.058Z`, `13:20:06.124Z` and `13:20:08.334Z`, the same retained
  duplicate-create response was rejected. The authoritative retry count advanced
  1 → 2 → 3; the third transaction committed the original review route.
- The private driver asserted actual candidate identity/score, unchanged source
  rows after the first two failures, the archived review source, a public review
  record with the committed source revision, and no leaked review for another
  authenticated account. A fourth delivery returned `not_pending` with no replay.
- The public review-resolution endpoint accepted the review and requeued a newer
  source revision as pending. The apply guard table was empty after settlement.
- Both D1 migrations and the four changed retry/invocation/apply source files in
  this hosted payload match the current repository files byte-for-byte. The
  original prompt prefix remains 10,491 bytes with SHA-256
  `be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa`.

All five Workers, both D1 databases and the index were observed absent after
cleanup. The trial finished at `2026-09-07T13:20:29.974Z`. Journal SHA-256:
`b058f5a0ff837c1a53f0d36e7d03c122647f566718632d85e5ffcad7d17fa5a6`.

Private evidence is retained under
`$CODEX_HOME/eddy-production/memory-consolidation-retry-20260907/`, including all
three hosted attempts, the minimal D1 comparison, retained-response replay,
source hashes and test logs. The private driver still supplied each invocation;
this proves the bounded failure-recovery path, not unattended scheduling, fresh
Qwen semantic quality, macOS production connectivity or a production release.
