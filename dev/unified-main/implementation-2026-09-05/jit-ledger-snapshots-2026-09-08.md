# Cloudflare desktop knowledge-ledger snapshot consumers

Authenticated Edge and the actual Core composition now expose both upstream
knowledge-ledger snapshot GET routes. The builder stages original completion /
projection validation, prompt selection, wire models, mirror cursors, row/alias
projection, page and chain hashes, and failure decisions. Default prompts and
upstream source are unchanged. Only the persistence calls become asynchronous.

Migration 0192 stores one completion/projection pair per owner. Consumers bind
it to independent account generation, stable ledger writer mode/epoch and the
canonical head/sequence. Reads cannot create migration completion, switch writer
mode or renew stale proof. Prompt consumers recheck the receipt's current source
rows and physical lock/review/privacy fields; time-dependent read scores do not
invalidate unchanged persisted content. The original receipt wire remains intact.

Mirror readers preserve 200 default / 500 maximum rows, one sentinel, the original
15-minute HMAC cursor, owner/epoch binding, cumulative counts and lineage aliases.
SQL bounds each transferred source record to 1 MB and each page to 4 MB. A byte
failure returns no partial rows and cannot certify completion. Both the source
page and head/proof are rechecked before returning authority. API Core now needs
the independent `MEMORY_V3_CURSOR_SECRET` secret-name mapping. Local test targets
already derive these names from the shared required-secret owner; production
input and the private secret map were prepared separately without exposing values.

Privacy retirement removes stored derived prompt text in the same SQL transaction.
The account-deletion registry owns the new table. Completion and prompt projection
are rebuildable records, outside the original portable ledger-history collections.

## Verification

The actual ASGI composition with all App migrations passes **26 focused tests**
(23.65 seconds). Cases include authentication, current and stale proof, account
isolation, original prompt wire, two signed pages, all 502 rows across a 500-row
page boundary, both historical alias reasons, expired/tampered/cross-owner cursors,
changed head, global kill during a read, malformed/source-generation/deleted rows,
source mutation before the final read, time-dependent belief scores and SQL byte
bounds. Public privacy deletion removes the stored projection at the same commit.
The initial oversize fixture attempted content exceeding the existing 50,000
character SQL constraint; it was corrected to exercise oversized metadata through
that unchanged constraint, rather than weakening the production schema.

The full Worker suite passes **1056 tests in 129 files** (19.95 seconds), including
Edge authority forwarding, immutable source identities, required secret mappings
and account-deletion registry coverage. TypeScript and manifest validation pass:
654 Cloudflare routes, 619 upstream routes, zero legacy-owned and 17 with unfinished
family qualification. The staged source inputs are included in candidate hashing.

The actual hosted run `eddy-ledger-snapshots-20260908-a` passed **34 public HTTP
checks**, finishing at `2026-09-08T06:52:04.795Z`. It used actual Auth signup,
opaque session restore, JWT, Rate Limit, Edge, Core, D1 and all 195 App migration
files (through migration 0192) plus 10 Auth migrations. Positive prompt/mirror
reads, signed multi-page completeness, both historical alias reasons, wrong-owner
and tampered cursors, invalid page size, stale source/head, global kill, privacy
retirement and logout revocation all passed. The before/after canonical head was
unchanged by reads. Four test Workers, two databases and one Queue were deleted,
with absence observed for all seven resources.

Receipts and historical metadata were explicit controlled fixtures after actual
native intake. No model, native capture, migration producer or queued erasure
was exercised. This is consumer verification, not production qualification.
All 50 generated memory modules, three runtime modules and migration 0192 match
the uploaded build byte for byte. The complete Core regression passes **1285
tests** in 718.07 seconds, with one existing Starlette deprecation warning.

Evidence archive: `/Users/macstudio/.codex/eddy-production/ledger-snapshots-public-20260908/`.

## Remaining work

The actual migration sweep/cutover publisher must still publish completion and
projection receipts through canonical authority. MCP, Developer and integration
writers that still write memory directly must converge on the canonical commit
owner before production publication. Test-only receipts explicitly exercise the
consumers; they are not evidence that this producer exists or migration completed.
Native JIT behavior, queued erasure, common two-target contracts, CF-4/CI-1 release
executors and all nine Eddy production Worker deployments remain incomplete.

## Commands

From the worktree root, with the existing pinned environment:

```bash
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest deploy/cloudflare/python/api-core/tests/test_jit_ledger_snapshot_routes.py -q
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest deploy/cloudflare/python/api-core/tests -q
```

From `deploy/cloudflare`, using installed Node 22:

```bash
node node_modules/vitest/vitest.mjs run
node node_modules/typescript/bin/tsc --noEmit
node scripts/validate-manifests.mjs
```

The archived `eddy-ledger-snapshots-public-20260908-a.mjs` owns the real hosted
verification and cleanup; it is an execution record, not a replacement CI or
release gate. The new hermetic cases run in the existing Core/Worker suites.
