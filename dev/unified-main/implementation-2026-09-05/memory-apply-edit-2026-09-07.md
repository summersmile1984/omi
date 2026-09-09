# Cloudflare native memory content correction — 2026-09-07

Native `PATCH /v3/memories/{id}` previously updated content and its projection
revision without a matching canonical operation, commit or head. It now shares
the native intake control loader and journal writer, and persists the original
upstream apply result in one App D1 batch.

The correction policy matches upstream `update_canonical_memory_content`:
trim the explicit content, return to pending Short-term, mark the correction
as user asserted, reset settled promotion/graph admission, retain bounded prior
submission and processing history, and start a fresh Short-term expiry. No
prompt or model call is introduced. The canonical and existing vector outboxes
both request deletion until the pending correction is processed. The content,
item version/revision, receipt, commit/head and pending events roll back together.
Editing does not create another intake usage charge.

Migration 0173 extends the existing transient guard with expected target
revision, version, canonical metadata and device provenance. Paid-plan lock
denial remains 402, including a lock acquired after the read. Account/head/source
and target races fail without rebasing or partial writes. Closed, invalidated
and superseded rows return 404 rather than being resurrected.

An existing row with empty canonical metadata is a pre-journal read projection.
Its deterministic historical marker is transient and is not a fabricated
durable ledger commit. The same guarded edit adopts the row at its existing ID,
records the actual correction commit and retains capture provenance. A
nonempty, invalid canonical metadata object fails closed rather than selecting
that historical path. Historical Short-term, Long-term and Archive corrections
all converge to the same pending Short-term write policy.

## Verification

The focused edit and mutation-lock suites passed 59 cases through actual native
HTTP handlers and all App migrations. Coverage includes an executable policy
comparison against the upstream function, account isolation, all three
historical tiers, same-second revisions, paid-plan races, target/head/generation
conflicts, closed-record protection, malformed metadata and late journal errors.
The existing same-second projection test now expects deletion because upstream
content correction explicitly returns the item to pending processing; its
monotonic revision and atomic-outbox checks remain intact (INV-MEM-4).

`env PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh` passed
inventory/manifest validation, typecheck, 987 Worker tests in 124 files,
815 Core tests and 150 AI tests. Core retains its existing Starlette/AnyIO
deprecation warning. Private log: `memory-apply-edit-routes-20260907.log`.
The later release-source dependency update passed all seven
`tests/release-files.test.mjs` cases; it binds the upstream correction function
used by the behavioral comparison into candidate identity. Pinned Python
formatting, diff whitespace and the zero-upstream-touch check passed.

## Actual hosted HTTP and D1

The owned `memory-edit-hosted-20260907-a` run built fresh Auth, Rate Limit,
Core and Edge modules and applied all 10 Auth / 176 App migrations to two
isolated D1 databases. A synthetic account authenticated through real Better
Auth and exercised the native HTTP handlers. The private outer Edge guard
restricted access to the test deployment; normal Auth/Edge/Core business code
handled the requests. There were no model calls or production data bindings.

- A Chinese/emoji content correction returned 200, advanced the item revision
  once, kept the original source commit and recorded the new operation/head.
  Canonical and vector outboxes both requested deletion for pending processing.
- An unrelated account returned 404. A paid-plan lock returned 402 without
  changing the item, head, projection or row counts.
- Three injected late failures (operation, commit and kernel outbox) each
  returned 503 and preserved the complete prior item, head, projection and counts.
- Actual pre-journal Short-term, Long-term and Archive rows were corrected in
  place without duplicated items, fabricated history or new intake charges.
- Owner export contained the corrected content and its committed user-mutation
  receipts. The existing single/batch intake and rollback probes also passed,
  including all seven exact-1-MB ASCII/Chinese/emoji cases and zero-write 413
  rejections for one-byte overflow and the previous approximately 8 MB request.

The run completed at `2026-09-07T03:44:24.431Z`; all four owned Workers and both
databases were observed absent after cleanup. Journal SHA-256:
`63cf086944192e1689ef96eef9ae324ddb90091e29d2bea9e641536cad6006ba`.
The three production Python modules and migration 0173 match the hosted frozen
bytes exactly. Private evidence: `memory-apply-edit-evidence-20260907.json` and
`memory-edit-hosted-20260907-a/` under `$CODEX_HOME/eddy-production/`.

The four protected prompt/provider files still match `9b7e48dca2` byte for byte.
The `2026-09-07T03:42:19.071Z` production observation found all nine intended
Eddy Workers absent and verified the refreshed Eddy.app's strict signature.
This isolated run does not establish production deployment or desktop UI
acceptance.

## Remaining scope

This change covers native content correction. Visibility, review, read/baseline,
deletion, other intake and mutation families, consolidation, graph assertion
storage and the complete ledger/JIT authority still require convergence.
No route inventory entry is promoted by this change. CF-4/CI-1 and the Eddy
production client loop remain unqualified; this is not a production deployment.
