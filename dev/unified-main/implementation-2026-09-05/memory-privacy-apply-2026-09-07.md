# Canonical memory privacy preparation — 2026-09-07

This preparation checkpoint is followed by the
[public deletion and finalization implementation](memory-privacy-delete-2026-09-07.md).

The previous receipt migration fenced creators but did not connect a canonical
delete transaction. `memory_privacy_apply.py` now prepares one bounded set of
complete lineages with the original upstream privacy scrubbers. It returns
content-free durable cleanup inventory, not a deletion acknowledgement. Public
DELETE routes remain on their prior implementation until cleanup and
finalization are connected; this preparation is not production readiness.

Migration 0175 adds the lineage view, transient transaction admission and durable
inventory. The transaction rechecks the account generation, exact canonical
control, item revision/version/metadata and complete connected lineage. It catches
incoming aliases added by legacy creators without an updated canonical head.
SQL lineage behavior matches the unchanged upstream owner for chains, missing
survivors, cycles, incoming aliases and Python Unicode whitespace handling.
Selection is bounded at 100 actual items and fails rather than partially deleting
a larger lineage. No additional product memory tier or parallel item store is
introduced.

Preparation atomically acquires destructive authority, scrubs the selected
canonical/physical semantic fields, appends a content-free privacy epoch and its
projection events, updates the control, and seals the HMAC receipts last. The
existing revision trigger produces exactly one new vector deletion revision.
A failed receipt, changed item/control/lineage, active hold or competing owner
rolls back the whole transaction. Unrelated items remain unchanged.

The new server-owned hold and destructive gate tables follow
`backend/database/legal_holds.py`. A legacy principal with no hold row is allowed;
an active trusted hold blocks acquisition, and a live competing operation owns
the gate for six hours. Hold placement and acquisition contend in D1. Payment
locks and canonical writer transition modes do not block privacy. The control
reader is separated from ordinary writer admission; existing intake and edit
callers still enforce that admission. Retrying an existing deletion reuses its
inventory and receipt; it cannot reopen a gate after concurrent finalization.
All four new tables join existing account erasure and mutation fences. They are
not exported as product history.

## Verification

The focused suite executes real production preparation and the complete App
migration chain in SQLite. It verifies legacy principals, paid locks, both
writer transition modes, same-inventory retry, concurrent finalization,
account erasure, canonical authority races, late receipt rollback and original
lineage parity. It also verifies that preparation retains historical operations
pending finalization, so a passing test cannot be mistaken for complete erasure.

`PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh` passed
route inventory, manifest validation, typecheck, **992 Worker / 867 Core / 150 AI
tests**, exit zero. After that suite had collected, the resume/finalization race
was fenced and three focused cases were added. The final
`uvx uv==0.12.3 run pytest -q --tb=short tests/test_memory_privacy_apply.py`
passed **20 cases**, exit zero, including the updated retry timestamp check.
The whole lane was not rerun after that localized follow-up; the 867 count must
not be presented as the final 870-case collection. Both logs use the
`memory-privacy-apply-` prefix in the private evidence directory.

The first whole-lane attempt found that the existing account erasure static
checker recognizes only `CREATE TRIGGER IF NOT EXISTS adf_*`; the new DDL
now uses that existing form. Its assertion was retained. No new manual check
or pass-only release entry is introduced. All new cases are discovered by the
existing Core pytest runner. Pinned Python formatting, diff whitespace and the
upstream-touch check pass; zero upstream files changed.

The private `memory-privacy-apply-hosted-20260907-a` run applied all prior App
migrations to an owned real D1 database, inserted a legacy row and projection
task, then upgraded through the ordinary Wrangler migration command. Both were
unchanged after upgrade. A private Worker executed the exact statement batches
produced by the real CPython preparation owner through the real D1 binding's
`batch` API. Two linked memories were scrubbed and sealed; a third was retained.
Three further accounts proved rollback after an injected late receipt failure,
a concurrent incoming alias, and an active legal hold. Independent D1 reads
confirmed no partial tombstone, inventory, gate or journal commit in those cases.

That probe does not execute native Python HTTP, provider cleanup, public DELETE,
or the new resume path. It is evidence for hosted SQL portability and transaction
behavior only. The owned Worker and D1 were both observed absent after cleanup.
Finished at `2026-09-07T05:43:25.994Z`; journal SHA-256:
`0bbee67e028925871380979ab52491a9ff34dbe74bf064dce6bada28a542fc71`.
Migration SHA-256:
`f7a153e7091583f1b5b098bf5d4c429bdf6ce8b6d2e6c4b308912edab1a54aa5`.
Private runner, plans and journal live in `$CODEX_HOME/eddy-production/` with the
`memory-privacy-apply-` prefix. No credentials or user data are committed.

## Remaining delivery boundary

Connect native/MCP/Developer public deletion to this owner and existing Jobs
vector erasure. Only observed provider absence permits history, review and
remaining projection cleanup, physical tombstone/inventory finalization, and a
successful API response. Delete-all/default scope and retained import/history
stores also need that same complete erasure contract. The hold/gate authority
currently protects this preparation owner, not all Cloudflare destructive
families. Existing writer convergence, history/revert, JIT and full CF-4/CI-1
qualification remain required. The earlier isolated small-batch 503 remains
unexplained; this change does not claim to fix it. Default prompts and Server OS
behavior are unchanged. No production deployment was performed.
