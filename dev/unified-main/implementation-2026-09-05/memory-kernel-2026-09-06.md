# Native Cloudflare canonical memory apply rules — 2026-09-06

The ordinary Core builder now stages nine upstream pure memory modules through
`scripts/memory_kernel_sources.py`: apply, admission, contracts, promotion,
domain vocabulary, operation identity, memory items, source evidence and
Short-term lifecycle. The projector changes only import-module names at parsed
import locations. It preserves all other source bytes and refuses dependencies
outside the reviewed pure module set. It creates no maintained source copies.
The same projector runs in Core's pytest setup, and frozen release identity
includes all nine upstream inputs.

This is a prerequisite to one durable Cloudflare apply owner. It is deliberately
not a new memory database, a public apply endpoint or a second mutation owner.
The pure engine's `committed` result describes the item, graph assertion,
operation receipt, next control head and outbox bundle that storage must commit
atomically. No existing D1 writer was replaced and no history/revert/JIT route
was promoted by this change. Inventory remains 602 staging-owned / 17 blocked.

## Verification

- `uvx uv==0.12.3 run pytest -q tests/test_memory_kernel.py`: **10 passed**.
  These execute the staged production rules for deterministic retries,
  already-committed replay, changed replay payload, head/account/source generation
  conflicts, tombstoned evidence, restricted-content delete-only projections,
  direct Long-term denial and valid/missing/stale promotion receipts. Successful
  promotion binds the graph assertion to the item's revision, content hash and
  new commit, using the original upstream admission receipt generator.
- `env PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh`:
  inventories, manifest validation and typecheck passed; **980 Worker tests in
  123 files, 743 Core tests and 150 AI tests passed**. Core reports one existing
  Starlette/AnyIO deprecation warning. uv repaired one local interpreter-cache
  entry before the successful run; this did not change project dependencies.
- Python formatting and `git diff --check` passed. After narrowing projector
  replacement to actual import locations, all nine generated modules were again
  compared byte-for-byte with the successfully hosted payload. Their bytes did
  not change, so the hosted behavior evidence remains current.

The native run built Core through the normal source builder, verified that the
frozen payload contained all nine kernel modules, then supplied a private probe
entrypoint to import and execute them in Cloudflare's Python Worker runtime.
That entrypoint was never added to the repository or public product routes. It
required a random probe key and consumed only synthetic memory/control inputs.
It created no D1, R2, queue, vector or production resource and made no model call.

All **13 native scenarios passed** against CPython-generated expectations:
Short-term intake; identical retry IDs; committed replay; changed-payload replay;
head, account-generation and source-generation conflicts; restricted-content
delete-only outbox; direct Long-term denial; valid, stale and missing promotion
receipts; and source deletion. Comparison includes status, next commit identity,
memory IDs/tiers/revisions, graph readiness/assertion count and outbox IDs/actions.
This proves native pure-rule execution, not persistence or public user behavior.
The probe hydrates source evidence into `MemoryEvidence` before apply; the pure
upstream engine expects typed authoritative evidence, not unvalidated JSON rows.

The owned Worker was `eddy-memory-kernel-20260906-a`. It was observed absent
before deployment; its version/tag were rechecked before deletion and absence
was observed afterward. The run finished at `2026-09-06T13:10:42.697Z`.
Journal SHA-256:
`73e483d8bf525682130017aae8566e82c1d1667d3cae022246aeb39a7a9c1282`.

Private evidence under `$CODEX_HOME/eddy-production/`:

- `memory-kernel-hosted-20260906-a/result.json`
- `memory-kernel-hosted-20260906-a/api-core/modules/memory_kernel_*.py`
- `memory-kernel-cases-20260906.json`
- `memory-kernel-routes-20260906.log`

## Required next boundary

The D1 adapter must read and fence current account/source generation, control
head, operation identity, authoritative evidence, target and superseded items.
It must atomically persist every successful apply result and reject stale input
without partial item, graph, ledger or outbox writes. A normal engine result or
a client-supplied snapshot must never be accepted as storage authority by itself.

Current native/MCP/Developer/Jobs/import/privacy writers must converge on that
owner while preserving the existing monotonic vector-publication and erasure
contracts. No shadow database or observation-only version table can qualify this
boundary. Consolidation, append-only facts, exact retry/reopen receipts, complete
lineage privacy, trigger feedback and ledger snapshots remain required work.
Default model prompts and Server OS runtime were not modified. Production and
the signed macOS end-to-end objective remain unfulfilled.
