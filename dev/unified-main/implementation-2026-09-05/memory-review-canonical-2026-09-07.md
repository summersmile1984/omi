# Canonical memory review resolution — 2026-09-07

Review acceptance and correction now share the canonical user-mutation owner;
rejection and timeout/drop share canonical privacy preparation and finalization.
The decision and its redacted review record commit with the item, journal and
control head. Migration 0178 checks the exact pending review and canonical
source inside the enclosing transaction, and requires a committed resolution
before removing its temporary guard. The table participates in account erasure.

## Upstream behavior and source authority

The policy owner is `resolve_canonical_memory_review` in
`backend/utils/memory/canonical_memory_adapter.py`; the source/read/write
contracts are in `backend/database/memory_apply_store.py`. Tests execute the
actual upstream accept/correct functions with only persistence controlled and
compare the resulting patch with this target's policy.

- Accept/correct return the reviewed candidate to pending Short-term, reset
  settled processing metadata and require normal consolidation again. They do
  not invalidate another conflict memory, grant promotion or keep a graph claim.
- Correction merges arguments and optionally replaces content. `target_fact_id`
  is audit metadata on this canonical route, not permission to edit that other
  fact. The queue stores neither correction text nor a duplicate candidate.
- Reject/drop privacy-tombstone the candidate's canonical lineage, recheck legal
  authority, wait for provider erasure and remove the physical memory/history.
  A payment lock does not prevent this privacy operation when review source
  authority is still current. An intervening lock/edit that advanced the source
  revision makes its preceding review stale.
- Explicit `current_veracity: 0` takes precedence over the stored candidate's
  higher value during timeout. This follows upstream's `is not None` selection.
- Completed accepted/corrected requests replay without a new operation/commit.
  Pending privacy work returns 503 `memory_cleanup_pending` with `Retry-After: 2`;
  repeat requests resume the durable cleanup inventory. After full privacy
  erasure the derived queue row is gone, so later lookups return 404.

A pending review must match its source's actual commit, item revision, content
hash and consolidation `promotion.route = review`. Historical timestamp-based
D1 rows are projected as redacted stale reviews and cannot mutate memory.
Native intake no longer substitutes a structural-field conflict heuristic for
canonical consolidation. The canonical producer itself remains part of the
unfinished consolidation work; this change does not claim to finish that loop.

Resolved rows expose null source metadata. The retained 0093 NOT NULL columns
use empty strings, revision 1 and impact 0 as non-authoritative storage sentinels.
No previous content hash survives them. Privacy finalization removes the entire
derived review row rather than retaining a content-derived deletion receipt.

## Verification

The existing `deploy/cloudflare/ci/routes.sh` passed inventory/manifest/typecheck
and 995 Worker cases after the new transient table's account fences were aligned
with the existing convention. Its first run caught that declaration mismatch;
no test was weakened. Final Core passed **915** cases with one existing
Starlette/AnyIO warning; AI passed **151**. Both use `uvx uv==0.12.3 run pytest
-q --tb=short` (the final Core run supplied its explicit project/test directory
from the repository root). Exact logs/hashes are in the private evidence JSON.
Core's suite covers source/owner isolation, historical principals, same-second
staleness, original upstream patch policies, unchanged conflict peers, locked
privacy deletion, zero-veracity timeout, late decision-write rollback, queue
races, missing enclosing guards and provider-pending retries.

Hosted run B uses actual Auth/Edge/Python Core/D1 over HTTP, with synthetic
accounts, controlled prior consolidation-review fixtures and controlled provider
completion. The fixture sets up already-reviewed source state; it is not a
production consolidation executor. No LLM/ASR/TTS call or real external vector
erasure is claimed by this run.

The ordinary remote migration upgraded through 0178 while preserving a prior
memory and historical review row. HTTP then verified owner isolation,
acceptance, correction, rejection, explicit-zero timeout/drop, accepted replay,
late decision-write failure during both ordinary and privacy transactions, and
a pending provider journal followed by retry/final physical erasure. Independent
D1 reads checked the rows, operation/commit counts, control head and removals.
The 26 HTTP requests reached actual deployed Python routes, not captured SQL
through a JavaScript surrogate.

B completed at `2026-09-07T08:02:24.421Z`; four owned Workers and two D1 databases
were observed absent after cleanup. Journal SHA-256:
`5008bb25c12e5a46588dbc39c47b18b6a575cb2b7fadc55ce87d287092a5e64f`.
Migration 0178 SHA-256:
`e9162d51a848ce5679ea23b03cb7d961fb6f3a0770389be3700e863cc283e29e`.
The final migration and shared raw-path extractor bytes match the deployed run;
eight final Core modules match its staged code by AST.

Run A exposed the encoded-path authentication defect before reaching review
logic. Its resources were also removed. The shared repair and its evidence are
recorded in [request-assertion-path-2026-09-07.md](request-assertion-path-2026-09-07.md).

## Remaining delivery

Canonical consolidation/review production, non-native writers, source deletion,
shared graph projections, kernel outbox delivery and complete history/revert/JIT
remain unfinished. The 17 blocked upstream identities and missing CF-4/CI-1
qualification owners are unchanged. Full retained-candidate upgrade/restore,
production Worker release and the signed Eddy app's actual production UI loop
are still required. This verification does not replace those gates.

The read-only production observation at `2026-09-07T08:04:02.091Z` still found
all nine Eddy production Workers absent. No production deployment, app rebuild,
push, PR or merge was performed. Server
OS code and all four protected prompt files remain unchanged. Private logs,
source hashes, live production observation and commit IDs are recorded under
`$CODEX_HOME/eddy-production/memory-review-*`.
