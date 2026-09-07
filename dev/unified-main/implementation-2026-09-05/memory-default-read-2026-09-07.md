# Cloudflare default memory lifecycle reads — 2026-09-07

Before this repair, the native list tested only deleted/invalid timestamps.
It returned archived, hidden/superseded, source-removed and user-rejected rows.
Ordinary product search excluded some product flags but did not check processing
state and labeled every result processed. With actual consolidation writes now
available, this would expose terminal decisions and raw inputs to default reads.

## Change and contract

One D1 predicate now owns lifecycle eligibility for the native list and ordinary
product search. It filters before COUNT/LIMIT/OFFSET. Processed Short-term and
Long-term records require active record/source state and the original sensitivity
and visibility rules. The restricted-label set comes directly from the unchanged
staged upstream model. Native lists additionally preserve the original required-
pending exception so owners can see submissions before model processing. Search
has no pending exception and returns actual persisted state/status. User rejection
is honored in both canonical metadata and the historical product column.

TTL alone does not hide active Short-term records: an authoritative lifecycle
transition must dispose of them. Already-shipped generation-zero processed rows
with empty metadata and no memory control, operation or receipt remain readable.
Reads neither normalize input nor create a receipt. No default prompt changed.

The regression oracle executes the original bodies from
`database/product_memory_items.py`, `canonical_visibility_filter.py`,
`device_scope_filter.py` and `_canonical_scan_item_visible`, with the same staged
models and controlled telemetry. It compares real authenticated HTTP results
backed by all D1 migrations across 17 lifecycle/source/review/sensitivity cases,
then checks count and one-item pages, owner isolation and storage errors. The
pre-journal test starts with no memory control or receipts and verifies none
are created by a read. The pre-fix regression run reproduced two failures and
one passing storage-error case; the old native list exposed 11 extra records.

Existing search fixtures now explicitly model historical processed snapshots.
The edit/review test checks content/visibility before rejection, then verifies
that rejection persists and removes the row from default lists. These expectations
come from the original canonical visibility policy, not a weakened assertion.

## Verification

- `PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python
  -m pytest -q --tb=short -p no:cacheprovider
  deploy/cloudflare/python/api-core/tests`: **950 passed**, one dependency
  deprecation warning, 288.23 seconds. Pinned Python formatting and whitespace
  checks pass. No Worker/AI source changed in this step.
- The fresh ordinary Core/Edge/Auth/Rate Limit build and isolated hosted run
  completed at `2026-09-07T09:32:23.680Z`. Actual normalization and all four
  consolidation routes, public review acceptance, graph replacement and injected
  transaction rollback remained successful. Candidate/model responses were
  controlled; this is not evidence of live inference or scheduling.
- Default native reads contained exactly 104 eligible rows, including the
  pending required submissions and all 100 memories from an exact 1 MB UTF-8
  batch with complete content readback. Ordinary search returned only the one
  processed replacement. Archived, hidden and superseded records were absent;
  one-item pagination/counts and another account's empty results passed.
- All four owned Workers and both D1 databases were observed absent after
  cleanup. The hosted result SHA-256 is
  `d6e8e7c89b094d033db6ac092f5e76c3b2551def740e5b1e0272cbda2b65b040`.
  The three checked runtime modules match the hosted build byte-for-byte.
  Logs, command, source hashes and cleanup observations are preserved under
  `$CODEX_HOME/eddy-production/default-read-evidence-20260907.json` and
  `default-read-hosted-20260907-a/`.

`FC-memory-default-read-skips-lifecycle` records this observed boundary and the
existing-runner regression surface (`Failure-Class: new`). No extra CI lane,
upstream edit or other failure-class lifecycle transition is introduced.

## Remaining production work

This change fixes persisted lifecycle eligibility. It does not yet implement
active-alias lineage collapse, native device/cursor parity or explicit native
archive reads. The ordinary search's separate count and page queries also retain
their existing concurrency behavior. Other reader/writer families, actual
consolidation inference, candidates, leases, scheduling, recurrence and kernel-
outbox delivery remain part of convergence. Production Workers, complete CF-4 /
CI-1 qualification and the Eddy production UI loop remain unverified.
