# Canonical memory deletion and provider completion — 2026-09-07

Native single/batch/all/default, MCP and Developer deletion now use the canonical
privacy preparation owner. A successful response requires completed provider
cleanup and physical memory/history erasure. Pending cleanup returns HTTP 503
with `memory_cleanup_pending` and `Retry-After: 2`; existing Jobs Queue and cron
continue the durable request without another client call. The native/MCP 200
body remains `{"status":"ok"}` and Developer remains `{"success":true}`.
Developer retains the upstream active paid-lock admission (402); native privacy
remains available for locked rows. An owned, unexpired receipt permits an
idempotent completed retry. Unknown IDs still return 404.

## Ownership and persistence

Migration 0176 adds final transaction admission, pending-inventory write fences
and durable all/default scopes. Core reacquires the existing destructive gate
and checks legal holds in the final D1 batch. Every selected tombstone must match
its immutable identity/revision/key and have zero external artifact receipts
or serving mappings, including receipts from earlier revisions. A late provider
write, changed authority or history deletion failure rolls the entire batch back.

Jobs obtains targets and renews the gate through method/path/audience-bound
internal Core assertions. It uses the existing artifact owner to observe real
provider erasure, then asks Core to finalize. Merely accepting an asynchronous
Vectorize deletion does not acknowledge the request. The existing service graph
is unchanged: Core emits Queue hints; Jobs uses its existing API_CORE binding.
No new credential, Core-to-Jobs binding or user-authored operation token exists.

All/default scopes survive between bounded 100-item batches. Default retains
Archive even if an Archive row is linked to a selected Short-term memory. The
scheduled reconciler revisits durable children and scopes in oldest-attempt
order. Pending inventory fences late creators beyond the 30-day receipt TTL.
After finalization only opaque receipts remain for the deleted identities.

Finalization removes referencing operations, commits, kernel/projection outbox,
review and non-active-route records, Archive history and lifecycle history.
Mixed historical receipts are removed whole; unrelated canonical memories remain.
Exact recursive JSON string values are references; object keys and substrings
are not. Memory usage-source identities are also removed, reducing the derived
historical memory counts. Original conversations and imported source artifacts
remain source data outside explicit memory deletion.

The changed physical-row/outbox expectations follow the existing upstream
`backend/database/memory_ledger.py::finalize_canonical_privacy_tombstones` and its
history erasure contract, rather than making tests accept the previous soft
DELETE implementation. The default scope follows
`canonical_memory_adapter.delete_default_canonical_memories`.

## Verification

All private evidence is under `$CODEX_HOME/eddy-production/`; no credentials or
user data are committed. The new tests are discovered by the existing route and
recording lanes.

- The route lane passed inventory, manifest validation, typecheck and all **995
  Worker tests**. Its initial Core stage exposed two obsolete soft-delete test
  expectations. After updating those to the upstream finalization contract,
  `uvx uv==0.12.3 run pytest -q --tb=short` passed **882 Core tests** and the
  same command in api-ai passed **150 tests**. The existing Starlette/AnyIO
  deprecation warning remains.
- The final deletion suite passed **13 cases**, including a subsequently added
  mixed Archive/lifecycle history case and explicit owner isolation. It covers
  public native/MCP/Developer deletion, lost-response retry, provider-pending
  rejection, late-history rollback, receipt expiry with pending inventory,
  default retention, durable 102-item continuation and private assertion
  authority/path/body checks. The whole Core suite was not rerun after that
  additional test; do not describe this as one 883-case run.
- The actual local product command was
  `PATH=<pinned-node-22-bin>:$PATH CLOUDFLARE_PYODIDE_CACHE_DIR=<existing-cache>
  node deploy/cloudflare/contracts/local-target.mjs --output <private-new-dir>
  --brand-id eddy --run-core --run-recording`. Run
  `memory-privacy-delete-local-20260907-b` passed **16 core and 19 recording
  cases** and closed its runtime. Auth, Edge, Python Core, Jobs, D1, R2, DO and
  Queue are actual local Workers; AI and Vectorize are controlled providers.
- The added recording case produced a memory through public audio/finalization,
  waited for real Jobs publication and public vector retrieval, then issued
  DELETE. Its first response was 503. Without another public DELETE, persisted
  memory/artifact/mapping counts changed from **1/1/1 to 0/0/0**, and durable
  cleanup inventory reached zero. The receipt remained, a subsequent DELETE
  returned 200, and both the source conversation and other account survived.
  `trace/memory-privacy-results.json` records these independent SQLite counts.

## Hosted D1 upgrade and finalization

The owned `memory-privacy-finalize-hosted-20260907-b` run created a fresh actual
D1 database, applied all migrations through 0175, inserted a legacy row and
projection task, then applied 0176 with ordinary Wrangler migrations. The legacy
row and task were unchanged. A private Worker executed the exact preparation
and finalization batches captured from the actual CPython owners through native
D1 `batch`; independent remote reads verified each result.

Two linked memories were physically erased and a third retained. Three further
accounts proved atomic rejection for an injected late history deletion failure,
a pending external writer and an active legal hold acquired after an abandoned
gate. All corresponding memory/history/inventory/gate snapshots were unchanged
after rejection. Supplying completed artifact-owner state allowed the pending
case to finalize on retry. This SQL portability/transaction probe does not
exercise native Python HTTP, real Vectorize cleanup or a production deployment.

The run finished at `2026-09-07T06:46:58.352Z`. The owned Worker and D1 database
were both observed absent after cleanup. Journal SHA-256:
`10e5888fcc92b7aedc3fde83676ea8c14873ea4f571ef7cafe1bae6f79fe1ede`.
Migration 0176 SHA-256:
`c5f58ec3992d553b3e54f97a7f095732eb46f0e8228fc255d1029ae8352a698e`.
Run A completed migration but received an unparseable HTML transaction response;
its cause was not established and it is not successful transaction evidence.
Its owned resources were also observed absent. Run B required an actual JSON
readiness response and completed all cases; no production binding was modified.

## Production boundary

A read-only observation at `2026-09-07T06:46:34.188Z` found all nine Eddy production
Workers absent. This change is local and does not establish a published product.
The 619 upstream-route inventory retains 17 blocked identities. CF-4 and CI-1
qualifier implementations are still missing; the full retained-candidate
upgrade/restore qualification and Eddy's new signed app production UI loop are
also unfinished. Complete canonical writer convergence, source deletion,
consolidation, graph and JIT remain required. The earlier intermittent small
batch 503 and large deletion heap/concurrency behavior are not resolved by this
verification. The existing real Vectorize cleanup probe remains separate from
this new local orchestration and hosted SQL evidence.

No default prompts or Server OS implementation changed. Four protected prompt
sources remain byte-identical to `9b7e48dca2`; the upstream-touch check reports
zero upstream changes. Pinned Python formatting and diff whitespace checks pass.
The signed Eddy app is unchanged by this backend-only work; no push, PR or merge
was performed.
