# Native memory required normalization — 2026-09-07

Native Cloudflare POSTs were entering the kernel without required-processing
metadata, so its fallback classified raw input as processed and the vector
projection requested an upsert. This was observed in the pre-production native
HTTP/consolidation verification and reproduced by the local SQL tests. Upstream
`MemoryService.create_external_memory` and `create_external_memory_batch` both
call `required_processing_payload`; its contract requires pending normalization
before the input can be admitted to durable memory.

## Repair

The authoritative native intake funnel now calls that unchanged upstream helper
and stages `_product_metadata_from_payload` with its original attribution enum.
Category, tags and known source identity accompany the submission. Single routes
supply `v3_manual` or `v3_api`; batches supply `v3_batch`. This argument is required
at every in-tree call, and arbitrary processing/promotion values in an HTTP body
are ignored by the existing input model.

The submission reuses the route's captured server acceptance timestamp. Rebuilding
an operation a day later therefore has exactly the same identity. Existing whole
replays remain no-ops; changed metadata and mixed replays remain errors. Native
content stays readable and byte-identical to the accepted input. Initial kernel
and vector publication work is delete-only; unlock alone does not normalize a
pending input. No model call or default-prompt change occurs in the POST.

Already-persisted processed rows retain their state. The vector freshness tests
now explicitly seed that prior native row shape before creating vector mappings.
They keep their original stale-score, revision race, publication repair and
rollback assertions. Lock/unlock tests cover both historical processed rows and
new pending rows. The changed expectations are grounded in upstream
`required_processing_payload`, `_processing_state_for_promotion`, and the
existing canonical vector admission policy, not a relaxed test contract.

## Hosted evidence

`native-memory-normalization-hosted-20260907-a` built fresh current Auth, Rate
Limit, Python Core and Edge Workers with two isolated D1 databases. Actual public
single POSTs entered pending state without the prior content-edit workaround.
Four controlled model decisions then exercised real normalization receipts and
all four terminal routes in the production D1 adapter. The generated review
resolved through the owner's public API and rejected another account. Graph
replacement and injected graph/review failures retained the previous atomicity
checks. Candidate retrieval and model responses were controlled; actual inference
and scheduling were not exercised.

A separate request contained exactly 1,000,000 UTF-8 bytes and 100 Chinese/emoji
memories. Every item was written and read back with its original content, pending
state, `v3_batch` submission identity and no invented processing receipt. All 100
vector intents were delete-only. The run completed at
`2026-09-07T08:56:25.161Z`; all four Workers and both databases were observed absent
after cleanup. This did not deploy Eddy production services.

## Verification and remaining work

The initial focused set passed 63 tests. The first complete lane passed 995
Worker tests, then exposed eight vector-test fixture assumptions (938 Core tests
passed); those fixtures now distinguish existing processed state from current
raw intake. The final Core suite passed 947 tests and the AI suite passed 151.
All 21 checked runtime modules match the hosted build byte-for-byte. The hosted
result SHA-256 is `8c21abebe7b8f1e3252daa688a4fa7de445f84c5e57ee01ee00b1b01c9a4c530`.
Commands, log hashes and module hashes are preserved in the private
`native-memory-normalization-evidence-20260907.json` record.

`FC-explicit-memory-skips-required-processing` records this boundary and its
existing-runner regression tests. The fix declares `Failure-Class: new`; it does
not change another class's lifecycle or add a separate CI lane. No upstream file
is edited. Other intake families, native default-read policy, actual consolidation
model/candidate processing, leases, scheduling and recurrence handoff still need
convergence before production qualification, history/revert or JIT is complete.
